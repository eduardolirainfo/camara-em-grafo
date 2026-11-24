"""Testes da interface visual HTMX e do serviço de ficha completa (M7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from camara.api.app import create_app
from camara.api.ficha_service import FichaDeputadoService, url_bandeira_uf, url_logo_partido
from camara.api.neo4j_repo import Neo4jGraphRepository
from camara.config import Settings


class _FakeUIRepo(Neo4jGraphRepository):
    def __init__(self) -> None:
        self.driver = None  # type: ignore[assignment]
        self.consultas_executadas = 0

    async def resumo_geral_ui(self) -> dict[str, Any]:
        return {
            "nos": {"Deputado": 648, "Partido": 26, "Proposicao": 26796, "Votacao": 1613},
            "relacoes": {"VOTOU": 477114, "VOTA_COMO": 193670},
            "partidos": [{"sigla": "PT", "nome": "PT"}, {"sigla": "PL", "nome": "PL"}],
            "comunidades": [
                {"id": 497, "numero": 1, "total": 279, "principais": ["UNIÃO", "PSD"], "rotulo": "Grupo 1 (UNIÃO, PSD)"}
            ],
        }

    async def buscar_deputados_ui(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "total": 1,
            "itens": [
                {
                    "id": 204501,
                    "nome": "Alencar Santana",
                    "uf": "SP",
                    "urlFoto": "https://img/204501.jpg",
                    "uri": "https://api/204501",
                    "comunidadeVoto": 141,
                    "pagerank": 1.25,
                    "betweenness": 120.5,
                    "taxaDivergencia": 0.05,
                    "totalContra": 3,
                    "totalVotos": 800,
                    "taxaPresenca": 0.5,
                    "numPartidos": 1,
                    "gastoTotalCeap": 1250000.0,
                    "gastoViagens": 310000.0,
                    "gastoDivulgacao": 420000.0,
                    "numFuncionarios": 18,
                    "partidos": ["PT"],
                }
            ],
        }

    async def listar_todos_deputados_simples(self) -> list[dict[str, Any]]:
        return [{"id": 204501, "nome": "Alencar Santana", "uf": "SP", "partidos": ["PT"]}]

    async def buscar_ficha_grafo(self, dep_id: int) -> dict[str, Any] | None:
        if dep_id != 204501:
            return None
        return {
            "id": 204501,
            "nome": "Alencar Santana",
            "uf": "SP",
            "urlFoto": "https://img/204501.jpg",
            "uri": "https://api/204501",
            "email": "dep@camara.leg.br",
            "comunidadeVoto": 141,
            "comunidadeVotoLeiden": 141,
            "gastoTotalCeap": 1250000.0,
            "gastoViagens": 310000.0,
            "gastoDivulgacao": 420000.0,
            "totalProposicoes": 42,
            "totalVotos": 800,
            "partidos": [{"sigla": "PT", "nome": "Partido dos Trabalhadores", "id": 36844}],
        }

    async def buscar_aliancas_deputado(self, dep_id: int) -> dict[str, Any]:
        return {
            "coautores": [],
            "vota_como": [],
            "temas_similares": [],
            "contra_orientacao": [],
        }

    async def listar_proposicoes_de_deputado(
        self, dep_id: int, offset: int = 0, limit: int = 10
    ) -> list[dict[str, Any]]:
        return []


class _FakeFichaService(FichaDeputadoService):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(cache_dir=tmp_path)

    async def obter_detalhes_api(self, dep_id: int) -> dict[str, Any]:
        return {
            "nomeCivil": "ALENCAR SANTANA BRAGA",
            "dataNascimento": "1976-03-14",
            "municipioNascimento": "São Paulo",
            "ufNascimento": "SP",
            "escolaridade": "Superior",
            "ultimoStatus": {"situacao": "Exercício", "gabinete": {"predio": "4", "sala": "301"}},
        }

    async def obter_despesas(self, dep_id: int, ano: int) -> dict[str, Any]:
        return {
            "ano": ano,
            "quantidade_notas": 2,
            "total": 1500.50,
            "categorias": [{"tipo": "DIVULGAÇÃO", "valor": 1500.50, "percentual": 100.0}],
            "top_fornecedores": [{"fornecedor": "Gráfica X", "valor": 1500.50}],
            "itens": [],
        }

    async def obter_funcionarios(self, dep_id: int) -> list[dict[str, Any]]:
        return [
            {
                "nome": "MARIA EXEMPLO",
                "grupo": "Secretário Parlamentar",
                "cargo": "SP15",
                "funcao": "",
                "lotacao": "GAB. 301",
                "dataNomeacao": "2023-02-01",
            }
        ]

    async def obter_atuacao(self, dep_id: int) -> dict[str, Any]:
        return {"frentes": [{"id": 1, "titulo": "Frente X"}], "orgaos": [], "discursos": []}

    async def obter_carreira(self, dep_id: int) -> dict[str, Any]:
        return {"profissoes": [{"titulo": "Advogado"}], "ocupacoes": [], "mandatos_externos": [], "historico": []}


def test_ui_pagina_inicial_e_ficha_completa(tmp_path: Path) -> None:
    cfg = Settings(neo4j_password="dummy")
    app = create_app(
        settings=cfg,
        repo_override=_FakeUIRepo(),
        ficha_service_override=_FakeFichaService(tmp_path),
    )
    client = TestClient(app)

    r_root = client.get("/", follow_redirects=False)
    assert r_root.status_code == 307
    assert r_root.headers["location"] == "/ui"

    r_ui = client.get("/ui")
    assert r_ui.status_code == 200
    assert "Alencar Santana" in r_ui.text
    assert "Bandeira" in r_ui.text

    r_ficha = client.get("/ui/deputado/204501")
    assert r_ficha.status_code == 200
    assert "ALENCAR SANTANA BRAGA" in r_ficha.text

    r_desp = client.get("/ui/deputado/204501/tab/despesas?ano=2025")
    assert r_desp.status_code == 200
    assert "R$ 1.500,50" in r_desp.text

    r_func = client.get("/ui/deputado/204501/tab/funcionarios")
    assert r_func.status_code == 200
    assert "MARIA EXEMPLO" in r_func.text

    assert url_bandeira_uf("SP") is not None
    assert url_logo_partido("PT") == "https://www.camara.leg.br/internet/Deputado/img/partidos/PT.gif"
