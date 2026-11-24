"""Carga idempotente do Marco 1 (M1) no Neo4j usando UNWIND + MERGE."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from neo4j import Driver
from neo4j.exceptions import Neo4jError

from camara.carga.schema import CONSTRAINTS_M1
from camara.coleta.m1 import DadosM1

CYPHER_MERGE_LEGISLATURA = """
MERGE (l:Legislatura {id: $id})
SET l.inicio = $inicio,
    l.fim = $fim,
    l.uri = $uri
"""

CYPHER_MERGE_PARTIDOS = """
UNWIND $linhas AS row
MERGE (p:Partido {sigla: row.sigla})
SET p.id = row.id,
    p.nome = row.nome,
    p.uri = row.uri
"""

CYPHER_MERGE_DEPUTADOS = """
UNWIND $linhas AS row
MERGE (d:Deputado {id: row.id})
SET d.nome = row.nome,
    d.uf = row.uf,
    d.urlFoto = row.urlFoto,
    d.uri = row.uri,
    d.email = row.email
WITH d, row
MERGE (l:Legislatura {id: row.idLegislatura})
MERGE (d)-[:EXERCE_MANDATO_EM]->(l)
WITH d, row
FOREACH (_ IN CASE WHEN row.uf IS NOT NULL AND row.uf <> '' THEN [1] ELSE [] END |
    MERGE (u:UF {sigla: row.uf})
    MERGE (d)-[:REPRESENTA]->(u)
)
WITH d, row
FOREACH (_ IN CASE WHEN row.siglaPartido IS NOT NULL AND row.siglaPartido <> '' THEN [1] ELSE [] END |
    MERGE (p:Partido {sigla: row.siglaPartido})
    MERGE (d)-[:FILIADO_A]->(p)
)
"""

CYPHER_CONTAR_NOS = """
MATCH (n)
UNWIND labels(n) AS rotulo
RETURN rotulo, count(*) AS total
ORDER BY rotulo
"""

CYPHER_CONTAR_RELACOES = """
MATCH ()-[r]->()
RETURN type(r) AS tipo, count(*) AS total
ORDER BY tipo
"""


@dataclass
class ResumoCargaM1:
    legislatura_id: int
    registros_partidos_api: int
    registros_deputados_api: int
    deputados_ids_distintos_api: int
    nos_por_rotulo: dict[str, int]
    relacoes_por_tipo: dict[str, int]
    tempo_segundos: float

    @property
    def total_nos(self) -> int:
        return sum(self.nos_por_rotulo.values())

    @property
    def total_relacoes(self) -> int:
        return sum(self.relacoes_por_tipo.values())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_nos"] = self.total_nos
        data["total_relacoes"] = self.total_relacoes
        return data


def _em_lotes(itens: list[dict[str, Any]], tamanho: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(itens), tamanho):
        yield itens[i : i + tamanho]


def normalizar_legislatura(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(raw["id"]),
        "inicio": raw.get("dataInicio"),
        "fim": raw.get("dataFim"),
        "uri": raw.get("uri"),
    }


def normalizar_partido(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(raw["id"]) if raw.get("id") is not None else None,
        "sigla": (raw.get("sigla") or "").strip(),
        "nome": (raw.get("nome") or "").strip(),
        "uri": raw.get("uri"),
    }


def normalizar_deputado(raw: dict[str, Any], legislatura_padrao: int) -> dict[str, Any]:
    sigla_uf = (raw.get("siglaUf") or "").strip() or None
    sigla_partido = (raw.get("siglaPartido") or "").strip() or None
    return {
        "id": int(raw["id"]),
        "nome": (raw.get("nome") or "").strip(),
        "uf": sigla_uf,
        "urlFoto": raw.get("urlFoto"),
        "uri": raw.get("uri"),
        "email": raw.get("email"),
        "siglaPartido": sigla_partido,
        "idLegislatura": int(raw.get("idLegislatura") or legislatura_padrao),
    }


class Neo4jLoaderM1:
    """Executa criação de constraints e carga idempotente do M1 no Neo4j."""

    def __init__(self, driver: Driver, tamanho_lote: int = 500) -> None:
        self.driver = driver
        self.tamanho_lote = tamanho_lote

    def criar_constraints(self) -> None:
        with self.driver.session() as session:
            for stmt in CONSTRAINTS_M1:
                session.run(stmt).consume()

    def limpar_banco(self) -> None:
        """Remove todos os nós e relações (usado apenas quando o usuário pede recarga limpa)."""
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()

    def carregar(self, dados: DadosM1, limpar_antes: bool = False) -> ResumoCargaM1:
        inicio = time.perf_counter()

        if limpar_antes:
            self.limpar_banco()

        self.criar_constraints()

        leg = normalizar_legislatura(dados.legislatura)
        partidos = [p for p in (normalizar_partido(r) for r in dados.partidos) if p["sigla"]]
        deputados = [normalizar_deputado(r, leg["id"]) for r in dados.deputados]

        with self.driver.session() as session:
            session.execute_write(
                lambda tx: tx.run(CYPHER_MERGE_LEGISLATURA, **leg).consume()
            )

            for lote in _em_lotes(partidos, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_PARTIDOS, linhas=rows).consume()
                )

            for lote in _em_lotes(deputados, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_DEPUTADOS, linhas=rows).consume()
                )

        nos, relacoes = self.contar_grafo()
        duracao = round(time.perf_counter() - inicio, 4)

        ids_distintos = len({d["id"] for d in deputados})

        return ResumoCargaM1(
            legislatura_id=leg["id"],
            registros_partidos_api=len(dados.partidos),
            registros_deputados_api=len(dados.deputados),
            deputados_ids_distintos_api=ids_distintos,
            nos_por_rotulo=nos,
            relacoes_por_tipo=relacoes,
            tempo_segundos=duracao,
        )

    def contar_grafo(self) -> tuple[dict[str, int], dict[str, int]]:
        with self.driver.session() as session:
            nos = {
                rec["rotulo"]: int(rec["total"])
                for rec in session.run(CYPHER_CONTAR_NOS)
            }
            relacoes = {
                rec["tipo"]: int(rec["total"])
                for rec in session.run(CYPHER_CONTAR_RELACOES)
            }
        return nos, relacoes

    def versoes_neo4j(self) -> dict[str, str]:
        """Consulta versão do Neo4j e do plugin GDS no servidor conectado."""
        info: dict[str, str] = {}
        with self.driver.session() as session:
            rec = session.run(
                "CALL dbms.components() YIELD name, versions, edition "
                "WHERE name = 'Neo4j Kernel' "
                "RETURN versions[0] AS version, edition"
            ).single()
            if rec:
                info["neo4j"] = f"{rec['version']} ({rec['edition']})"
            try:
                gds = session.run("RETURN gds.version() AS version").single()
                if gds:
                    info["gds"] = str(gds["version"])
            except Neo4jError as exc:
                info["gds"] = f"indisponível ({exc.__class__.__name__})"
        return info
