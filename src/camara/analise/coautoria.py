"""Cálculo da relação derivada (Deputado)-[:COAUTOR_COM {n}]->(Deputado)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from neo4j import Driver

CYPHER_LIMPAR_COAUTORIA = """
MATCH ()-[r:COAUTOR_COM {legislatura: $legislatura}]->()
DELETE r
"""

CYPHER_CALCULAR_COAUTORIA = """
MATCH (d1:Deputado)-[:AUTOR_DE]->(p:Proposicao)<-[:AUTOR_DE]-(d2:Deputado)
WHERE d1.id < d2.id
  AND p.ano IN $anos
  AND (size($tipos) = 0 OR p.siglaTipo IN $tipos)
WITH d1, d2, count(DISTINCT p) AS n
WHERE n >= $minimo
MERGE (d1)-[r:COAUTOR_COM {legislatura: $legislatura}]->(d2)
SET r.n = n,
    r.anos = $anos,
    r.tipos = $tipos,
    r.minimo = $minimo
RETURN count(r) AS pares
"""


@dataclass
class ResumoCoautoria:
    legislatura_id: int
    anos: list[int]
    tipos: list[str]
    minimo: int
    pares_criados: int
    tempo_segundos: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def recalcular_coautoria(
    driver: Driver,
    legislatura_id: int,
    anos: list[int],
    tipos: set[str] | None = None,
    minimo: int = 1,
) -> ResumoCoautoria:
    """Recalcula relações COAUTOR_COM de forma idempotente para o recorte informado."""
    inicio = time.perf_counter()
    tipos_ordenados = sorted(tipos) if tipos else []

    with driver.session() as session:
        session.execute_write(
            lambda tx: tx.run(CYPHER_LIMPAR_COAUTORIA, legislatura=legislatura_id).consume()
        )
        pares = session.execute_write(
            lambda tx: int(
                tx.run(
                    CYPHER_CALCULAR_COAUTORIA,
                    legislatura=legislatura_id,
                    anos=anos,
                    tipos=tipos_ordenados,
                    minimo=minimo,
                ).single()["pares"]
            )
        )

    duracao = round(time.perf_counter() - inicio, 4)
    return ResumoCoautoria(
        legislatura_id=legislatura_id,
        anos=anos,
        tipos=tipos_ordenados,
        minimo=minimo,
        pares_criados=pares,
        tempo_segundos=duracao,
    )
