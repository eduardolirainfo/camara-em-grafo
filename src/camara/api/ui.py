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
    formatar_trecho_legivel,
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


def _fmt_brl_curto(valor: float | None) -> str:
    if not valor:
        return "R$ 0"
    v = float(valor)
    if v >= 1_000_000:
        return f"R$ {v / 1_000_000:.2f} mi".replace(".", ",")
    if v >= 1_000:
        return f"R$ {v / 1_000:.0f} mil"
    return f"R$ {v:.0f}"


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
    templates.env.filters["brl_curto"] = _fmt_brl_curto
    templates.env.filters["pct"] = _fmt_pct
    templates.env.filters["data_br"] = _fmt_data_br
    templates.env.filters["trecho_legivel"] = formatar_trecho_legivel

    async def _mapa_grupos(repo: Neo4jGraphRepository) -> dict[int, dict[str, Any]]:
        resumo = await repo.resumo_geral_ui()
        return {int(c["id"]): c for c in resumo.get("comunidades", [])}

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def pagina_inicial(
        request: Request,
        busca: str | None = None,
        uf: str | None = None,
        partido: str | None = None,
        comunidade: str | None = None,
        situacao: str | None = "em_exercicio",
        ordenar: str = "nome",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=24, ge=1, le=60),
        painel: str = "deputados",
    ) -> HTMLResponse:
        repo = obter_repo()
        resumo = await repo.resumo_geral_ui()
        mapa_grupos = {int(c["id"]): c for c in resumo.get("comunidades", [])}
        com_int = int(comunidade) if comunidade and comunidade.strip().isdigit() else None
        sit_filtro = None if situacao in {None, "", "todos"} else situacao
        inicial = await repo.buscar_deputados_ui(
            busca=busca,
            uf=uf,
            partido=partido,
            comunidade=com_int,
            situacao=sit_filtro,
            ordenar=ordenar,
            offset=offset,
            limit=limit,
        )
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
                "busca": busca or "",
                "uf": (uf or "").upper(),
                "partido": partido or "",
                "comunidade": comunidade or "",
                "situacao": "todos" if sit_filtro is None else sit_filtro,
                "ordenar": ordenar,
                "offset": offset,
                "limit": limit,
                "painel_inicial": painel if painel in {"deputados", "proposicoes", "caminho", "fornecedores"} else "deputados",
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
        situacao: str | None = None,
        ordenar: str = "nome",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=24, ge=1, le=60),
    ) -> HTMLResponse:
        repo = obter_repo()
        com_int = int(comunidade) if comunidade and comunidade.strip().isdigit() else None
        sit_filtro = None if situacao in {None, "", "todos"} else situacao
        res = await repo.buscar_deputados_ui(
            busca=busca,
            uf=uf,
            partido=partido,
            comunidade=com_int,
            situacao=sit_filtro,
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
                "situacao": "todos" if sit_filtro is None else sit_filtro,
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

    @router.get("/partials/fornecedores", response_class=HTMLResponse)
    async def partial_fornecedores(
        request: Request,
        busca: str | None = None,
        ordenar: str = "deputados_desc",
        ocultar_comuns: str | None = None,
    ) -> HTMLResponse:
        ocultar = str(ocultar_comuns or "").lower() in {"1", "true", "on", "sim"}
        fornecedores = await obter_repo().buscar_fornecedores_ui(
            busca=busca, ordenar=ordenar, ocultar_comuns=ocultar, limit=25
        )
        return templates.TemplateResponse(
            request=request,
            name="partials/fornecedores_lista.html",
            context={"fornecedores": fornecedores, "busca": busca or ""},
        )

    @router.get("/partials/cnpj/{cnpj}", response_class=HTMLResponse)
    async def partial_cnpj_receita(
        request: Request,
        cnpj: str,
        dep_id: int | None = None,
    ) -> HTMLResponse:
        nomes_deps: list[str] = []
        if dep_id is not None:
            dep = await obter_repo().buscar_ficha_grafo(dep_id)
            if dep and dep.get("nome"):
                nomes_deps.append(str(dep["nome"]))
            try:
                det = await ficha_service.obter_detalhes_api(dep_id)
                if det.get("nomeCivil"):
                    nomes_deps.append(str(det["nomeCivil"]))
            except _API_ERROS:
                pass
        else:
            forns = await obter_repo().buscar_fornecedores_ui(busca=cnpj, limit=1)
            if forns:
                for d in forns[0].get("topDeputados") or []:
                    if d.get("nome"):
                        nomes_deps.append(str(d["nome"]))

        try:
            dados = await ficha_service.obter_dados_cnpj(cnpj, nomes_deputados=nomes_deps)
        except _API_ERROS:
            dados = {
                "erro": "Falha temporária ao consultar a Receita Federal (BrasilAPI). Tente novamente em instantes."
            }
        return templates.TemplateResponse(
            request=request,
            name="partials/cnpj_detalhe.html",
            context={"dados": dados},
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

        pessoal_atual = await ficha_service.obter_pessoal_gabinete(
            dep_id,
            ano=anos_ordenados[0],
            nome_civil=str(detalhes.get("nomeCivil") or ""),
        )
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
                "total_funcionarios": pessoal_atual["ativos"],
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
        mes: str | None = None,
    ) -> HTMLResponse:
        mes_int = int(mes) if mes and mes.strip().isdigit() and 1 <= int(mes) <= 12 else None
        erro: str | None = None
        despesas: dict[str, Any] = {
            "ano": ano,
            "mes": mes_int,
            "quantidade_notas": 0,
            "total": 0.0,
            "total_ano": 0.0,
            "mensal": [],
            "categorias": [],
            "top_fornecedores": [],
            "itens": [],
        }
        nomes_dep: list[str] = []
        dep_g = await obter_repo().buscar_ficha_grafo(dep_id)
        if dep_g and dep_g.get("nome"):
            nomes_dep.append(str(dep_g["nome"]))
        try:
            det = await ficha_service.obter_detalhes_api(dep_id)
            if det.get("nomeCivil"):
                nomes_dep.append(str(det["nomeCivil"]))
        except _API_ERROS:
            pass
        try:
            despesas = await ficha_service.obter_despesas(
                dep_id,
                ano,
                mes=mes_int,
                nomes_parlamentar=nomes_dep,
                anos_verificacao=anos_ordenados,
            )
        except _API_ERROS:
            erro = "Falha temporária ao consultar despesas CEAP na API da Câmara."
        return templates.TemplateResponse(
            request=request,
            name="partials/tab_despesas.html",
            context={
                "despesas": despesas,
                "dep_id": dep_id,
                "ano": ano,
                "mes": mes_int,
                "erro": erro,
            },
        )

    @router.get("/deputado/{dep_id}/tab/funcionarios", response_class=HTMLResponse)
    async def tab_funcionarios(
        request: Request,
        dep_id: int,
        ano: str | None = None,
        busca: str | None = None,
    ) -> HTMLResponse:
        busca_limpa = (busca or "").strip()
        modo_todos = (ano or "").strip().lower() == "todos"
        nome_civil = ""
        try:
            det = await ficha_service.obter_detalhes_api(dep_id)
            nome_civil = str(det.get("nomeCivil") or "")
        except _API_ERROS:
            pass

        if modo_todos:
            pessoal = await ficha_service.obter_pessoal_gabinete_todos_anos(
                dep_id,
                anos=anos_ordenados,
                nome_civil=nome_civil,
                busca=busca_limpa,
            )
            ano_ctx: int | str = "todos"
        else:
            ano_sel = int(ano) if ano and ano.strip().isdigit() else anos_ordenados[0]
            pessoal = await ficha_service.obter_pessoal_gabinete(
                dep_id, ano=ano_sel, nome_civil=nome_civil, busca=busca_limpa
            )
            ano_ctx = ano_sel

        return templates.TemplateResponse(
            request=request,
            name="partials/tab_funcionarios.html",
            context={
                "pessoal": pessoal,
                "funcionarios": pessoal["itens"],
                "dep_id": dep_id,
                "ano": ano_ctx,
                "modo_todos": modo_todos,
                "busca": busca_limpa,
            },
        )

    @router.get("/deputado/{dep_id}/tab/proposicoes", response_class=HTMLResponse)
    async def tab_proposicoes(
        request: Request,
        dep_id: int,
        tipo: str | None = None,
        resultado: str | None = None,
        ordenar: str = "recentes",
    ) -> HTMLResponse:
        repo = obter_repo()
        if hasattr(repo, "listar_proposicoes_de_deputado_filtrado"):
            props = await repo.listar_proposicoes_de_deputado_filtrado(
                dep_id, tipo=tipo, resultado=resultado, ordenar=ordenar, limit=40
            )
        else:
            props = await repo.listar_proposicoes_de_deputado(dep_id, offset=0, limit=40)
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
            context={
                "proposicoes": props,
                "dep_id": dep_id,
                "tipo": (tipo or "").upper(),
                "resultado": (resultado or "").lower(),
                "ordenar": ordenar,
            },
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

    @router.get("/funcionario/{codigo}", response_class=HTMLResponse)
    async def pagina_funcionario(
        request: Request,
        codigo: str,
        nome: str = "Funcionário de Gabinete",
        cargo: str = "",
        dep_id: int | None = None,
        ano: str = "todos",
    ) -> HTMLResponse:
        dep_nome = ""
        if dep_id is not None:
            dep = await obter_repo().buscar_ficha_grafo(dep_id)
            if dep:
                dep_nome = str(dep.get("nome") or "")
        anos_consulta = (
            [int(ano)] if ano.isdigit() and int(ano) in anos_ordenados else sorted(anos_ordenados)
        )
        historico = await ficha_service.obter_historico_remuneracao(codigo, anos_consulta)
        return templates.TemplateResponse(
            request=request,
            name="funcionario.html",
            context={
                "codigo": codigo,
                "nome": nome,
                "cargo": cargo,
                "dep_id": dep_id,
                "dep_nome": dep_nome,
                "ano_selecionado": ano,
                "historico": historico,
            },
        )

    @router.get("/funcionario/{codigo}/dados", response_class=HTMLResponse)
    async def partial_funcionario_salarios(
        request: Request,
        codigo: str,
        ano: str = "todos",
    ) -> HTMLResponse:
        anos_consulta = (
            [int(ano)] if ano.isdigit() and int(ano) in anos_ordenados else sorted(anos_ordenados)
        )
        historico = await ficha_service.obter_historico_remuneracao(codigo, anos_consulta)
        return templates.TemplateResponse(
            request=request,
            name="partials/funcionario_salarios.html",
            context={
                "codigo": codigo,
                "ano_selecionado": ano,
                "historico": historico,
            },
        )

    return router
