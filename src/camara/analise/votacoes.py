"""Cálculo das relações derivadas de votação: VOTA_COMO e CONTRA_ORIENTACAO."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from neo4j import Driver

VOTOS_VALIDOS_SIMILARIDADE: tuple[str, ...] = ("Sim", "Não", "Abstenção", "Obstrução")
ORIENTACOES_DIRECIONAIS: tuple[str, ...] = ("Sim", "Não", "Obstrução")

CYPHER_LER_VOTOS_VALIDOS = """
MATCH (d:Deputado)-[r:VOTOU]->(v:Votacao)
WHERE r.voto IN $votos_validos
  AND v.ano IN $anos
RETURN d.id AS depId, v.id AS votId, r.voto AS voto
"""

CYPHER_LIMPAR_VOTA_COMO = """
MATCH ()-[r:VOTA_COMO {legislatura: $legislatura}]->()
DELETE r
"""

CYPHER_GRAVAR_VOTA_COMO = """
UNWIND $linhas AS row
MATCH (d1:Deputado {id: row.d1})
MATCH (d2:Deputado {id: row.d2})
MERGE (d1)-[r:VOTA_COMO {legislatura: $legislatura}]->(d2)
SET r.concordancia = row.concordancia,
    r.votacoesEmComum = row.votacoesEmComum,
    r.votosIguais = row.votosIguais,
    r.anos = $anos,
    r.minimoVotacoes = $minimo
"""

CYPHER_LIMPAR_CONTRA_ORIENTACAO = """
MATCH ()-[r:CONTRA_ORIENTACAO {legislatura: $legislatura}]->()
DELETE r
"""

CYPHER_CALCULAR_CONTRA_ORIENTACAO = """
MATCH (d:Deputado)-[v:VOTOU]->(vot:Votacao)<-[o:ORIENTOU]-(p:Partido)
WHERE vot.ano IN $anos
  AND v.partidoNaData = p.sigla
  AND v.voto IN $votos_validos
  AND o.orientacao IN $orientacoes_direcionais
WITH d, p,
     count(DISTINCT vot) AS totalOrientadas,
     count(DISTINCT CASE WHEN v.voto <> o.orientacao THEN vot END) AS contra
WHERE contra >= 1
MERGE (d)-[r:CONTRA_ORIENTACAO {legislatura: $legislatura}]->(p)
SET r.n = contra,
    r.totalOrientadas = totalOrientadas,
    r.taxaDivergencia = round(toFloat(contra) / totalOrientadas, 4),
    r.anos = $anos
RETURN count(r) AS relacoes
"""


@dataclass
class ResumoVotaComo:
    legislatura_id: int
    anos: list[int]
    minimo_votacoes: int
    deputados_com_votos: int
    votacoes_consideradas: int
    pares_criados: int
    tempo_calculo_segundos: float
    tempo_gravacao_segundos: float
    tempo_total_segundos: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResumoContraOrientacao:
    legislatura_id: int
    anos: list[int]
    relacoes_criadas: int
    tempo_segundos: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _em_lotes(itens: list[dict[str, Any]], tamanho: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(itens), tamanho):
        yield itens[i : i + tamanho]


def calcular_pares_vota_como(
    votos: Iterable[tuple[int, str, str]],
    minimo_votacoes: int = 20,
) -> tuple[list[dict[str, Any]], int, int]:
    """Calcula pares (d1 < d2) de concordância de votos usando bitmasks de inteiros Python.

    Retorna (lista_de_pares, num_deputados, num_votacoes).
    """
    vot_to_bit: dict[str, int] = {}
    masks: dict[int, list[int]] = {}
    voto_idx = {v: i for i, v in enumerate(VOTOS_VALIDOS_SIMILARIDADE)}

    for dep_id, vot_id, voto in votos:
        idx = voto_idx.get(voto)
        if idx is None:
            continue
        bit = vot_to_bit.get(vot_id)
        if bit is None:
            bit = 1 << len(vot_to_bit)
            vot_to_bit[vot_id] = bit
        dep_masks = masks.get(dep_id)
        if dep_masks is None:
            dep_masks = [0, 0, 0, 0, 0]  # [Sim, Não, Abstenção, Obstrução, qualquer]
            masks[dep_id] = dep_masks
        dep_masks[idx] |= bit
        dep_masks[4] |= bit

    dep_ids = sorted(masks.keys())
    n_deps = len(dep_ids)
    pares: list[dict[str, Any]] = []

    for i in range(n_deps):
        d1 = dep_ids[i]
        m1 = masks[d1]
        m1_any = m1[4]
        if m1_any.bit_count() < minimo_votacoes:
            continue
        m1_0, m1_1, m1_2, m1_3 = m1[0], m1[1], m1[2], m1[3]

        for j in range(i + 1, n_deps):
            d2 = dep_ids[j]
            m2 = masks[d2]
            comum = (m1_any & m2[4]).bit_count()
            if comum < minimo_votacoes:
                continue
            iguais = (
                (m1_0 & m2[0]).bit_count()
                + (m1_1 & m2[1]).bit_count()
                + (m1_2 & m2[2]).bit_count()
                + (m1_3 & m2[3]).bit_count()
            )
            pares.append(
                {
                    "d1": d1,
                    "d2": d2,
                    "votacoesEmComum": comum,
                    "votosIguais": iguais,
                    "concordancia": round(iguais / comum, 4),
                }
            )

    return pares, n_deps, len(vot_to_bit)


def recalcular_vota_como(
    driver: Driver,
    legislatura_id: int,
    anos: list[int],
    minimo_votacoes: int = 20,
    tamanho_lote: int = 5000,
) -> ResumoVotaComo:
    """Lê votos nominais do Neo4j, calcula VOTA_COMO por bitmask e grava em lotes."""
    inicio_total = time.perf_counter()

    with driver.session() as session:
        registros = [
            (int(rec["depId"]), str(rec["votId"]), str(rec["voto"]))
            for rec in session.run(
                CYPHER_LER_VOTOS_VALIDOS,
                votos_validos=list(VOTOS_VALIDOS_SIMILARIDADE),
                anos=anos,
            )
        ]

    t_calc_ini = time.perf_counter()
    pares, n_deps, n_vots = calcular_pares_vota_como(registros, minimo_votacoes=minimo_votacoes)
    tempo_calc = round(time.perf_counter() - t_calc_ini, 4)

    t_grav_ini = time.perf_counter()
    with driver.session() as session:
        session.execute_write(
            lambda tx: tx.run(CYPHER_LIMPAR_VOTA_COMO, legislatura=legislatura_id).consume()
        )
        for lote in _em_lotes(pares, tamanho_lote):
            session.execute_write(
                lambda tx, rows=lote: tx.run(
                    CYPHER_GRAVAR_VOTA_COMO,
                    linhas=rows,
                    legislatura=legislatura_id,
                    anos=anos,
                    minimo=minimo_votacoes,
                ).consume()
            )
    tempo_grav = round(time.perf_counter() - t_grav_ini, 4)
    tempo_total = round(time.perf_counter() - inicio_total, 4)

    return ResumoVotaComo(
        legislatura_id=legislatura_id,
        anos=anos,
        minimo_votacoes=minimo_votacoes,
        deputados_com_votos=n_deps,
        votacoes_consideradas=n_vots,
        pares_criados=len(pares),
        tempo_calculo_segundos=tempo_calc,
        tempo_gravacao_segundos=tempo_grav,
        tempo_total_segundos=tempo_total,
    )


def recalcular_contra_orientacao(
    driver: Driver,
    legislatura_id: int,
    anos: list[int],
) -> ResumoContraOrientacao:
    """Recalcula (Deputado)-[:CONTRA_ORIENTACAO]->(Partido) de forma idempotente."""
    inicio = time.perf_counter()
    with driver.session() as session:
        session.execute_write(
            lambda tx: tx.run(
                CYPHER_LIMPAR_CONTRA_ORIENTACAO, legislatura=legislatura_id
            ).consume()
        )
        relacoes = session.execute_write(
            lambda tx: int(
                tx.run(
                    CYPHER_CALCULAR_CONTRA_ORIENTACAO,
                    legislatura=legislatura_id,
                    anos=anos,
                    votos_validos=list(VOTOS_VALIDOS_SIMILARIDADE),
                    orientacoes_direcionais=list(ORIENTACOES_DIRECIONAIS),
                ).single()["relacoes"]
            )
        )
    duracao = round(time.perf_counter() - inicio, 4)
    return ResumoContraOrientacao(
        legislatura_id=legislatura_id,
        anos=anos,
        relacoes_criadas=relacoes,
        tempo_segundos=duracao,
    )
