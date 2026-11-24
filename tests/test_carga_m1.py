"""Testes da camada de normalização e carga idempotente do M1 no Neo4j."""

from __future__ import annotations

from typing import Any, Self

from fixtures_m1 import (
    DEPUTADOS_PAGINA_1,
    DEPUTADOS_PAGINA_2,
    LEGISLATURA_57,
    PARTIDOS_PAGINA_1,
)

from camara.carga.neo4j_loader import (
    CYPHER_CONTAR_NOS,
    CYPHER_CONTAR_RELACOES,
    Neo4jLoaderM1,
    normalizar_deputado,
    normalizar_legislatura,
    normalizar_partido,
)
from camara.carga.schema import CONSTRAINTS_M1
from camara.coleta.m1 import DadosM1


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def consume(self) -> None:
        return None

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None


class _FakeTx:
    def __init__(self, log: list[tuple[str, dict[str, Any]]]) -> None:
        self.log = log

    def run(self, query: str, **params: Any) -> _FakeResult:
        self.log.append((query.strip(), params))
        return _FakeResult()


class _FakeSession:
    def __init__(self, log: list[tuple[str, dict[str, Any]]]) -> None:
        self.log = log

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def run(self, query: str, **params: Any) -> _FakeResult:
        q = query.strip()
        self.log.append((q, params))
        if q == CYPHER_CONTAR_NOS.strip():
            return _FakeResult(
                [
                    {"rotulo": "Deputado", "total": 3},
                    {"rotulo": "Legislatura", "total": 1},
                    {"rotulo": "Partido", "total": 2},
                    {"rotulo": "UF", "total": 3},
                ]
            )
        if q == CYPHER_CONTAR_RELACOES.strip():
            return _FakeResult(
                [
                    {"tipo": "EXERCE_MANDATO_EM", "total": 3},
                    {"tipo": "FILIADO_A", "total": 3},
                    {"tipo": "REPRESENTA", "total": 3},
                ]
            )
        return _FakeResult()

    def execute_write(self, fn):
        return fn(_FakeTx(self.log))


class _FakeDriver:
    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []

    def session(self) -> _FakeSession:
        return _FakeSession(self.log)


def test_normalizacao_campos_api_para_modelo_grafo() -> None:
    leg = normalizar_legislatura(LEGISLATURA_57["dados"])
    assert leg == {
        "id": 57,
        "inicio": "2023-02-01",
        "fim": "2027-01-31",
        "uri": "https://dadosabertos.camara.leg.br/api/v2/legislaturas/57",
    }

    partido = normalizar_partido(PARTIDOS_PAGINA_1["dados"][0])
    assert partido["sigla"] == "AVANTE"
    assert partido["id"] == 36898

    dep = normalizar_deputado(DEPUTADOS_PAGINA_1["dados"][0], legislatura_padrao=57)
    assert dep["id"] == 220593
    assert dep["uf"] == "MT"
    assert dep["siglaPartido"] == "PL"
    assert dep["idLegislatura"] == 57

    dep_sem_partido = normalizar_deputado(DEPUTADOS_PAGINA_2["dados"][1], legislatura_padrao=57)
    assert dep_sem_partido["siglaPartido"] is None
    assert dep_sem_partido["uf"] == "SP"


def test_carga_cria_constraints_antes_de_merges_e_faz_lotes() -> None:
    driver = _FakeDriver()
    loader = Neo4jLoaderM1(driver, tamanho_lote=2)  # type: ignore[arg-type]

    dados = DadosM1(
        legislatura=LEGISLATURA_57["dados"],
        partidos=PARTIDOS_PAGINA_1["dados"],
        deputados=DEPUTADOS_PAGINA_1["dados"] + DEPUTADOS_PAGINA_2["dados"],
        coletado_em="2026-11-24T12:00:00+00:00",
    )

    resumo = loader.carregar(dados, limpar_antes=False)

    queries = [q for q, _ in driver.log]
    # As 4 constraints precisam vir antes de qualquer MERGE
    for idx, constraint in enumerate(CONSTRAINTS_M1):
        assert queries[idx] == constraint

    # Deputados têm 4 registros -> com tamanho_lote=2, devem gerar 2 lotes UNWIND
    lotes_deputados = [
        params["linhas"]
        for q, params in driver.log
        if "MERGE (d:Deputado" in q
    ]
    assert len(lotes_deputados) == 2
    assert len(lotes_deputados[0]) == 2
    assert len(lotes_deputados[1]) == 2

    # Resumo registra registros brutos vs IDs distintos
    assert resumo.legislatura_id == 57
    assert resumo.registros_partidos_api == 2
    assert resumo.registros_deputados_api == 4
    assert resumo.deputados_ids_distintos_api == 3
    assert resumo.total_nos == 9
    assert resumo.total_relacoes == 9


def test_carga_com_limpar_antes_executa_detach_delete() -> None:
    driver = _FakeDriver()
    loader = Neo4jLoaderM1(driver)  # type: ignore[arg-type]
    dados = DadosM1(
        legislatura=LEGISLATURA_57["dados"],
        partidos=PARTIDOS_PAGINA_1["dados"],
        deputados=DEPUTADOS_PAGINA_1["dados"],
        coletado_em="2026-11-24T12:00:00+00:00",
    )

    loader.carregar(dados, limpar_antes=True)
    assert driver.log[0][0] == "MATCH (n) DETACH DELETE n"
