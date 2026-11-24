"""Testes da carga M3, expansão de federações e cálculo por bitmask de VOTA_COMO."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from camara.analise.votacoes import CYPHER_LER_VOTOS_VALIDOS, calcular_pares_vota_como
from camara.carga.neo4j_loader import CYPHER_CONTAR_NOS, CYPHER_CONTAR_RELACOES
from camara.carga.neo4j_loader_m3 import (
    CYPHER_DEPUTADOS_EXISTENTES,
    CYPHER_PARTIDOS_EXISTENTES,
    CYPHER_PROPOSICOES_EXISTENTES,
    Neo4jLoaderM3,
    expandir_sigla_bancada,
    normalizar_votacao,
    normalizar_voto,
)
from camara.carga.schema import CONSTRAINTS_M3
from camara.coleta.arquivos import ArquivosAnoM3


def test_expandir_sigla_bancada_partido_e_federacao() -> None:
    partidos = {"PT": "PT", "PCDOB": "PCdoB", "PV": "PV", "PL": "PL", "PSDB": "PSDB"}
    assert expandir_sigla_bancada("PL", partidos) == ["PL"]
    assert expandir_sigla_bancada("Fdr PT-PCdoB-PV", partidos) == ["PT", "PCdoB", "PV"]
    assert expandir_sigla_bancada("Governo", partidos) == []
    assert expandir_sigla_bancada("Bl UniPpPsd...", partidos) == []


def test_calcular_pares_vota_como_bitmask_exato() -> None:
    # 3 votações válidas + 1 Artigo 17 (ignorado)
    votos = [
        (1, "v1", "Sim"),
        (2, "v1", "Sim"),
        (3, "v1", "Não"),
        (1, "v2", "Não"),
        (2, "v2", "Não"),
        (3, "v2", "Sim"),
        (1, "v3", "Sim"),
        (2, "v3", "Não"),
        (3, "v3", "Sim"),
        (1, "v4", "Artigo 17"),
        (2, "v4", "Sim"),
    ]
    pares, n_deps, n_vots = calcular_pares_vota_como(votos, minimo_votacoes=2)
    assert n_deps == 3
    assert n_vots == 4  # v4 entrou pelo voto válido de dep 2, mas não entra em comum para dep 1
    por_par = {(p["d1"], p["d2"]): p for p in pares}
    assert por_par[(1, 2)]["votacoesEmComum"] == 3
    assert por_par[(1, 2)]["votosIguais"] == 2
    assert por_par[(1, 2)]["concordancia"] == 0.6667
    assert por_par[(1, 3)]["concordancia"] == 0.3333
    assert por_par[(2, 3)]["concordancia"] == 0.0


def test_normalizacao_votacao_e_voto() -> None:
    vot = normalizar_votacao(
        {
            "id": "1228863-102",
            "uri": "https://dadosabertos.camara.leg.br/api/v2/votacoes/1228863-102",
            "data": "2026-06-10",
            "siglaOrgao": "PLEN",
            "descricao": "Aprovado",
            "aprovacao": "1",
            "votosSim": "300",
            "votosNao": "100",
            "votosOutros": "2",
        },
        ano_arquivo=2026,
    )
    assert vot is not None
    assert vot["id"] == "1228863-102"
    assert vot["ano"] == 2026
    assert vot["aprovacao"] is True
    assert vot["votosSim"] == 300

    voto = normalizar_voto(
        {
            "idVotacao": "1228863-102",
            "deputado_id": "204501",
            "voto": "Não",
            "deputado_siglaPartido": "PT",
            "dataHoraVoto": "2026-06-10T14:46:15",
        }
    )
    assert voto is not None
    assert voto["idDeputado"] == 204501
    assert voto["partidoNaData"] == "PT"


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
        if "RETURN count(r) AS relacoes" in q:
            return _FakeResult([{"relacoes": 1}])
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
        if q == CYPHER_DEPUTADOS_EXISTENTES.strip():
            return _FakeResult([{"id": 1}, {"id": 2}])
        if q == CYPHER_PARTIDOS_EXISTENTES.strip():
            return _FakeResult([{"sigla": "PT"}, {"sigla": "PCdoB"}, {"sigla": "PL"}])
        if q == CYPHER_PROPOSICOES_EXISTENTES.strip():
            return _FakeResult([{"id": 1001}])
        if q == CYPHER_LER_VOTOS_VALIDOS.strip():
            return _FakeResult(
                [
                    {"depId": 1, "votId": "v1", "voto": "Sim"},
                    {"depId": 2, "votId": "v1", "voto": "Não"},
                ]
            )
        if q == CYPHER_CONTAR_NOS.strip():
            return _FakeResult([{"rotulo": "Votacao", "total": 1}])
        if q == CYPHER_CONTAR_RELACOES.strip():
            return _FakeResult([{"tipo": "VOTOU", "total": 2}])
        return _FakeResult()

    def execute_write(self, fn):
        return fn(_FakeTx(self.log))


class _FakeDriver:
    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []

    def session(self) -> _FakeSession:
        return _FakeSession(self.log)


def test_loader_m3_filtra_nominais_e_expande_federacoes(tmp_path: Path) -> None:
    p_vot = tmp_path / "votacoes-2025.csv"
    p_votos = tmp_path / "votacoesVotos-2025.csv"
    p_ori = tmp_path / "votacoesOrientacoes-2025.csv"
    p_obj = tmp_path / "votacoesObjetos-2025.csv"

    p_vot.write_text(
        '"id";"uri";"data";"siglaOrgao";"descricao";"aprovacao";"votosSim";"votosNao";"votosOutros"\n'
        '"v1";"https://example/v1";"2025-05-01";"PLEN";"Aprovada";"1";"200";"100";"0"\n'
        '"v_simbolica";"https://example/v2";"2025-05-01";"CCP";"Simbólica";"1";"0";"0";"0"\n',
        encoding="utf-8",
    )
    p_votos.write_text(
        '"idVotacao";"dataHoraVoto";"voto";"deputado_id";"deputado_siglaPartido"\n'
        '"v1";"2025-05-01T15:00:00";"Sim";"1";"PT"\n'
        '"v1";"2025-05-01T15:00:01";"Não";"2";"PL"\n'
        '"v1";"2025-05-01T15:00:02";"Sim";"999";"PL"\n',
        encoding="utf-8",
    )
    p_ori.write_text(
        '"idVotacao";"siglaBancada";"orientacao"\n'
        '"v1";"Fdr PT-PCdoB";"Sim"\n'
        '"v1";"PL";""\n'
        '"v1";"Governo";"Sim"\n',
        encoding="utf-8",
    )
    p_obj.write_text(
        '"idVotacao";"proposicao_id"\n'
        '"v1";"1001"\n'
        '"v1";"9999"\n',
        encoding="utf-8",
    )

    driver = _FakeDriver()
    loader = Neo4jLoaderM3(driver, tamanho_lote=10)  # type: ignore[arg-type]
    resumo = loader.carregar(
        arquivos_por_ano=[ArquivosAnoM3(2025, p_vot, p_votos, p_ori, p_obj)],
        legislatura_id=57,
        minimo_votacoes=1,
    )

    assert driver.log[0][0] == CONSTRAINTS_M3[0]
    assert resumo.votacoes_lidas_csv == 2
    assert resumo.votacoes_nominais_carregadas == 1  # v_simbolica filtrada fora
    assert resumo.votos_vinculados == 2
    assert resumo.votos_ignorados_fora_legislatura == 1
    assert resumo.orientacoes_vinculadas == 2  # PT + PCdoB expandidos de Fdr PT-PCdoB
    assert resumo.orientacoes_ignoradas_vazias == 1
    assert resumo.orientacoes_ignoradas_nao_partido == 1
    assert resumo.objetos_vinculados_sobre == 1
    assert resumo.vota_como.pares_criados == 1
    assert resumo.contra_orientacao.relacoes_criadas == 1
