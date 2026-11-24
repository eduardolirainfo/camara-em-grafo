"""Rotas HTML + HTMX para exploração visual do grafo e ficha completa do deputado (M7)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from camara.api.ficha_service import (
    UF_INFO,
    FichaDeputadoService,
    url_bandeira_uf,
    url_logo_partido,
)
from camara.api.neo4j_repo import Neo4jGraphRepository
from camara.coleta.cliente import RetryableAPIError

_API_ERROS = (httpx.HTTPError, RetryableAPIError, OSError, KeyError, ValueError)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _fmt_brl(valor: float | None) -> str:
    if valor is None:
        return "R$ 0,00"
    s = f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _fmt_pct(valor: float | None) -> str:
    if valor is None:
        return "0%"
    return f"{float(valor) * 100:.1f}%".replace(".", ",")


def _fmt_data_br(valor: str | None) -> str:
    if not valor:
        return "—"
    parte = str(valor).strip()[:10]
    if len(parte) == 10 and parte[4] == "-" and parte[7] == "-":
        ano, mes, dia = parte.split("-")
        return f"{dia}/{mes}/{ano}"
    return str(valor)


def criar_ui_router(
    obter_repo: Callable[[], Neo4jGraphRepository],
    ficha_service: FichaDeputadoService,
    anos_disponiveis: list[int] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/ui", tags=["ui"])
    anos_ordenados = sorted(anos_disponiveis or [2023, 2024, 2025, 2026], reverse=True)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["url_bandeira_uf"] = url_bandeira_uf
    templates.env.globals["url_logo_partido"] = url_logo_partido
    templates.env.globals["UF_INFO"] = UF_INFO
    templates.env.globals["ANOS_DISPONIVEIS"] = anos_ordenados
    templates.env.filters["brl"] = _fmt_brl
    templates.env.filters["pct"] = _fmt_pct
    templates.env.filters["data_br"] = _fmt_data_br

    async def _mapa_grupos(repo: Neo4jGraphRepository) -> dict[int, dict[str, Any]]:
        resumo = await repo.resumo_geral_ui()
        return {int(c["id"]): c for c in resumo.get("comunidades", [])}

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def pagina_inicial(request: Request) -> HTMLResponse:
        repo = obter_repo()
        resumo = await repo.resumo_geral_ui()
        mapa_grupos = {int(c["id"]): c for c in resumo.get("comunidades", [])}
        inicial = await repo.buscar_deputados_ui(offset=0, limit=24)
        todos_deps = await repo.listar_todos_deputados_simples()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "resumo": resumo,
                "mapa_grupos": mapa_grupos,
                "ufs": sorted(UF_INFO.items()),
                "deputados": inicial["itens"],
                "total_filtrado": inicial["total"],
                "offset": 0,
                "limit": 24,
                "todos_deps": todos_deps,
            },
        )

    @router.get("/partials/deputados", response_class=HTMLResponse)
    async def partial_deputados(
        request: Request,
        busca: str | None = None,
        uf: str | None = None,
        partido: str | None = None,
        comunidade: str | None = None,
        ordenar: str = "nome",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=24, ge=1, le=60),
    ) -> HTMLResponse:
        repo = obter_repo()
        com_int = int(comunidade) if comunidade and comunidade.strip().isdigit() else None
        res = await repo.buscar_deputados_ui(
            busca=busca,
            uf=uf,
            partido=partido,
            comunidade=com_int,
            ordenar=ordenar,
            offset=offset,
            limit=limit,
        )
        mapa_grupos = await _mapa_grupos(repo)
        return templates.TemplateResponse(
            request=request,
            name="partials/deputados_grid.html",
            context={
                "deputados": res["itens"],
                "mapa_grupos": mapa_grupos,
                "total_filtrado": res["total"],
                "busca": busca or "",
                "uf": uf or "",
                "partido": partido or "",
                "comunidade": comunidade or "",
                "ordenar": ordenar,
                "offset": offset,
                "limit": limit,
            },
        )

    @router.get("/partials/proposicoes", response_class=HTMLResponse)
    async def partial_proposicoes(
        request: Request,
        texto: str | None = None,
        tema: str | None = None,
        ano: str | None = None,
        tipo: str | None = None,
    ) -> HTMLResponse:
        repo = obter_repo()
        ano_int = int(ano) if ano and ano.strip().isdigit() else None
        props = await repo.listar_proposicoes(
            texto=texto, tema=tema, ano=ano_int, tipo=tipo or None, offset=0, limit=20
        )
        ids = [int(p["id"]) for p in props]
        temas_map = await repo.batch_temas_por_proposicoes(ids) if ids else {}
        autores_map = (
            await repo.batch_autores_por_proposicoes([(i, 5) for i in ids]) if ids else {}
        )
        votacoes_map = await repo.batch_votacoes_por_proposicoes(ids) if ids else {}
        for p in props:
            pid = int(p["id"])
            p["temas"] = temas_map.get(pid, [])
            p["autores"] = autores_map.get((pid, 5), [])
            p["votacoes"] = votacoes_map.get(pid, [])
        return templates.TemplateResponse(
            request=request,
            name="partials/proposicoes_lista.html",
            context={"proposicoes": props, "texto": texto or ""},
        )

    @router.get("/partials/proposicao/{prop_id}/status", response_class=HTMLResponse)
    async def partial_proposicao_status(request: Request, prop_id: int) -> HTMLResponse:
        erro: str | None = None
        status: dict[str, Any] = {}
        try:
            status = await ficha_service.obter_status_proposicao(prop_id)
        except _API_ERROS:
            erro = "Não foi possível consultar a tramitação atual na API da Câmara."
        return templates.TemplateResponse(
            request=request,
            name="partials/proposicao_status.html",
            context={"status": status, "erro": erro},
        )

    @router.get("/partials/caminho", response_class=HTMLResponse)
    async def partial_caminho(
        request: Request,
        deputado_a: int = Query(...),
        deputado_b: int = Query(...),
    ) -> HTMLResponse:
        repo = obter_repo()
        nos = await repo.menor_caminho_coautoria(deputado_a, deputado_b)
        comparacao = await repo.comparar_dois_deputados(deputado_a, deputado_b)
        ids = [int(n["id"]) for n in nos]
        partidos_map = await repo.batch_partidos_por_deputados(ids) if ids else {}
        for n in nos:
            n["partidos"] = [p["sigla"] for p in partidos_map.get(int(n["id"]), [])]
        return templates.TemplateResponse(
            request=request,
            name="partials/caminho_resultado.html",
            context={"caminho": nos, "comparacao": comparacao},
        )

    @router.get("/deputado/{dep_id}", response_class=HTMLResponse)
    async def ficha_deputado(request: Request, dep_id: int) -> HTMLResponse:
        repo = obter_repo()
        dep = await repo.buscar_ficha_grafo(dep_id)
        if dep is None:
            raise HTTPException(status_code=404, detail="Deputado não encontrado no grafo.")

        detalhes: dict[str, Any] = {}
        erro_api: str | None = None
        try:
            detalhes = await ficha_service.obter_detalhes_api(dep_id)
        except _API_ERROS:
            erro_api = "Não foi possível consultar os detalhes extras na API da Câmara agora."

        funcionarios = await ficha_service.obter_funcionarios(dep_id)
        aliancas = await repo.buscar_aliancas_deputado(dep_id)
        mapa_grupos = await _mapa_grupos(repo)

        return templates.TemplateResponse(
            request=request,
            name="deputado.html",
            context={
                "dep": dep,
                "mapa_grupos": mapa_grupos,
                "detalhes": detalhes,
                "erro_api": erro_api,
                "total_funcionarios": len(funcionarios),
                "aliancas": aliancas,
            },
        )

    @router.get("/deputado/{dep_id}/tab/grafo", response_class=HTMLResponse)
    async def tab_grafo(request: Request, dep_id: int) -> HTMLResponse:
        aliancas = await obter_repo().buscar_aliancas_deputado(dep_id)
        return templates.TemplateResponse(
            request=request,
            name="partials/tab_grafo.html",
            context={"aliancas": aliancas, "dep_id": dep_id},
        )

    @router.get("/deputado/{dep_id}/tab/despesas", response_class=HTMLResponse)
    async def tab_despesas(
        request: Request,
        dep_id: int,
        ano: int = Query(default=2025, ge=2000, le=2100),
    ) -> HTMLResponse:
        erro: str | None = None
        despesas: dict[str, Any] = {
            "ano": ano,
            "quantidade_notas": 0,
            "total": 0.0,
            "categorias": [],
            "top_fornecedores": [],
            "itens": [],
        }
        try:
            despesas = await ficha_service.obter_despesas(dep_id, ano)
        except _API_ERROS:
            erro = "Falha temporária ao consultar despesas CEAP na API da Câmara."
        return templates.TemplateResponse(
            request=request,
            name="partials/tab_despesas.html",
            context={"despesas": despesas, "dep_id": dep_id, "ano": ano, "erro": erro},
        )

    @router.get("/deputado/{dep_id}/tab/funcionarios", response_class=HTMLResponse)
    async def tab_funcionarios(request: Request, dep_id: int) -> HTMLResponse:
        funcionarios = await ficha_service.obter_funcionarios(dep_id)
        return templates.TemplateResponse(
            request=request,
            name="partials/tab_funcionarios.html",
            context={"funcionarios": funcionarios, "dep_id": dep_id},
        )

    @router.get("/deputado/{dep_id}/tab/proposicoes", response_class=HTMLResponse)
    async def tab_proposicoes(request: Request, dep_id: int) -> HTMLResponse:
        repo = obter_repo()
        props = await repo.listar_proposicoes_de_deputado(dep_id, offset=0, limit=30)
        ids = [int(p["id"]) for p in props]
        temas_map = await repo.batch_temas_por_proposicoes(ids) if ids else {}
        votacoes_map = await repo.batch_votacoes_por_proposicoes(ids) if ids else {}
        for p in props:
            pid = int(p["id"])
            p["temas"] = temas_map.get(pid, [])
            p["votacoes"] = votacoes_map.get(pid, [])
        return templates.TemplateResponse(
            request=request,
            name="partials/tab_proposicoes.html",
            context={"proposicoes": props, "dep_id": dep_id},
        )

    @router.get("/deputado/{dep_id}/tab/atuacao", response_class=HTMLResponse)
    async def tab_atuacao(request: Request, dep_id: int) -> HTMLResponse:
        erro: str | None = None
        atuacao: dict[str, Any] = {"frentes": [], "orgaos": [], "discursos": []}
        try:
            atuacao = await ficha_service.obter_atuacao(dep_id)
        except _API_ERROS:
            erro = "Falha temporária ao consultar frentes, órgãos e discursos na API da Câmara."
        return templates.TemplateResponse(
            request=request,
            name="partials/tab_atuacao.html",
            context={"atuacao": atuacao, "dep_id": dep_id, "erro": erro},
        )

    @router.get("/deputado/{dep_id}/tab/carreira", response_class=HTMLResponse)
    async def tab_carreira(request: Request, dep_id: int) -> HTMLResponse:
        erro: str | None = None
        carreira: dict[str, Any] = {
            "profissoes": [],
            "ocupacoes": [],
            "mandatos_externos": [],
            "historico": [],
        }
        try:
            carreira = await ficha_service.obter_carreira(dep_id)
        except _API_ERROS:
            erro = "Falha temporária ao consultar histórico e carreira na API da Câmara."
        return templates.TemplateResponse(
            request=request,
            name="partials/tab_carreira.html",
            context={"carreira": carreira, "dep_id": dep_id, "erro": erro},
        )

    return router
