"""Benchmark reproduzível da API GraphQL vs rotas REST equivalentes (Marco 6)."""

from __future__ import annotations

import asyncio
import csv
import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class CenarioBenchmark:
    nome: str
    protocolo: str  # "graphql" | "rest"
    metodo: str  # "POST" | "GET"
    caminho: str
    payload: dict[str, Any] | None = None


@dataclass
class ResultadoCenario:
    nome: str
    protocolo: str
    requisicoes: int
    sucessos: int
    erros: int
    concorrencia: int
    media_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    req_por_seg: float
    payload_bytes_medio: float


def percentil(valores_ordenados: list[float], p: float) -> float:
    if not valores_ordenados:
        return 0.0
    if len(valores_ordenados) == 1:
        return round(valores_ordenados[0], 2)
    k = (len(valores_ordenados) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(valores_ordenados[int(k)], 2)
    val = valores_ordenados[f] * (c - k) + valores_ordenados[c] * (k - f)
    return round(val, 2)


def cenarios_padrao_m6(votacao_id: str = "1197773-90") -> list[CenarioBenchmark]:
    return [
        CenarioBenchmark(
            nome="deputado_perfil",
            protocolo="graphql",
            metodo="POST",
            caminho="/graphql",
            payload={
                "query": """
                query {
                  deputado(id: "204501") {
                    id nome uf urlFoto uri
                    partidos { sigla nome }
                    coautores(primeiro: 3) {
                      proposicoesEmComum
                      deputado { id nome uf }
                    }
                    votaComo(primeiro: 3, minimoVotacoes: 100) {
                      concordancia votacoesEmComum
                      deputado { id nome uf }
                    }
                    contraOrientacao {
                      partido { sigla }
                      contra totalOrientadas taxaDivergencia
                    }
                  }
                }
                """
            },
        ),
        CenarioBenchmark(
            nome="deputado_perfil",
            protocolo="rest",
            metodo="GET",
            caminho="/rest/deputados/204501?primeiro_coautores=3&primeiro_vota_como=3&minimo_votacoes=100",
        ),
        CenarioBenchmark(
            nome="deputados_sp_lista_10",
            protocolo="graphql",
            metodo="POST",
            caminho="/graphql",
            payload={
                "query": """
                query {
                  deputados(uf: "SP", primeiro: 10) {
                    edges {
                      node {
                        id nome uf
                        partidos { sigla }
                        coautores(primeiro: 3) {
                          proposicoesEmComum
                          deputado { id nome uf }
                        }
                        votaComo(primeiro: 3, minimoVotacoes: 100) {
                          concordancia votacoesEmComum
                          deputado { id nome uf }
                        }
                      }
                    }
                  }
                }
                """
            },
        ),
        CenarioBenchmark(
            nome="deputados_sp_lista_10",
            protocolo="rest",
            metodo="GET",
            caminho="/rest/deputados?uf=SP&primeiro=10&primeiro_coautores=3&primeiro_vota_como=3&minimo_votacoes=100",
        ),
        CenarioBenchmark(
            nome="proposicoes_fulltext_10",
            protocolo="graphql",
            metodo="POST",
            caminho="/graphql",
            payload={
                "query": """
                query {
                  proposicoes(texto: "inteligência artificial", primeiro: 10) {
                    edges {
                      node {
                        id siglaTipo numero ano ementa
                        temas { cod nome }
                        autores(primeiro: 3) { id nome uf }
                      }
                    }
                  }
                }
                """
            },
        ),
        CenarioBenchmark(
            nome="proposicoes_fulltext_10",
            protocolo="rest",
            metodo="GET",
            caminho="/rest/proposicoes?texto=intelig%C3%AAncia+artificial&primeiro=10&primeiro_autores=3",
        ),
        CenarioBenchmark(
            nome="caminho_coautoria",
            protocolo="graphql",
            metodo="POST",
            caminho="/graphql",
            payload={
                "query": """
                query {
                  caminho(deputadoA: "220593", deputadoB: "204501") {
                    id nome uf partidos { sigla }
                  }
                }
                """
            },
        ),
        CenarioBenchmark(
            nome="votacao_detalhe",
            protocolo="graphql",
            metodo="POST",
            caminho="/graphql",
            payload={
                "query": f"""
                query {{
                  votacao(id: "{votacao_id}") {{
                    id data siglaOrgao descricao aprovacao votosSim votosNao
                    proposicoes {{ id siglaTipo numero ano ementa }}
                    orientacoes {{ partido {{ sigla }} orientacao siglaBancada }}
                  }}
                }}
                """
            },
        ),
    ]


async def executar_cenario(
    client: httpx.AsyncClient,
    cenario: CenarioBenchmark,
    repeticoes: int,
    concorrencia: int,
    warmup: int = 2,
) -> ResultadoCenario:
    # Aquecimento (não entra nas estatísticas)
    for _ in range(warmup):
        if cenario.metodo == "POST":
            await client.post(cenario.caminho, json=cenario.payload)
        else:
            await client.get(cenario.caminho)

    semaforo = asyncio.Semaphore(concorrencia)
    latencias: list[float] = []
    tamanhos: list[int] = []
    erros = 0

    async def _uma_chamada() -> None:
        nonlocal erros
        async with semaforo:
            t0 = time.perf_counter()
            try:
                if cenario.metodo == "POST":
                    resp = await client.post(cenario.caminho, json=cenario.payload)
                else:
                    resp = await client.get(cenario.caminho)
                ms = (time.perf_counter() - t0) * 1000.0
                if resp.status_code != 200:
                    erros += 1
                    return
                if cenario.protocolo == "graphql":
                    body = resp.json()
                    if body.get("errors"):
                        erros += 1
                        return
                latencias.append(ms)
                tamanhos.append(len(resp.content))
            except httpx.HTTPError:
                erros += 1

    t_ini = time.perf_counter()
    await asyncio.gather(*(_uma_chamada() for _ in range(repeticoes)))
    duracao = max(time.perf_counter() - t_ini, 1e-6)

    latencias.sort()
    sucessos = len(latencias)
    return ResultadoCenario(
        nome=cenario.nome,
        protocolo=cenario.protocolo,
        requisicoes=repeticoes,
        sucessos=sucessos,
        erros=erros,
        concorrencia=concorrencia,
        media_ms=round(sum(latencias) / sucessos, 2) if sucessos else 0.0,
        p50_ms=percentil(latencias, 50),
        p95_ms=percentil(latencias, 95),
        p99_ms=percentil(latencias, 99),
        min_ms=round(latencias[0], 2) if sucessos else 0.0,
        max_ms=round(latencias[-1], 2) if sucessos else 0.0,
        req_por_seg=round(sucessos / duracao, 2),
        payload_bytes_medio=round(sum(tamanhos) / sucessos, 1) if sucessos else 0.0,
    )


async def executar_benchmark_m6(
    base_url: str,
    repeticoes: int = 30,
    concorrencia: int = 5,
    votacao_id: str = "1197773-90",
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    cenarios = cenarios_padrao_m6(votacao_id=votacao_id)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0)
    try:
        resultados: list[ResultadoCenario] = []
        for cen in cenarios:
            res = await executar_cenario(
                client=client,
                cenario=cen,
                repeticoes=repeticoes,
                concorrencia=concorrencia,
            )
            resultados.append(res)
    finally:
        if owns_client:
            await client.aclose()

    return {
        "executado_em": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "repeticoes": repeticoes,
        "concorrencia": concorrencia,
        "votacao_id": votacao_id,
        "resultados": [asdict(r) for r in resultados],
    }


def salvar_benchmark_csv(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    linhas = payload["resultados"]
    if not linhas:
        return
    campos = list(linhas[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=campos)
        writer.writeheader()
        writer.writerows(linhas)
