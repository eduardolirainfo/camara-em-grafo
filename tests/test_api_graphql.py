"""Testes da API GraphQL (FastAPI + Strawberry): DataLoader, paginação, limites e segurança."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from camara.api.app import create_app
from camara.api.neo4j_repo import Neo4jGraphRepository
from camara.config import Settings


class _FakeGraphRepo(Neo4jGraphRepository):
    """Repositório fake em memória que conta chamadas em lote para provar ausência de N+1."""

    def __init__(self, falhar_em_deputado: bool = False) -> None:
        self.driver = None  # type: ignore[assignment]
        self.consultas_executadas = 0
        self.chamadas_batch_partidos = 0
        self.chamadas_batch_temas = 0
        self.falhar_em_deputado = falhar_em_deputado

        self._deputados = [
            {
                "id": 1,
                "nome": "Ana Silva",
                "uf": "SP",
                "urlFoto": "https://img/1.jpg",
                "uri": "https://api/deputados/1",
            },
            {
                "id": 2,
                "nome": "Bruno Costa",
                "uf": "RJ",
                "urlFoto": "https://img/2.jpg",
                "uri": "https://api/deputados/2",
            },
            {
                "id": 3,
                "nome": "Carla Souza",
                "uf": "MG",
                "urlFoto": "https://img/3.jpg",
                "uri": "https://api/deputados/3",
            },
        ]
        self._partidos = {
            1: [{"sigla": "PT", "nome": "Partido dos Trabalhadores", "id": 10, "uri": "u1"}],
            2: [{"sigla": "PL", "nome": "Partido Liberal", "id": 20, "uri": "u2"}],
            3: [{"sigla": "PSD", "nome": "PSD", "id": 30, "uri": "u3"}],
        }

    async def verificar_saude(self) -> bool:
        return True

    async def buscar_deputado(self, dep_id: int) -> dict[str, Any] | None:
        if self.falhar_em_deputado:
            raise RuntimeError("Neo4j Cypher internal error: MATCH (d:Deputado) SECRET")
        return next((d for d in self._deputados if d["id"] == dep_id), None)

    async def listar_deputados(
        self,
        uf: str | None = None,
        partido: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        res = self._deputados
        if uf:
            res = [d for d in res if d["uf"] == uf.upper()]
        return res[offset : offset + limit]

    async def listar_proposicoes(
        self,
        tema: str | None = None,
        texto: str | None = None,
        ano: int | None = None,
        tipo: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        props = [
            {
                "id": 100,
                "siglaTipo": "PL",
                "numero": 10,
                "ano": 2025,
                "ementa": "Política de inteligência artificial",
                "dataApresentacao": "2025-03-01",
                "uri": "https://api/proposicoes/100",
            }
        ]
        return props[offset : offset + limit]

    async def listar_proposicoes_de_deputado(
        self, dep_id: int, offset: int = 0, limit: int = 10
    ) -> list[dict[str, Any]]:
        return await self.listar_proposicoes(offset=offset, limit=limit)

    async def buscar_votacao(self, vot_id: str) -> dict[str, Any] | None:
        if vot_id != "v-1":
            return None
        return {
            "id": "v-1",
            "data": "2025-06-10",
            "ano": 2025,
            "siglaOrgao": "PLEN",
            "descricao": "Aprovado",
            "aprovacao": True,
            "votosSim": 300,
            "votosNao": 120,
            "votosOutros": 2,
            "uri": "https://api/votacoes/v-1",
        }

    async def menor_caminho_coautoria(
        self, dep_a: int, dep_b: int, max_saltos: int = 6
    ) -> list[dict[str, Any]]:
        return [self._deputados[0], self._deputados[1]]

    async def batch_partidos_por_deputados(
        self, dep_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        self.chamadas_batch_partidos += 1
        return {i: self._partidos.get(i, []) for i in dep_ids}

    async def batch_coautores_por_deputados(
        self, chaves: list[tuple[int, int]]
    ) -> dict[tuple[int, int], list[dict[str, Any]]]:
        return {
            k: [{"deputado": self._deputados[1], "proposicoesEmComum": 12}] for k in chaves
        }

    async def batch_vota_como_por_deputados(
        self, chaves: list[tuple[int, int, int]]
    ) -> dict[tuple[int, int, int], list[dict[str, Any]]]:
        return {
            k: [
                {
                    "deputado": self._deputados[2],
                    "concordancia": 0.95,
                    "votacoesEmComum": 120,
                }
            ]
            for k in chaves
        }

    async def batch_contra_orientacao_por_deputados(
        self, dep_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        return {
            i: [
                {
                    "partido": self._partidos[1][0],
                    "contra": 5,
                    "totalOrientadas": 50,
                    "taxaDivergencia": 0.1,
                }
            ]
            for i in dep_ids
        }

    async def batch_temas_por_proposicoes(
        self, prop_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        self.chamadas_batch_temas += 1
        return {i: [{"cod": 56, "nome": "Saúde"}] for i in prop_ids}

    async def batch_autores_por_proposicoes(
        self, chaves: list[tuple[int, int]]
    ) -> dict[tuple[int, int], list[dict[str, Any]]]:
        return {k: [self._deputados[0]] for k in chaves}

    async def batch_proposicoes_por_votacoes(
        self, vot_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        return {vid: [] for vid in vot_ids}

    async def batch_orientacoes_por_votacoes(
        self, vot_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            vid: [
                {
                    "partido": self._partidos[1][0],
                    "orientacao": "Sim",
                    "siglaBancada": "Fdr PT-PCdoB-PV",
                }
            ]
            for vid in vot_ids
        }


def _client(
    repo: _FakeGraphRepo | None = None, **settings_kwargs: Any
) -> tuple[TestClient, _FakeGraphRepo]:
    fake = repo or _FakeGraphRepo()
    cfg = Settings(neo4j_password="dummy", **settings_kwargs)
    app = create_app(settings=cfg, repo_override=fake)
    return TestClient(app), fake


def test_health_endpoint() -> None:
    client, _ = _client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "neo4j": True}


def test_dataloader_evita_n_mais_1_em_deputados_partidos() -> None:
    client, repo = _client()
    query = """
    query {
      deputados(primeiro: 3) {
        edges {
          cursor
          node {
            id
            nome
            partidos { sigla }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    resp = client.post("/graphql", json={"query": query})
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" not in body
    edges = body["data"]["deputados"]["edges"]
    assert len(edges) == 3
    # 3 deputados resolvidos com APENAS 1 chamada em lote ao repositório (sem N+1)
    assert repo.chamadas_batch_partidos == 1


def test_paginacao_por_cursor_e_validacao() -> None:
    client, _ = _client()
    q1 = """
    query {
      deputados(primeiro: 2) {
        edges { node { nome } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    r1 = client.post("/graphql", json={"query": q1}).json()
    assert len(r1["data"]["deputados"]["edges"]) == 2
    assert r1["data"]["deputados"]["pageInfo"]["hasNextPage"] is True
    cursor = r1["data"]["deputados"]["pageInfo"]["endCursor"]

    q2 = """
    query($c: String) {
      deputados(primeiro: 2, depois: $c) {
        edges { node { nome } }
        pageInfo { hasNextPage }
      }
    }
    """
    r2 = client.post("/graphql", json={"query": q2, "variables": {"c": cursor}}).json()
    assert len(r2["data"]["deputados"]["edges"]) == 1
    assert r2["data"]["deputados"]["pageInfo"]["hasNextPage"] is False

    # Cursor inválido retorna erro amigável
    r_inv = client.post(
        "/graphql", json={"query": q2, "variables": {"c": "cursor-invalido"}}
    ).json()
    assert "Cursor 'depois' inválido" in r_inv["errors"][0]["message"]


def test_limite_de_profundidade_bloqueia_consulta_muito_aninhada() -> None:
    client, _ = _client(api_max_profundidade=4)
    query_profunda = """
    query {
      deputado(id: "1") {
        coautores(primeiro: 2) {
          deputado {
            coautores(primeiro: 2) {
              deputado {
                nome
              }
            }
          }
        }
      }
    }
    """
    body = client.post("/graphql", json={"query": query_profunda}).json()
    assert "errors" in body
    assert body["errors"][0]["extensions"]["code"] == "MAX_DEPTH_EXCEEDED"


def test_limite_de_custo_bloqueia_consulta_cara() -> None:
    client, _ = _client(api_max_custo=25)
    query_cara = """
    query {
      deputados(primeiro: 50) {
        edges {
          node {
            coautores(primeiro: 20) {
              proposicoesEmComum
              deputado { nome }
            }
          }
        }
      }
    }
    """
    body = client.post("/graphql", json={"query": query_cara}).json()
    assert "errors" in body
    assert body["errors"][0]["extensions"]["code"] == "MAX_COST_EXCEEDED"


def test_introspeccao_desabilitada_bloqueia_schema() -> None:
    client, _ = _client(api_introspeccao_habilitada=False)
    body = client.post("/graphql", json={"query": "{ __schema { types { name } } }"}).json()
    assert "errors" in body
    assert body["errors"][0]["extensions"]["code"] == "INTROSPECTION_DISABLED"


def test_mascaramento_de_erro_interno_nao_vaza_cypher() -> None:
    client, _ = _client(repo=_FakeGraphRepo(falhar_em_deputado=True))
    body = client.post("/graphql", json={"query": '{ deputado(id: "1") { nome } }'}).json()
    assert "errors" in body
    msg = body["errors"][0]["message"]
    assert msg == "Erro interno ao processar a consulta."
    assert "Cypher" not in msg
    assert "MATCH" not in msg


def test_rate_limit_retorna_429() -> None:
    client, _ = _client(api_rate_limit_requisicoes=2, api_rate_limit_janela_segundos=60)
    q = {"query": '{ votacao(id: "v-1") { id descricao } }'}
    assert client.post("/graphql", json=q).status_code == 200
    assert client.post("/graphql", json=q).status_code == 200
    r3 = client.post("/graphql", json=q)
    assert r3.status_code == 429
    assert "Retry-After" in r3.headers
    assert r3.json()["errors"][0]["extensions"]["code"] == "RATE_LIMIT_EXCEEDED"


def test_votacao_com_proposicoes_sem_argumentos_nao_falha_no_custo() -> None:
    """Regressão: campo lista sem argumentos (field.arguments=None no graphql-core 3.3)."""
    client, _ = _client()
    query = """
    query DetalheVotacao {
      votacao(id: "v-1") {
        id
        data
        descricao
        proposicoes { siglaTipo numero ano ementa }
        orientacoes { partido { sigla } orientacao siglaBancada }
      }
    }
    """
    resp = client.post("/graphql", json={"query": query})
    assert resp.status_code == 200
    body = resp.json()
    assert "errors" not in body
    assert body["data"]["votacao"]["id"] == "v-1"
    assert len(body["data"]["votacao"]["orientacoes"]) == 1


def test_rotas_rest_equivalentes_m6() -> None:
    client, _ = _client()
    r1 = client.get("/rest/deputados/1")
    assert r1.status_code == 200
    assert r1.json()["nome"] == "Ana Silva"
    assert len(r1.json()["partidos"]) == 1

    r2 = client.get("/rest/deputados?uf=SP&primeiro=5")
    assert r2.status_code == 200
    assert len(r2.json()["itens"]) == 1

    r3 = client.get("/rest/proposicoes?texto=artificial&primeiro=5")
    assert r3.status_code == 200
    assert len(r3.json()["itens"]) == 1
    assert r3.json()["itens"][0]["temas"][0]["nome"] == "Saúde"
