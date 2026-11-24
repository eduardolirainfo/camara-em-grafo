"""Carga idempotente do Marco 2 (M2): proposições, temas, autores e COAUTOR_COM."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from neo4j import Driver

from camara.analise.coautoria import ResumoCoautoria, recalcular_coautoria
from camara.carga.neo4j_loader import CYPHER_CONTAR_NOS, CYPHER_CONTAR_RELACOES
from camara.carga.schema import CONSTRAINTS_M2, INDEXES_M2
from camara.coleta.arquivos import ArquivosAnoM2, extrair_id_de_uri, ler_csv_camara

CYPHER_MERGE_PROPOSICOES = """
UNWIND $linhas AS row
MERGE (p:Proposicao {id: row.id})
SET p.siglaTipo = row.siglaTipo,
    p.numero = row.numero,
    p.ano = row.ano,
    p.ementa = row.ementa,
    p.dataApresentacao = row.dataApresentacao,
    p.uri = row.uri
"""

CYPHER_MERGE_TEMAS = """
UNWIND $linhas AS row
MERGE (t:Tema {cod: row.cod})
SET t.nome = row.nome
"""

CYPHER_MERGE_TRATA_DE = """
UNWIND $linhas AS row
MATCH (p:Proposicao {id: row.idProposicao})
MATCH (t:Tema {cod: row.codTema})
MERGE (p)-[:TRATA_DE]->(t)
"""

CYPHER_MERGE_AUTORES = """
UNWIND $linhas AS row
MATCH (d:Deputado {id: row.idDeputado})
MATCH (p:Proposicao {id: row.idProposicao})
MERGE (d)-[r:AUTOR_DE]->(p)
SET r.ordem = row.ordem,
    r.proponente = row.proponente
"""

CYPHER_DEPUTADOS_EXISTENTES = "MATCH (d:Deputado) RETURN d.id AS id"


@dataclass
class ResumoCargaM2:
    legislatura_id: int
    anos: list[int]
    tipos_filtrados: list[str]
    proposicoes_lidas_csv: int
    proposicoes_no_recorte: int
    temas_distintos_csv: int
    relacoes_tema_lidas: int
    autores_lidos_csv: int
    autores_deputados_vinculados: int
    autores_ignorados_nao_deputado: int
    autores_ignorados_fora_legislatura: int
    coautoria: ResumoCoautoria
    nos_por_rotulo: dict[str, int]
    relacoes_por_tipo: dict[str, int]
    tempo_carga_segundos: float
    tempo_total_segundos: float

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


def _to_int(val: Any) -> int | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def normalizar_proposicao(raw: dict[str, Any]) -> dict[str, Any] | None:
    prop_id = _to_int(raw.get("id"))
    if prop_id is None:
        return None
    return {
        "id": prop_id,
        "siglaTipo": (raw.get("siglaTipo") or "").strip().upper(),
        "numero": _to_int(raw.get("numero")),
        "ano": _to_int(raw.get("ano")),
        "ementa": (raw.get("ementa") or "").strip(),
        "dataApresentacao": (raw.get("dataApresentacao") or "").strip() or None,
        "uri": (raw.get("uri") or "").strip() or None,
    }


def normalizar_tema_vinculo(raw: dict[str, Any]) -> dict[str, Any] | None:
    prop_id = _to_int(raw.get("idProposicao")) or extrair_id_de_uri(raw.get("uriProposicao"))
    cod = _to_int(raw.get("codTema"))
    nome = (raw.get("tema") or "").strip()
    if prop_id is None or cod is None:
        return None
    return {
        "idProposicao": prop_id,
        "codTema": cod,
        "nomeTema": nome,
    }


def normalizar_autor(raw: dict[str, Any]) -> dict[str, Any] | None:
    prop_id = _to_int(raw.get("idProposicao")) or extrair_id_de_uri(raw.get("uriProposicao"))
    dep_id = _to_int(raw.get("idDeputadoAutor"))
    if prop_id is None:
        return None
    ordem = _to_int(raw.get("ordemAssinatura"))
    prop_flag = str(raw.get("proponente") or "").strip() in {"1", "true", "True"}
    return {
        "idProposicao": prop_id,
        "idDeputado": dep_id,
        "ordem": ordem,
        "proponente": prop_flag,
    }


class Neo4jLoaderM2:
    """Cria constraints/índices e executa carga idempotente do M2 no Neo4j."""

    def __init__(self, driver: Driver, tamanho_lote: int = 2000) -> None:
        self.driver = driver
        self.tamanho_lote = tamanho_lote

    def criar_constraints_e_indices(self) -> None:
        with self.driver.session() as session:
            for stmt in (*CONSTRAINTS_M2, *INDEXES_M2):
                session.run(stmt).consume()

    def limpar_m2(self) -> None:
        """Remove apenas nós e relações introduzidos pelo M2 (preserva M1)."""
        with self.driver.session() as session:
            session.run("MATCH ()-[r:COAUTOR_COM]->() DELETE r").consume()
            session.run("MATCH (p:Proposicao) DETACH DELETE p").consume()
            session.run("MATCH (t:Tema) DETACH DELETE t").consume()

    def _carregar_ids_deputados_existentes(self) -> set[int]:
        with self.driver.session() as session:
            return {int(rec["id"]) for rec in session.run(CYPHER_DEPUTADOS_EXISTENTES)}

    def contar_grafo(self) -> tuple[dict[str, int], dict[str, int]]:
        with self.driver.session() as session:
            nos = {rec["rotulo"]: int(rec["total"]) for rec in session.run(CYPHER_CONTAR_NOS)}
            relacoes = {
                rec["tipo"]: int(rec["total"]) for rec in session.run(CYPHER_CONTAR_RELACOES)
            }
        return nos, relacoes

    def carregar(
        self,
        arquivos_por_ano: list[ArquivosAnoM2],
        legislatura_id: int,
        tipos: set[str] | None = None,
        minimo_coautorias: int = 1,
        limpar_antes: bool = False,
    ) -> ResumoCargaM2:
        inicio_total = time.perf_counter()

        if limpar_antes:
            self.limpar_m2()

        self.criar_constraints_e_indices()
        deputados_conhecidos = self._carregar_ids_deputados_existentes()

        proposicoes_lidas = 0
        proposicoes_por_id: dict[int, dict[str, Any]] = {}
        temas_por_cod: dict[int, str] = {}
        vinculos_tema: list[dict[str, Any]] = []
        autores_lidos = 0
        autores_nao_deputado = 0
        autores_fora_legislatura = 0
        vinculos_autor: list[dict[str, Any]] = []

        for arq in arquivos_por_ano:
            for row in ler_csv_camara(arq.proposicoes):
                proposicoes_lidas += 1
                norm = normalizar_proposicao(row)
                if norm is None:
                    continue
                if tipos and norm["siglaTipo"] not in tipos:
                    continue
                proposicoes_por_id[norm["id"]] = norm

        ids_proposicoes = set(proposicoes_por_id.keys())

        for arq in arquivos_por_ano:
            for row in ler_csv_camara(arq.temas):
                norm = normalizar_tema_vinculo(row)
                if norm is None or norm["idProposicao"] not in ids_proposicoes:
                    continue
                temas_por_cod[norm["codTema"]] = norm["nomeTema"]
                vinculos_tema.append(
                    {"idProposicao": norm["idProposicao"], "codTema": norm["codTema"]}
                )

            for row in ler_csv_camara(arq.autores):
                autores_lidos += 1
                norm = normalizar_autor(row)
                if norm is None or norm["idProposicao"] not in ids_proposicoes:
                    continue
                if norm["idDeputado"] is None:
                    autores_nao_deputado += 1
                    continue
                if deputados_conhecidos and norm["idDeputado"] not in deputados_conhecidos:
                    autores_fora_legislatura += 1
                    continue
                vinculos_autor.append(norm)

        proposicoes_lista = list(proposicoes_por_id.values())
        temas_lista = [{"cod": cod, "nome": nome} for cod, nome in temas_por_cod.items()]

        with self.driver.session() as session:
            for lote in _em_lotes(proposicoes_lista, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_PROPOSICOES, linhas=rows).consume()
                )

            for lote in _em_lotes(temas_lista, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_TEMAS, linhas=rows).consume()
                )

            for lote in _em_lotes(vinculos_tema, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_TRATA_DE, linhas=rows).consume()
                )

            for lote in _em_lotes(vinculos_autor, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_AUTORES, linhas=rows).consume()
                )

        tempo_carga = round(time.perf_counter() - inicio_total, 4)

        anos = [arq.ano for arq in arquivos_por_ano]
        coautoria = recalcular_coautoria(
            driver=self.driver,
            legislatura_id=legislatura_id,
            anos=anos,
            tipos=tipos,
            minimo=minimo_coautorias,
        )

        nos, relacoes = self.contar_grafo()
        tempo_total = round(time.perf_counter() - inicio_total, 4)

        return ResumoCargaM2(
            legislatura_id=legislatura_id,
            anos=anos,
            tipos_filtrados=sorted(tipos) if tipos else ["TODOS"],
            proposicoes_lidas_csv=proposicoes_lidas,
            proposicoes_no_recorte=len(proposicoes_lista),
            temas_distintos_csv=len(temas_lista),
            relacoes_tema_lidas=len(vinculos_tema),
            autores_lidos_csv=autores_lidos,
            autores_deputados_vinculados=len(vinculos_autor),
            autores_ignorados_nao_deputado=autores_nao_deputado,
            autores_ignorados_fora_legislatura=autores_fora_legislatura,
            coautoria=coautoria,
            nos_por_rotulo=nos,
            relacoes_por_tipo=relacoes,
            tempo_carga_segundos=tempo_carga,
            tempo_total_segundos=tempo_total,
        )
