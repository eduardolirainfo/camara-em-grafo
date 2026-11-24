"""Testes da camada de normalização e carga idempotente do M2 no Neo4j."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from camara.carga.neo4j_loader import CYPHER_CONTAR_NOS, CYPHER_CONTAR_RELACOES
from camara.carga.neo4j_loader_m2 import (
    CYPHER_DEPUTADOS_EXISTENTES,
    Neo4jLoaderM2,
    normalizar_autor,
    normalizar_proposicao,
    normalizar_tema_vinculo,
)
from camara.carga.schema import CONSTRAINTS_M2, INDEXES_M2
from camara.coleta.arquivos import ArquivosAnoM2


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
        q = query.strip()
        self.log.append((q, params))
        if "RETURN count(r) AS pares" in q:
            return _FakeResult([{"pares": 1}])
        return _FakeResult()


class _FakeSession:
    def __init__(self, log: list[tuple[str, dict[str, Any]]], deputados_ids: list[int]) -> None:
        self.log = log
        self.deputados_ids = deputados_ids

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def run(self, query: str, **params: Any) -> _FakeResult:
        q = query.strip()
        self.log.append((q, params))
        if q == CYPHER_DEPUTADOS_EXISTENTES.strip():
            return _FakeResult([{"id": i} for i in self.deputados_ids])
        if q == CYPHER_CONTAR_NOS.strip():
            return _FakeResult(
                [
                    {"rotulo": "Deputado", "total": len(self.deputados_ids)},
                    {"rotulo": "Proposicao", "total": 1},
                    {"rotulo": "Tema", "total": 1},
                ]
            )
        if q == CYPHER_CONTAR_RELACOES.strip():
            return _FakeResult(
                [
                    {"tipo": "AUTOR_DE", "total": 2},
                    {"tipo": "COAUTOR_COM", "total": 1},
                    {"tipo": "TRATA_DE", "total": 1},
                ]
            )
        return _FakeResult()

    def execute_write(self, fn):
        return fn(_FakeTx(self.log))


class _FakeDriver:
    def __init__(self, deputados_ids: list[int]) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.deputados_ids = deputados_ids

    def session(self) -> _FakeSession:
        return _FakeSession(self.log, self.deputados_ids)


def test_normalizacao_registros_m2() -> None:
    prop = normalizar_proposicao(
        {
            "id": "281460",
            "siglaTipo": "pl",
            "numero": "1051",
            "ano": "2026",
            "ementa": "  Altera lei  ",
            "dataApresentacao": "2026-01-10T10:00:00",
            "uri": "https://dadosabertos.camara.leg.br/api/v2/proposicoes/281460",
        }
    )
    assert prop is not None
    assert prop["id"] == 281460
    assert prop["siglaTipo"] == "PL"
    assert prop["ementa"] == "Altera lei"

    tema = normalizar_tema_vinculo(
        {
            "uriProposicao": "https://dadosabertos.camara.leg.br/api/v2/proposicoes/281460",
            "codTema": "57",
            "tema": "Defesa e Segurança",
        }
    )
    assert tema == {"idProposicao": 281460, "codTema": 57, "nomeTema": "Defesa e Segurança"}

    aut = normalizar_autor(
        {
            "idProposicao": "281460",
            "idDeputadoAutor": "220593",
            "ordemAssinatura": "1",
            "proponente": "1",
        }
    )
    assert aut == {
        "idProposicao": 281460,
        "idDeputado": 220593,
        "ordem": 1,
        "proponente": True,
    }


def test_carga_m2_filtra_tipos_cria_schema_e_calcula_coautoria(tmp_path: Path) -> None:
    p_prop = tmp_path / "proposicoes-2025.csv"
    p_aut = tmp_path / "proposicoesAutores-2025.csv"
    p_tem = tmp_path / "proposicoesTemas-2025.csv"

    p_prop.write_text(
        '"id";"uri";"siglaTipo";"numero";"ano";"ementa";"dataApresentacao"\n'
        '"1001";"https://dadosabertos.camara.leg.br/api/v2/proposicoes/1001";"PL";"10";"2025";"Saúde pública";"2025-02-10"\n'
        '"1002";"https://dadosabertos.camara.leg.br/api/v2/proposicoes/1002";"REQ";"50";"2025";"Requerimento";"2025-02-11"\n',
        encoding="utf-8",
    )
    p_aut.write_text(
        '"idProposicao";"idDeputadoAutor";"tipoAutor";"ordemAssinatura";"proponente"\n'
        '"1001";"220593";"Deputado(a)";"1";"1"\n'
        '"1001";"100001";"Deputado(a)";"2";"0"\n'
        '"1001";"999999";"Deputado(a)";"3";"0"\n'
        '"1001";"";"Poder Executivo";"4";"0"\n'
        '"1002";"220593";"Deputado(a)";"1";"1"\n',
        encoding="utf-8",
    )
    p_tem.write_text(
        '"uriProposicao";"codTema";"tema"\n'
        '"https://dadosabertos.camara.leg.br/api/v2/proposicoes/1001";"56";"Saúde"\n'
        '"https://dadosabertos.camara.leg.br/api/v2/proposicoes/1002";"34";"Administração"\n',
        encoding="utf-8",
    )

    driver = _FakeDriver(deputados_ids=[220593, 100001])
    loader = Neo4jLoaderM2(driver, tamanho_lote=10)  # type: ignore[arg-type]

    resumo = loader.carregar(
        arquivos_por_ano=[ArquivosAnoM2(2025, p_prop, p_aut, p_tem)],
        legislatura_id=57,
        tipos={"PL", "PEC"},
    )

    queries = [q for q, _ in driver.log]
    expected_schema = (*CONSTRAINTS_M2, *INDEXES_M2)
    for idx, stmt in enumerate(expected_schema):
        assert queries[idx] == stmt

    assert resumo.proposicoes_lidas_csv == 2
    assert resumo.proposicoes_no_recorte == 1  # REQ filtrado fora
    assert resumo.temas_distintos_csv == 1
    assert resumo.autores_deputados_vinculados == 2
    assert resumo.autores_ignorados_nao_deputado == 1
    assert resumo.autores_ignorados_fora_legislatura == 1
    assert resumo.coautoria.pares_criados == 1
