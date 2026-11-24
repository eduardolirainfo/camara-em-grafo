"""Download com cache em disco e leitura em streaming dos arquivos em lote da Câmara (CSV)."""

from __future__ import annotations

import asyncio
import csv
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from camara.coleta.cliente import RetryableAPIError

logger = logging.getLogger(__name__)

RECURSOS_M2: tuple[str, ...] = (
    "proposicoes",
    "proposicoesAutores",
    "proposicoesTemas",
)

RECURSOS_M3: tuple[str, ...] = (
    "votacoes",
    "votacoesVotos",
    "votacoesOrientacoes",
    "votacoesProposicoes",
)


@dataclass(frozen=True)
class ArquivosAnoM2:
    ano: int
    proposicoes: Path
    autores: Path
    temas: Path


@dataclass(frozen=True)
class ArquivosAnoM3:
    ano: int
    votacoes: Path
    votos: Path
    orientacoes: Path
    objetos: Path


class ArquivosCamaraClient:
    """Baixa arquivos anuais CSV de dadosabertos.camara.leg.br/arquivos com cache em disco."""

    def __init__(
        self,
        cache_dir: Path,
        base_url: str = "https://dadosabertos.camara.leg.br/arquivos",
        user_agent: str = "camara-em-grafo/0.1 (projeto de estudo de dados abertos)",
        max_concorrencia: int = 2,
        pausa_segundos: float = 0.3,
        timeout_segundos: float = 180.0,
        max_tentativas: int = 5,
        backoff_min: float = 1.0,
        backoff_max: float = 20.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) / "arquivos"
        self.base_url = base_url.rstrip("/")
        self.pausa_segundos = pausa_segundos
        self.max_tentativas = max_tentativas
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self._semaphore = asyncio.Semaphore(max_concorrencia)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={"User-Agent": user_agent},
            timeout=timeout_segundos,
            follow_redirects=True,
        )
        self.downloads_rede: int = 0
        self.hits_cache: int = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def caminho_local(self, recurso: str, ano: int) -> Path:
        return self.cache_dir / recurso / f"{recurso}-{ano}.csv"

    def url_relativa(self, recurso: str, ano: int) -> str:
        return f"/{recurso}/csv/{recurso}-{ano}.csv"

    async def baixar_csv(self, recurso: str, ano: int, use_cache: bool = True) -> Path:
        destino = self.caminho_local(recurso, ano)
        if use_cache and destino.exists() and destino.stat().st_size > 0:
            self.hits_cache += 1
            return destino

        destino.parent.mkdir(parents=True, exist_ok=True)
        rel_url = self.url_relativa(recurso, ano)

        async with self._semaphore:
            await self._download_with_retry(rel_url, destino)
            if self.pausa_segundos > 0:
                await asyncio.sleep(self.pausa_segundos)

        return destino

    async def _download_with_retry(self, rel_url: str, destino: Path) -> None:
        tmp = destino.with_suffix(".csv.tmp")
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((RetryableAPIError, httpx.TransportError)),
            stop=stop_after_attempt(self.max_tentativas),
            wait=wait_exponential(
                multiplier=self.backoff_min, min=self.backoff_min, max=self.backoff_max
            ),
            reraise=True,
        ):
            with attempt:
                self.downloads_rede += 1
                logger.info("Baixando arquivo em lote: %s%s", self.base_url, rel_url)
                async with self._client.stream("GET", rel_url) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise RetryableAPIError(response.status_code, str(response.request.url))
                    response.raise_for_status()
                    with tmp.open("wb") as fh:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            fh.write(chunk)
                tmp.replace(destino)
                return

    async def coletar_m2(
        self,
        anos: list[int],
        use_cache: bool = True,
    ) -> list[ArquivosAnoM2]:
        resultados: list[ArquivosAnoM2] = []
        for ano in anos:
            p_prop = await self.baixar_csv("proposicoes", ano, use_cache=use_cache)
            p_aut = await self.baixar_csv("proposicoesAutores", ano, use_cache=use_cache)
            p_tem = await self.baixar_csv("proposicoesTemas", ano, use_cache=use_cache)
            resultados.append(
                ArquivosAnoM2(ano=ano, proposicoes=p_prop, autores=p_aut, temas=p_tem)
            )
        return resultados

    def localizar_m2_no_cache(self, anos: list[int]) -> list[ArquivosAnoM2]:
        resultados: list[ArquivosAnoM2] = []
        for ano in anos:
            p_prop = self.caminho_local("proposicoes", ano)
            p_aut = self.caminho_local("proposicoesAutores", ano)
            p_tem = self.caminho_local("proposicoesTemas", ano)
            faltando = [str(p) for p in (p_prop, p_aut, p_tem) if not p.exists()]
            if faltando:
                raise FileNotFoundError(
                    f"Arquivos do M2 ausentes no cache para {ano}: {', '.join(faltando)}. "
                    "Execute 'camara coletar-m2' antes de recarregar do cache."
                )
            resultados.append(
                ArquivosAnoM2(ano=ano, proposicoes=p_prop, autores=p_aut, temas=p_tem)
            )
        return resultados

    async def coletar_m3(
        self,
        anos: list[int],
        use_cache: bool = True,
    ) -> list[ArquivosAnoM3]:
        resultados: list[ArquivosAnoM3] = []
        for ano in anos:
            p_vot = await self.baixar_csv("votacoes", ano, use_cache=use_cache)
            p_votos = await self.baixar_csv("votacoesVotos", ano, use_cache=use_cache)
            p_ori = await self.baixar_csv("votacoesOrientacoes", ano, use_cache=use_cache)
            p_obj = await self.baixar_csv("votacoesProposicoes", ano, use_cache=use_cache)
            resultados.append(
                ArquivosAnoM3(
                    ano=ano,
                    votacoes=p_vot,
                    votos=p_votos,
                    orientacoes=p_ori,
                    objetos=p_obj,
                )
            )
        return resultados

    def localizar_m3_no_cache(self, anos: list[int]) -> list[ArquivosAnoM3]:
        resultados: list[ArquivosAnoM3] = []
        for ano in anos:
            p_vot = self.caminho_local("votacoes", ano)
            p_votos = self.caminho_local("votacoesVotos", ano)
            p_ori = self.caminho_local("votacoesOrientacoes", ano)
            p_obj = self.caminho_local("votacoesProposicoes", ano)
            faltando = [str(p) for p in (p_vot, p_votos, p_ori, p_obj) if not p.exists()]
            if faltando:
                raise FileNotFoundError(
                    f"Arquivos do M3 ausentes no cache para {ano}: {', '.join(faltando)}. "
                    "Execute 'camara coletar-m3' antes de recarregar do cache."
                )
            resultados.append(
                ArquivosAnoM3(
                    ano=ano,
                    votacoes=p_vot,
                    votos=p_votos,
                    orientacoes=p_ori,
                    objetos=p_obj,
                )
            )
        return resultados


def ler_csv_camara(path: Path) -> Iterator[dict[str, Any]]:
    """Lê um arquivo CSV da Câmara (UTF-8 com possível BOM, separador ';') em streaming."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        yield from reader


def extrair_id_de_uri(uri: str | None) -> int | None:
    if not uri:
        return None
    parte = uri.strip().rstrip("/").split("/")[-1]
    try:
        return int(parte)
    except ValueError:
        return None
