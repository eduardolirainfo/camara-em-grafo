"""Aplicação FastAPI que expõe a API GraphQL (Strawberry) e rota de saúde."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError
from strawberry.fastapi import GraphQLRouter

from camara.api.dataloaders import GraphDataLoaders
from camara.api.ficha_service import FichaDeputadoService
from camara.api.limits import RateLimitMiddleware
from camara.api.neo4j_repo import Neo4jGraphRepository
from camara.api.schema import GraphQLContext, criar_schema
from camara.api.ui import criar_ui_router
from camara.config import Settings, get_settings


def create_app(
    settings: Settings | None = None,
    repo_override: Neo4jGraphRepository | None = None,
    ficha_service_override: FichaDeputadoService | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    driver_holder: dict[str, AsyncDriver] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if repo_override is None:
            if not cfg.neo4j_password:
                raise RuntimeError("NEO4J_PASSWORD não definida. Configure o arquivo .env.")
            driver_holder["driver"] = AsyncGraphDatabase.driver(
                cfg.neo4j_uri,
                auth=(cfg.neo4j_user, cfg.neo4j_password),
            )
        try:
            yield
        finally:
            driver = driver_holder.pop("driver", None)
            if driver is not None:
                await driver.close()

    app = FastAPI(
        title="Câmara em Grafo — API GraphQL",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        RateLimitMiddleware,
        max_requisicoes=cfg.api_rate_limit_requisicoes,
        janela_segundos=cfg.api_rate_limit_janela_segundos,
    )

    def _obter_repo() -> Neo4jGraphRepository:
        if repo_override is not None:
            return repo_override
        return Neo4jGraphRepository(driver_holder["driver"])

    async def get_context() -> GraphQLContext:
        repo = _obter_repo()
        return GraphQLContext(
            repo=repo,
            loaders=GraphDataLoaders(repo),
            max_itens_pagina=cfg.api_max_itens_pagina,
        )

    schema = criar_schema(
        max_profundidade=cfg.api_max_profundidade,
        max_custo=cfg.api_max_custo,
        introspeccao_habilitada=cfg.api_introspeccao_habilitada,
    )

    graphql_router: GraphQLRouter[Any, Any] = GraphQLRouter(
        schema,
        context_getter=get_context,
        graphql_ide="graphiql" if cfg.api_graphiql_habilitado else None,
    )
    app.include_router(graphql_router, prefix="/graphql")

    ficha_svc = ficha_service_override or FichaDeputadoService(
        cache_dir=cfg.camara_cache_dir,
        api_base_url=cfg.camara_api_base_url,
        arquivos_base_url=cfg.camara_arquivos_base_url,
        user_agent=cfg.camara_user_agent,
    )
    app.include_router(criar_ui_router(_obter_repo, ficha_svc, anos_disponiveis=cfg.anos_m2()))

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/ui")

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            ok = await _obter_repo().verificar_saude()
        except (Neo4jError, OSError, RuntimeError):
            ok = False
        status = 200 if ok else 503
        return JSONResponse(
            status_code=status,
            content={"status": "ok" if ok else "degraded", "neo4j": ok},
        )

    # ---- Rotas REST equivalentes para comparação direta no benchmark (M6) ----

    @app.get("/rest/deputados/{dep_id}")
    async def rest_deputado(
        dep_id: int,
        primeiro_coautores: int = Query(default=3, ge=1, le=50),
        primeiro_vota_como: int = Query(default=3, ge=1, le=50),
        minimo_votacoes: int = Query(default=100, ge=1),
    ) -> dict[str, Any]:
        repo = _obter_repo()
        dep = await repo.buscar_deputado(dep_id)
        if dep is None:
            raise HTTPException(status_code=404, detail="Deputado não encontrado.")
        partidos = (await repo.batch_partidos_por_deputados([dep_id])).get(dep_id, [])
        coautores = (
            await repo.batch_coautores_por_deputados([(dep_id, primeiro_coautores)])
        ).get((dep_id, primeiro_coautores), [])
        vota_como = (
            await repo.batch_vota_como_por_deputados(
                [(dep_id, primeiro_vota_como, minimo_votacoes)]
            )
        ).get((dep_id, primeiro_vota_como, minimo_votacoes), [])
        contra = (await repo.batch_contra_orientacao_por_deputados([dep_id])).get(dep_id, [])
        return {
            **dep,
            "partidos": partidos,
            "coautores": coautores,
            "votaComo": vota_como,
            "contraOrientacao": contra,
        }

    @app.get("/rest/deputados")
    async def rest_deputados(
        uf: str | None = None,
        partido: str | None = None,
        primeiro: int = Query(default=10, ge=1, le=50),
        primeiro_coautores: int = Query(default=3, ge=1, le=50),
        primeiro_vota_como: int = Query(default=3, ge=1, le=50),
        minimo_votacoes: int = Query(default=100, ge=1),
    ) -> dict[str, Any]:
        repo = _obter_repo()
        deps = await repo.listar_deputados(uf=uf, partido=partido, offset=0, limit=primeiro)
        ids = [int(d["id"]) for d in deps]
        partidos_map = await repo.batch_partidos_por_deputados(ids)
        coaut_map = await repo.batch_coautores_por_deputados([(i, primeiro_coautores) for i in ids])
        vota_map = await repo.batch_vota_como_por_deputados(
            [(i, primeiro_vota_como, minimo_votacoes) for i in ids]
        )
        itens = []
        for d in deps:
            i = int(d["id"])
            itens.append(
                {
                    **d,
                    "partidos": partidos_map.get(i, []),
                    "coautores": coaut_map.get((i, primeiro_coautores), []),
                    "votaComo": vota_map.get((i, primeiro_vota_como, minimo_votacoes), []),
                }
            )
        return {"itens": itens}

    @app.get("/rest/proposicoes")
    async def rest_proposicoes(
        texto: str | None = None,
        tema: str | None = None,
        ano: int | None = None,
        tipo: str | None = None,
        primeiro: int = Query(default=10, ge=1, le=50),
        primeiro_autores: int = Query(default=3, ge=1, le=50),
    ) -> dict[str, Any]:
        repo = _obter_repo()
        props = await repo.listar_proposicoes(
            tema=tema, texto=texto, ano=ano, tipo=tipo, offset=0, limit=primeiro
        )
        ids = [int(p["id"]) for p in props]
        temas_map = await repo.batch_temas_por_proposicoes(ids)
        aut_map = await repo.batch_autores_por_proposicoes([(i, primeiro_autores) for i in ids])
        itens = []
        for p in props:
            i = int(p["id"])
            itens.append(
                {
                    **p,
                    "temas": temas_map.get(i, []),
                    "autores": aut_map.get((i, primeiro_autores), []),
                }
            )
        return {"itens": itens}

    return app


app = create_app()
