"""Testes unitários do módulo de análises GDS do Marco 5 (M5)."""

from __future__ import annotations

from typing import Any, Self

from camara.analise.gds_m5 import (
    PROJECTION_COAUTORIA,
    PROJECTION_TEMAS,
    PROJECTION_VOTACAO,
    executar_analises_m5,
)


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def consume(self) -> None:
        return None

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None


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
        if "gds.graph.project" in q:
            return _FakeResult([{"nos": 100, "relacoes": 500}])
        if "gds.louvain.write" in q:
            return _FakeResult(
                [
                    {
                        "communityCount": 3,
                        "modularity": 0.42,
                        "computeMillis": 12,
                        "writeMillis": 8,
                    }
                ]
            )
        if "gds.leiden.write" in q:
            return _FakeResult(
                [
                    {
                        "communityCount": 3,
                        "modularity": 0.45,
                        "computeMillis": 15,
                        "writeMillis": 7,
                    }
                ]
            )
        if "gds.pageRank.write" in q:
            return _FakeResult(
                [
                    {
                        "ranIterations": 20,
                        "didConverge": True,
                        "computeMillis": 10,
                        "writeMillis": 5,
                    }
                ]
            )
        if "gds.betweenness.write" in q:
            return _FakeResult([{"computeMillis": 25, "writeMillis": 6}])
        if "gds.nodeSimilarity.write" in q:
            return _FakeResult(
                [
                    {
                        "nodesCompared": 100,
                        "relationshipsWritten": 400,
                        "computeMillis": 18,
                        "writeMillis": 9,
                    }
                ]
            )
        return _FakeResult([])


class _FakeDriver:
    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []

    def session(self) -> _FakeSession:
        return _FakeSession(self.log)


def test_executar_analises_m5_projeta_e_limpa_todas_as_projecoes() -> None:
    driver = _FakeDriver()
    resumo = executar_analises_m5(
        driver=driver,  # type: ignore[arg-type]
        legislatura_id=57,
        min_votacoes_comum=50,
        min_concordancia=0.70,
        min_coautorias=5,
    )

    assert resumo.comunidades_louvain["community_count"] == 3
    assert resumo.comunidades_leiden["modularity"] == 0.45
    assert resumo.centralidade_coautoria["pagerank_convergiu"] is True
    assert resumo.similaridade_temas["relationships_written"] == 400

    # Verifica que todas as 3 projeções GDS foram removidas ao final (sem vazar memória)
    drops = [
        params["nome"]
        for q, params in driver.log
        if "gds.graph.drop" in q
    ]
    assert drops.count(PROJECTION_VOTACAO) == 2
    assert drops.count(PROJECTION_COAUTORIA) == 2
    assert drops.count(PROJECTION_TEMAS) == 2
