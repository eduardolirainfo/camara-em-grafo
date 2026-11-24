"""Carga idempotente do Marco 3 (M3): votações, votos, orientações, VOTA_COMO e CONTRA_ORIENTACAO."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from neo4j import Driver

from camara.analise.votacoes import (
    ResumoContraOrientacao,
    ResumoVotaComo,
    recalcular_contra_orientacao,
    recalcular_vota_como,
)
from camara.carga.neo4j_loader import CYPHER_CONTAR_NOS, CYPHER_CONTAR_RELACOES
from camara.carga.schema import CONSTRAINTS_M3
from camara.coleta.arquivos import ArquivosAnoM3, ler_csv_camara

CYPHER_MERGE_VOTACOES = """
UNWIND $linhas AS row
MERGE (v:Votacao {id: row.id})
SET v.data = row.data,
    v.ano = row.ano,
    v.siglaOrgao = row.siglaOrgao,
    v.descricao = row.descricao,
    v.aprovacao = row.aprovacao,
    v.votosSim = row.votosSim,
    v.votosNao = row.votosNao,
    v.votosOutros = row.votosOutros,
    v.uri = row.uri
"""

CYPHER_MERGE_SOBRE = """
UNWIND $linhas AS row
MATCH (v:Votacao {id: row.idVotacao})
MATCH (p:Proposicao {id: row.idProposicao})
MERGE (v)-[:SOBRE]->(p)
"""

CYPHER_MERGE_VOTOS = """
UNWIND $linhas AS row
MATCH (d:Deputado {id: row.idDeputado})
MATCH (v:Votacao {id: row.idVotacao})
MERGE (d)-[r:VOTOU]->(v)
SET r.voto = row.voto,
    r.partidoNaData = row.partidoNaData,
    r.dataHoraVoto = row.dataHoraVoto
"""

CYPHER_MERGE_ORIENTACOES = """
UNWIND $linhas AS row
MATCH (p:Partido {sigla: row.siglaPartido})
MATCH (v:Votacao {id: row.idVotacao})
MERGE (p)-[r:ORIENTOU]->(v)
SET r.orientacao = row.orientacao,
    r.siglaBancada = row.siglaBancada
"""

CYPHER_DEPUTADOS_EXISTENTES = "MATCH (d:Deputado) RETURN d.id AS id"
CYPHER_PARTIDOS_EXISTENTES = "MATCH (p:Partido) RETURN p.sigla AS sigla"
CYPHER_PROPOSICOES_EXISTENTES = "MATCH (p:Proposicao) RETURN p.id AS id"


@dataclass
class ResumoCargaM3:
    legislatura_id: int
    anos: list[int]
    votacoes_lidas_csv: int
    votacoes_nominais_carregadas: int
    votos_lidos_csv: int
    votos_vinculados: int
    votos_ignorados_fora_legislatura: int
    orientacoes_lidas_csv: int
    orientacoes_vinculadas: int
    orientacoes_ignoradas_vazias: int
    orientacoes_ignoradas_nao_partido: int
    objetos_vinculados_sobre: int
    vota_como: ResumoVotaComo
    contra_orientacao: ResumoContraOrientacao
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


def _to_bool_aprovacao(val: Any) -> bool | None:
    s = str(val or "").strip()
    if s == "1":
        return True
    if s == "0":
        return False
    return None


def expandir_sigla_bancada(
    sigla_bancada: str,
    partidos_por_upper: dict[str, str],
) -> list[str]:
    """Resolve uma siglaBancada para siglas oficiais de Partido (suporta Federações 'Fdr X-Y-Z')."""
    bruta = sigla_bancada.strip()
    if not bruta:
        return []

    exata = partidos_por_upper.get(bruta.upper())
    if exata:
        return [exata]

    # Federações partidárias na Câmara vêm como "Fdr PT-PCdoB-PV", "Fdr PSDB-Cidadania", etc.
    if bruta.lower().startswith("fdr "):
        membros = [m.strip() for m in bruta[4:].split("-") if m.strip()]
        resolvidos = [
            partidos_por_upper[m.upper()]
            for m in membros
            if m.upper() in partidos_por_upper
        ]
        return resolvidos

    return []


def normalizar_votacao(raw: dict[str, Any], ano_arquivo: int) -> dict[str, Any] | None:
    vot_id = (raw.get("id") or "").strip()
    if not vot_id:
        return None
    data = (raw.get("data") or "").strip() or None
    ano = ano_arquivo
    if data and len(data) >= 4 and data[:4].isdigit():
        ano = int(data[:4])
    return {
        "id": vot_id,
        "data": data,
        "ano": ano,
        "siglaOrgao": (raw.get("siglaOrgao") or "").strip() or None,
        "descricao": (raw.get("descricao") or "").strip(),
        "aprovacao": _to_bool_aprovacao(raw.get("aprovacao")),
        "votosSim": _to_int(raw.get("votosSim")) or 0,
        "votosNao": _to_int(raw.get("votosNao")) or 0,
        "votosOutros": _to_int(raw.get("votosOutros")) or 0,
        "uri": (raw.get("uri") or "").strip() or None,
    }


def normalizar_voto(raw: dict[str, Any]) -> dict[str, Any] | None:
    vot_id = (raw.get("idVotacao") or "").strip()
    dep_id = _to_int(raw.get("deputado_id"))
    voto = (raw.get("voto") or "").strip()
    if not vot_id or dep_id is None or not voto:
        return None
    return {
        "idVotacao": vot_id,
        "idDeputado": dep_id,
        "voto": voto,
        "partidoNaData": (raw.get("deputado_siglaPartido") or "").strip() or None,
        "dataHoraVoto": (raw.get("dataHoraVoto") or "").strip() or None,
    }


class Neo4jLoaderM3:
    """Cria constraint de Votacao e executa carga idempotente do M3 no Neo4j."""

    def __init__(self, driver: Driver, tamanho_lote: int = 5000) -> None:
        self.driver = driver
        self.tamanho_lote = tamanho_lote

    def criar_constraints(self) -> None:
        with self.driver.session() as session:
            for stmt in CONSTRAINTS_M3:
                session.run(stmt).consume()

    def limpar_m3(self) -> None:
        """Remove apenas nós e relações introduzidos pelo M3 (preserva M1 e M2)."""
        with self.driver.session() as session:
            session.run("MATCH ()-[r:VOTA_COMO]->() DELETE r").consume()
            session.run("MATCH ()-[r:CONTRA_ORIENTACAO]->() DELETE r").consume()
            session.run("MATCH (v:Votacao) DETACH DELETE v").consume()

    def _carregar_chaves_existentes(self) -> tuple[set[int], dict[str, str], set[int]]:
        with self.driver.session() as session:
            deps = {int(rec["id"]) for rec in session.run(CYPHER_DEPUTADOS_EXISTENTES)}
            partidos = {
                str(rec["sigla"]).upper(): str(rec["sigla"])
                for rec in session.run(CYPHER_PARTIDOS_EXISTENTES)
            }
            props = {int(rec["id"]) for rec in session.run(CYPHER_PROPOSICOES_EXISTENTES)}
        return deps, partidos, props

    def contar_grafo(self) -> tuple[dict[str, int], dict[str, int]]:
        with self.driver.session() as session:
            nos = {rec["rotulo"]: int(rec["total"]) for rec in session.run(CYPHER_CONTAR_NOS)}
            relacoes = {
                rec["tipo"]: int(rec["total"]) for rec in session.run(CYPHER_CONTAR_RELACOES)
            }
        return nos, relacoes

    def carregar(
        self,
        arquivos_por_ano: list[ArquivosAnoM3],
        legislatura_id: int,
        minimo_votacoes: int = 20,
        limpar_antes: bool = False,
    ) -> ResumoCargaM3:
        inicio_total = time.perf_counter()

        if limpar_antes:
            self.limpar_m3()

        self.criar_constraints()
        deps_existentes, partidos_upper, props_existentes = self._carregar_chaves_existentes()

        # 1. Ler votos nominais primeiro para saber exatamente quais votações têm votos nominais
        votos_lidos = 0
        votos_fora_leg = 0
        votos_validos: list[dict[str, Any]] = []
        ids_votacoes_nominais: set[str] = set()

        for arq in arquivos_por_ano:
            for row in ler_csv_camara(arq.votos):
                votos_lidos += 1
                norm = normalizar_voto(row)
                if norm is None:
                    continue
                if deps_existentes and norm["idDeputado"] not in deps_existentes:
                    votos_fora_leg += 1
                    continue
                # Normaliza partidoNaData para a grafia canônica no grafo quando existir
                if norm["partidoNaData"] and norm["partidoNaData"].upper() in partidos_upper:
                    norm["partidoNaData"] = partidos_upper[norm["partidoNaData"].upper()]
                ids_votacoes_nominais.add(norm["idVotacao"])
                votos_validos.append(norm)

        # 2. Ler metadados apenas das votações com votos nominais
        votacoes_lidas = 0
        votacoes_por_id: dict[str, dict[str, Any]] = {}
        for arq in arquivos_por_ano:
            for row in ler_csv_camara(arq.votacoes):
                votacoes_lidas += 1
                norm = normalizar_votacao(row, ano_arquivo=arq.ano)
                if norm is None or norm["id"] not in ids_votacoes_nominais:
                    continue
                votacoes_por_id[norm["id"]] = norm

        # Garante stub mínimo se algum voto referenciar votação ausente no CSV de metadados
        for vid in ids_votacoes_nominais:
            if vid not in votacoes_por_id:
                votacoes_por_id[vid] = {
                    "id": vid,
                    "data": None,
                    "ano": arquivos_por_ano[-1].ano if arquivos_por_ano else None,
                    "siglaOrgao": None,
                    "descricao": "",
                    "aprovacao": None,
                    "votosSim": 0,
                    "votosNao": 0,
                    "votosOutros": 0,
                    "uri": f"https://dadosabertos.camara.leg.br/api/v2/votacoes/{vid}",
                }

        # 3. Ler orientações de partidos e federações para as votações nominais
        orientacoes_lidas = 0
        orientacoes_vazias = 0
        orientacoes_nao_partido = 0
        orientacoes_vinculos: list[dict[str, Any]] = []

        for arq in arquivos_por_ano:
            for row in ler_csv_camara(arq.orientacoes):
                orientacoes_lidas += 1
                vid = (row.get("idVotacao") or "").strip()
                if vid not in ids_votacoes_nominais:
                    continue
                orientacao = (row.get("orientacao") or "").strip()
                if not orientacao:
                    orientacoes_vazias += 1
                    continue
                sigla_bancada = (row.get("siglaBancada") or "").strip()
                siglas = expandir_sigla_bancada(sigla_bancada, partidos_upper)
                if not siglas:
                    orientacoes_nao_partido += 1
                    continue
                for sigla in siglas:
                    orientacoes_vinculos.append(
                        {
                            "idVotacao": vid,
                            "siglaPartido": sigla,
                            "orientacao": orientacao,
                            "siglaBancada": sigla_bancada,
                        }
                    )

        # 4. Ler objetos votados para vincular (Votacao)-[:SOBRE]->(Proposicao)
        objetos_vistos: set[tuple[str, int]] = set()
        objetos_vinculos: list[dict[str, Any]] = []
        for arq in arquivos_por_ano:
            for row in ler_csv_camara(arq.objetos):
                vid = (row.get("idVotacao") or "").strip()
                pid = _to_int(row.get("proposicao_id"))
                if (
                    vid in ids_votacoes_nominais
                    and pid is not None
                    and (not props_existentes or pid in props_existentes)
                    and (vid, pid) not in objetos_vistos
                ):
                    objetos_vistos.add((vid, pid))
                    objetos_vinculos.append({"idVotacao": vid, "idProposicao": pid})

        # 5. Gravação em lotes no Neo4j
        votacoes_lista = list(votacoes_por_id.values())
        with self.driver.session() as session:
            for lote in _em_lotes(votacoes_lista, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_VOTACOES, linhas=rows).consume()
                )
            for lote in _em_lotes(objetos_vinculos, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_SOBRE, linhas=rows).consume()
                )
            for lote in _em_lotes(orientacoes_vinculos, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_ORIENTACOES, linhas=rows).consume()
                )
            for lote in _em_lotes(votos_validos, self.tamanho_lote):
                session.execute_write(
                    lambda tx, rows=lote: tx.run(CYPHER_MERGE_VOTOS, linhas=rows).consume()
                )

        tempo_carga = round(time.perf_counter() - inicio_total, 4)

        # 6. Relações derivadas: VOTA_COMO e CONTRA_ORIENTACAO
        anos = [arq.ano for arq in arquivos_por_ano]
        vota_como = recalcular_vota_como(
            driver=self.driver,
            legislatura_id=legislatura_id,
            anos=anos,
            minimo_votacoes=minimo_votacoes,
            tamanho_lote=self.tamanho_lote,
        )
        contra_orientacao = recalcular_contra_orientacao(
            driver=self.driver,
            legislatura_id=legislatura_id,
            anos=anos,
        )

        nos, relacoes = self.contar_grafo()
        tempo_total = round(time.perf_counter() - inicio_total, 4)

        return ResumoCargaM3(
            legislatura_id=legislatura_id,
            anos=anos,
            votacoes_lidas_csv=votacoes_lidas,
            votacoes_nominais_carregadas=len(votacoes_lista),
            votos_lidos_csv=votos_lidos,
            votos_vinculados=len(votos_validos),
            votos_ignorados_fora_legislatura=votos_fora_leg,
            orientacoes_lidas_csv=orientacoes_lidas,
            orientacoes_vinculadas=len(orientacoes_vinculos),
            orientacoes_ignoradas_vazias=orientacoes_vazias,
            orientacoes_ignoradas_nao_partido=orientacoes_nao_partido,
            objetos_vinculados_sobre=len(objetos_vinculos),
            vota_como=vota_como,
            contra_orientacao=contra_orientacao,
            nos_por_rotulo=nos,
            relacoes_por_tipo=relacoes,
            tempo_carga_segundos=tempo_carga,
            tempo_total_segundos=tempo_total,
        )
