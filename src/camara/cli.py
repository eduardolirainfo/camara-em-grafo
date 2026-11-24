"""Interface de linha de comando para coleta e carga do Câmara em Grafo."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from neo4j import GraphDatabase

from camara.analise.gds_m5 import executar_analises_m5
from camara.carga.neo4j_loader import Neo4jLoaderM1
from camara.carga.neo4j_loader_gastos import carregar_gastos_e_equipe
from camara.carga.neo4j_loader_m2 import Neo4jLoaderM2
from camara.carga.neo4j_loader_m3 import Neo4jLoaderM3
from camara.coleta.arquivos import ArquivosCamaraClient
from camara.coleta.cache import DiskCache
from camara.coleta.cliente import CamaraAPIClient
from camara.coleta.m1 import carregar_m1_do_cache, coletar_m1
from camara.config import Settings, get_settings
from camara.medicao.benchmark_m6 import executar_benchmark_m6, salvar_benchmark_csv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camara",
        description="CLI de coleta e carga do projeto Câmara em Grafo.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p_coletar = sub.add_parser(
        "coletar-m1",
        help="Coleta legislatura, partidos e deputados da API da Câmara para o cache em disco.",
    )
    p_coletar.add_argument("--legislatura", type=int, default=None)
    p_coletar.add_argument(
        "--forcar",
        action="store_true",
        help="Ignora cache existente e busca novamente da API.",
    )

    p_carregar = sub.add_parser(
        "carregar-m1",
        help="Executa coleta (usando cache se houver) e carga idempotente no Neo4j.",
    )
    p_carregar.add_argument("--legislatura", type=int, default=None)
    p_carregar.add_argument("--forcar-coleta", action="store_true")
    p_carregar.add_argument(
        "--limpar",
        action="store_true",
        help="Limpa o banco antes de carregar.",
    )
    p_carregar.add_argument(
        "--salvar-medicao",
        type=Path,
        default=None,
        help="Salva o resumo JSON da carga no caminho informado.",
    )

    p_recarregar = sub.add_parser(
        "recarregar-m1",
        help="Recarrega todo o M1 no Neo4j exclusivamente a partir do cache em disco (sem rede).",
    )
    p_recarregar.add_argument("--legislatura", type=int, default=None)
    p_recarregar.add_argument(
        "--limpar",
        action="store_true",
        help="Remove nós e relações antes de recarregar do cache.",
    )
    p_recarregar.add_argument(
        "--salvar-medicao",
        type=Path,
        default=None,
        help="Salva o resumo JSON da carga no caminho informado.",
    )

    sub.add_parser("status", help="Mostra versões do Neo4j/GDS e contagem atual de nós e relações.")

    p_coletar_m2 = sub.add_parser(
        "coletar-m2",
        help="Baixa os arquivos CSV em lote do M2 (proposições, autores, temas) para cache/.",
    )
    p_coletar_m2.add_argument("--anos", type=int, nargs="+", default=None)
    p_coletar_m2.add_argument(
        "--forcar",
        action="store_true",
        help="Ignora arquivos em cache e baixa novamente.",
    )

    p_carregar_m2 = sub.add_parser(
        "carregar-m2",
        help="Baixa (usando cache se houver) e carrega proposições, temas, autores e COAUTOR_COM.",
    )
    p_carregar_m2.add_argument("--legislatura", type=int, default=None)
    p_carregar_m2.add_argument("--anos", type=int, nargs="+", default=None)
    p_carregar_m2.add_argument(
        "--tipos",
        type=str,
        default=None,
        help="Lista separada por vírgula (ex.: PL,PLP,PEC,PDL,PRC) ou TODOS.",
    )
    p_carregar_m2.add_argument("--min-coautorias", type=int, default=1)
    p_carregar_m2.add_argument("--forcar-coleta", action="store_true")
    p_carregar_m2.add_argument(
        "--limpar-m2",
        action="store_true",
        help="Remove nós Proposicao/Tema e relações COAUTOR_COM antes de carregar.",
    )
    p_carregar_m2.add_argument("--salvar-medicao", type=Path, default=None)

    p_recarregar_m2 = sub.add_parser(
        "recarregar-m2",
        help="Recarrega o M2 no Neo4j exclusivamente a partir dos CSVs em cache (sem rede).",
    )
    p_recarregar_m2.add_argument("--legislatura", type=int, default=None)
    p_recarregar_m2.add_argument("--anos", type=int, nargs="+", default=None)
    p_recarregar_m2.add_argument("--tipos", type=str, default=None)
    p_recarregar_m2.add_argument("--min-coautorias", type=int, default=1)
    p_recarregar_m2.add_argument("--limpar-m2", action="store_true")
    p_recarregar_m2.add_argument("--salvar-medicao", type=Path, default=None)

    p_coletar_m3 = sub.add_parser(
        "coletar-m3",
        help="Baixa os arquivos CSV em lote do M3 (votações, votos, orientações, objetos).",
    )
    p_coletar_m3.add_argument("--anos", type=int, nargs="+", default=None)
    p_coletar_m3.add_argument("--forcar", action="store_true")

    p_carregar_m3 = sub.add_parser(
        "carregar-m3",
        help="Baixa (usando cache se houver) e carrega votações, votos, orientações, VOTA_COMO e CONTRA_ORIENTACAO.",
    )
    p_carregar_m3.add_argument("--legislatura", type=int, default=None)
    p_carregar_m3.add_argument("--anos", type=int, nargs="+", default=None)
    p_carregar_m3.add_argument("--min-votacoes", type=int, default=20)
    p_carregar_m3.add_argument("--forcar-coleta", action="store_true")
    p_carregar_m3.add_argument(
        "--limpar-m3",
        action="store_true",
        help="Remove nós Votacao e relações VOTA_COMO/CONTRA_ORIENTACAO antes de carregar.",
    )
    p_carregar_m3.add_argument("--salvar-medicao", type=Path, default=None)

    p_recarregar_m3 = sub.add_parser(
        "recarregar-m3",
        help="Recarrega o M3 exclusivamente a partir dos CSVs em cache (sem rede).",
    )
    p_recarregar_m3.add_argument("--legislatura", type=int, default=None)
    p_recarregar_m3.add_argument("--anos", type=int, nargs="+", default=None)
    p_recarregar_m3.add_argument("--min-votacoes", type=int, default=20)
    p_recarregar_m3.add_argument("--limpar-m3", action="store_true")
    p_recarregar_m3.add_argument("--salvar-medicao", type=Path, default=None)

    p_analisar_m5 = sub.add_parser(
        "analisar-m5",
        help="Executa análises GDS do M5 (Louvain, Leiden, PageRank, Betweenness e similaridade por temas).",
    )
    p_analisar_m5.add_argument("--legislatura", type=int, default=None)
    p_analisar_m5.add_argument("--min-votacoes", type=int, default=50)
    p_analisar_m5.add_argument("--min-concordancia", type=float, default=0.70)
    p_analisar_m5.add_argument("--min-coautorias", type=int, default=5)
    p_analisar_m5.add_argument("--salvar-medicao", type=Path, default=None)

    p_medir_m6 = sub.add_parser(
        "medir-m6",
        help="Executa benchmark GraphQL vs REST equivalente sobre a API em execução (M6).",
    )
    p_medir_m6.add_argument("--url", type=str, default="http://localhost:8001")
    p_medir_m6.add_argument("--repeticoes", type=int, default=30)
    p_medir_m6.add_argument("--concorrencia", type=int, default=5)
    p_medir_m6.add_argument("--votacao-id", type=str, default="1197773-90")
    p_medir_m6.add_argument("--salvar-json", type=Path, default=None)
    p_medir_m6.add_argument("--salvar-csv", type=Path, default=None)

    p_gastos = sub.add_parser(
        "carregar-gastos",
        help="Baixa os arquivos anuais CEAP (.csv.zip) e grava gastos + equipe de gabinete nos deputados.",
    )
    p_gastos.add_argument("--anos", type=int, nargs="+", default=None)
    p_gastos.add_argument("--forcar", action="store_true")

    return parser


def _criar_driver(settings: Settings):
    if not settings.neo4j_password:
        raise RuntimeError("NEO4J_PASSWORD não definida. Configure o arquivo .env.")
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


async def _executar_coleta_m1(settings: Settings, legislatura_id: int, forcar: bool):
    cache = DiskCache(settings.camara_cache_dir)
    async with CamaraAPIClient(
        base_url=settings.camara_api_base_url,
        cache=cache,
        user_agent=settings.camara_user_agent,
        max_concorrencia=settings.camara_max_concorrencia,
        pausa_segundos=settings.camara_pausa_segundos,
        timeout_segundos=settings.camara_timeout_segundos,
        max_tentativas=settings.camara_max_tentativas,
    ) as client:
        dados = await coletar_m1(
            client=client,
            legislatura_id=legislatura_id,
            itens_por_pagina=settings.camara_itens_por_pagina,
            use_cache=not forcar,
        )
        return dados, client.requests_rede, client.hits_cache


def _parse_tipos(raw: str | None, settings: Settings) -> set[str] | None:
    if raw is None:
        return settings.tipos_m2()
    itens = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if not itens or "TODOS" in itens:
        return None
    return set(itens)


async def _executar_coleta_m2(settings: Settings, anos: list[int], forcar: bool):
    async with ArquivosCamaraClient(
        cache_dir=settings.camara_cache_dir,
        base_url=settings.camara_arquivos_base_url,
        user_agent=settings.camara_user_agent,
        max_concorrencia=settings.camara_max_concorrencia,
        pausa_segundos=settings.camara_pausa_segundos,
        timeout_segundos=settings.camara_download_timeout_segundos,
        max_tentativas=settings.camara_max_tentativas,
    ) as client:
        arquivos = await client.coletar_m2(anos=anos, use_cache=not forcar)
        return arquivos, client.downloads_rede, client.hits_cache


async def _executar_coleta_m3(settings: Settings, anos: list[int], forcar: bool):
    async with ArquivosCamaraClient(
        cache_dir=settings.camara_cache_dir,
        base_url=settings.camara_arquivos_base_url,
        user_agent=settings.camara_user_agent,
        max_concorrencia=settings.camara_max_concorrencia,
        pausa_segundos=settings.camara_pausa_segundos,
        timeout_segundos=settings.camara_download_timeout_segundos,
        max_tentativas=settings.camara_max_tentativas,
    ) as client:
        arquivos = await client.coletar_m3(anos=anos, use_cache=not forcar)
        return arquivos, client.downloads_rede, client.hits_cache


def _salvar_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    if args.comando == "coletar-m1":
        leg_id = args.legislatura or settings.camara_legislatura_id
        dados, reqs, hits = asyncio.run(_executar_coleta_m1(settings, leg_id, args.forcar))
        print(
            json.dumps(
                {
                    "legislatura_id": leg_id,
                    "partidos": len(dados.partidos),
                    "deputados": len(dados.deputados),
                    "requisicoes_rede": reqs,
                    "hits_cache": hits,
                    "cache_dir": str(settings.camara_cache_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.comando == "carregar-m1":
        leg_id = args.legislatura or settings.camara_legislatura_id
        dados, reqs, hits = asyncio.run(_executar_coleta_m1(settings, leg_id, args.forcar_coleta))
        with _criar_driver(settings) as driver:
            loader = Neo4jLoaderM1(driver)
            resumo = loader.carregar(dados, limpar_antes=args.limpar)
            saida = resumo.to_dict()
            saida["requisicoes_rede"] = reqs
            saida["hits_cache"] = hits
            saida["versoes_servidor"] = loader.versoes_neo4j()
        if args.salvar_medicao:
            _salvar_json(args.salvar_medicao, saida)
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "recarregar-m1":
        leg_id = args.legislatura or settings.camara_legislatura_id
        cache = DiskCache(settings.camara_cache_dir)
        dados = carregar_m1_do_cache(cache, leg_id)
        with _criar_driver(settings) as driver:
            loader = Neo4jLoaderM1(driver)
            resumo = loader.carregar(dados, limpar_antes=args.limpar)
            saida = resumo.to_dict()
            saida["origem"] = "cache"
            saida["versoes_servidor"] = loader.versoes_neo4j()
        if args.salvar_medicao:
            _salvar_json(args.salvar_medicao, saida)
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "status":
        with _criar_driver(settings) as driver:
            loader = Neo4jLoaderM1(driver)
            nos, relacoes = loader.contar_grafo()
            saida = {
                "versoes_servidor": loader.versoes_neo4j(),
                "nos_por_rotulo": nos,
                "total_nos": sum(nos.values()),
                "relacoes_por_tipo": relacoes,
                "total_relacoes": sum(relacoes.values()),
            }
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "coletar-m2":
        anos = args.anos or settings.anos_m2()
        arquivos, dls, hits = asyncio.run(_executar_coleta_m2(settings, anos, args.forcar))
        print(
            json.dumps(
                {
                    "anos": anos,
                    "arquivos": [
                        {
                            "ano": a.ano,
                            "proposicoes": str(a.proposicoes),
                            "autores": str(a.autores),
                            "temas": str(a.temas),
                        }
                        for a in arquivos
                    ],
                    "downloads_rede": dls,
                    "hits_cache": hits,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.comando == "carregar-m2":
        leg_id = args.legislatura or settings.camara_legislatura_id
        anos = args.anos or settings.anos_m2()
        tipos = _parse_tipos(args.tipos, settings)
        arquivos, dls, hits = asyncio.run(
            _executar_coleta_m2(settings, anos, args.forcar_coleta)
        )
        with _criar_driver(settings) as driver:
            loader = Neo4jLoaderM2(driver)
            resumo = loader.carregar(
                arquivos_por_ano=arquivos,
                legislatura_id=leg_id,
                tipos=tipos,
                minimo_coautorias=args.min_coautorias,
                limpar_antes=args.limpar_m2,
            )
            saida = resumo.to_dict()
            saida["downloads_rede"] = dls
            saida["hits_cache"] = hits
        if args.salvar_medicao:
            _salvar_json(args.salvar_medicao, saida)
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "recarregar-m2":
        leg_id = args.legislatura or settings.camara_legislatura_id
        anos = args.anos or settings.anos_m2()
        tipos = _parse_tipos(args.tipos, settings)
        client = ArquivosCamaraClient(cache_dir=settings.camara_cache_dir)
        arquivos = client.localizar_m2_no_cache(anos)
        with _criar_driver(settings) as driver:
            loader = Neo4jLoaderM2(driver)
            resumo = loader.carregar(
                arquivos_por_ano=arquivos,
                legislatura_id=leg_id,
                tipos=tipos,
                minimo_coautorias=args.min_coautorias,
                limpar_antes=args.limpar_m2,
            )
            saida = resumo.to_dict()
            saida["origem"] = "cache"
        if args.salvar_medicao:
            _salvar_json(args.salvar_medicao, saida)
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "coletar-m3":
        anos = args.anos or settings.anos_m2()
        arquivos, dls, hits = asyncio.run(_executar_coleta_m3(settings, anos, args.forcar))
        print(
            json.dumps(
                {
                    "anos": anos,
                    "arquivos": [
                        {
                            "ano": a.ano,
                            "votacoes": str(a.votacoes),
                            "votos": str(a.votos),
                            "orientacoes": str(a.orientacoes),
                            "objetos": str(a.objetos),
                        }
                        for a in arquivos
                    ],
                    "downloads_rede": dls,
                    "hits_cache": hits,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.comando == "carregar-m3":
        leg_id = args.legislatura or settings.camara_legislatura_id
        anos = args.anos or settings.anos_m2()
        arquivos, dls, hits = asyncio.run(
            _executar_coleta_m3(settings, anos, args.forcar_coleta)
        )
        with _criar_driver(settings) as driver:
            loader = Neo4jLoaderM3(driver)
            resumo = loader.carregar(
                arquivos_por_ano=arquivos,
                legislatura_id=leg_id,
                minimo_votacoes=args.min_votacoes,
                limpar_antes=args.limpar_m3,
            )
            saida = resumo.to_dict()
            saida["downloads_rede"] = dls
            saida["hits_cache"] = hits
        if args.salvar_medicao:
            _salvar_json(args.salvar_medicao, saida)
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "recarregar-m3":
        leg_id = args.legislatura or settings.camara_legislatura_id
        anos = args.anos or settings.anos_m2()
        client = ArquivosCamaraClient(cache_dir=settings.camara_cache_dir)
        arquivos = client.localizar_m3_no_cache(anos)
        with _criar_driver(settings) as driver:
            loader = Neo4jLoaderM3(driver)
            resumo = loader.carregar(
                arquivos_por_ano=arquivos,
                legislatura_id=leg_id,
                minimo_votacoes=args.min_votacoes,
                limpar_antes=args.limpar_m3,
            )
            saida = resumo.to_dict()
            saida["origem"] = "cache"
        if args.salvar_medicao:
            _salvar_json(args.salvar_medicao, saida)
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "analisar-m5":
        leg_id = args.legislatura or settings.camara_legislatura_id
        with _criar_driver(settings) as driver:
            resumo = executar_analises_m5(
                driver=driver,
                legislatura_id=leg_id,
                min_votacoes_comum=args.min_votacoes,
                min_concordancia=args.min_concordancia,
                min_coautorias=args.min_coautorias,
            )
            saida = resumo.to_dict()
        if args.salvar_medicao:
            _salvar_json(args.salvar_medicao, saida)
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "medir-m6":
        saida = asyncio.run(
            executar_benchmark_m6(
                base_url=args.url,
                repeticoes=args.repeticoes,
                concorrencia=args.concorrencia,
                votacao_id=args.votacao_id,
            )
        )
        if args.salvar_json:
            _salvar_json(args.salvar_json, saida)
        if args.salvar_csv:
            salvar_benchmark_csv(args.salvar_csv, saida)
        print(json.dumps(saida, ensure_ascii=False, indent=2))
        return 0

    if args.comando == "carregar-gastos":
        anos = args.anos or settings.anos_m2()
        with _criar_driver(settings) as driver:
            resumo = carregar_gastos_e_equipe(
                driver=driver,
                cache_dir=settings.camara_cache_dir,
                anos=anos,
                user_agent=settings.camara_user_agent,
                forcar_download=args.forcar,
            )
        print(json.dumps(resumo.to_dict(), ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
