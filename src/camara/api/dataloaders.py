"""DataLoaders por requisição para eliminar consultas N+1 contra o Neo4j."""

from __future__ import annotations

from typing import Any

from strawberry.dataloader import DataLoader

from camara.api.neo4j_repo import Neo4jGraphRepository


class GraphDataLoaders:
    """Agrupa os DataLoaders instanciados uma vez por requisição GraphQL."""

    def __init__(self, repo: Neo4jGraphRepository) -> None:
        self.repo = repo
        self.partidos_por_deputado: DataLoader[int, list[dict[str, Any]]] = DataLoader(
            load_fn=self._load_partidos
        )
        self.coautores_por_deputado: DataLoader[tuple[int, int], list[dict[str, Any]]] = (
            DataLoader(load_fn=self._load_coautores)
        )
        self.vota_como_por_deputado: DataLoader[tuple[int, int, int], list[dict[str, Any]]] = (
            DataLoader(load_fn=self._load_vota_como)
        )
        self.contra_orientacao_por_deputado: DataLoader[int, list[dict[str, Any]]] = DataLoader(
            load_fn=self._load_contra_orientacao
        )
        self.temas_por_proposicao: DataLoader[int, list[dict[str, Any]]] = DataLoader(
            load_fn=self._load_temas
        )
        self.autores_por_proposicao: DataLoader[tuple[int, int], list[dict[str, Any]]] = (
            DataLoader(load_fn=self._load_autores)
        )
        self.proposicoes_por_votacao: DataLoader[str, list[dict[str, Any]]] = DataLoader(
            load_fn=self._load_proposicoes_votacao
        )
        self.orientacoes_por_votacao: DataLoader[str, list[dict[str, Any]]] = DataLoader(
            load_fn=self._load_orientacoes_votacao
        )

    async def _load_partidos(self, keys: list[int]) -> list[list[dict[str, Any]]]:
        mapa = await self.repo.batch_partidos_por_deputados(keys)
        return [mapa.get(k, []) for k in keys]

    async def _load_coautores(
        self, keys: list[tuple[int, int]]
    ) -> list[list[dict[str, Any]]]:
        mapa = await self.repo.batch_coautores_por_deputados(keys)
        return [mapa.get(k, []) for k in keys]

    async def _load_vota_como(
        self, keys: list[tuple[int, int, int]]
    ) -> list[list[dict[str, Any]]]:
        mapa = await self.repo.batch_vota_como_por_deputados(keys)
        return [mapa.get(k, []) for k in keys]

    async def _load_contra_orientacao(self, keys: list[int]) -> list[list[dict[str, Any]]]:
        mapa = await self.repo.batch_contra_orientacao_por_deputados(keys)
        return [mapa.get(k, []) for k in keys]

    async def _load_temas(self, keys: list[int]) -> list[list[dict[str, Any]]]:
        mapa = await self.repo.batch_temas_por_proposicoes(keys)
        return [mapa.get(k, []) for k in keys]

    async def _load_autores(
        self, keys: list[tuple[int, int]]
    ) -> list[list[dict[str, Any]]]:
        mapa = await self.repo.batch_autores_por_proposicoes(keys)
        return [mapa.get(k, []) for k in keys]

    async def _load_proposicoes_votacao(self, keys: list[str]) -> list[list[dict[str, Any]]]:
        mapa = await self.repo.batch_proposicoes_por_votacoes(keys)
        return [mapa.get(k, []) for k in keys]

    async def _load_orientacoes_votacao(self, keys: list[str]) -> list[list[dict[str, Any]]]:
        mapa = await self.repo.batch_orientacoes_por_votacoes(keys)
        return [mapa.get(k, []) for k in keys]
