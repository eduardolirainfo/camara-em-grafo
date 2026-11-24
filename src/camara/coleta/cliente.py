"""Cliente HTTP assíncrono para a API de Dados Abertos da Câmara (v2)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Self
from urllib.parse import parse_qs, urlparse

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from camara.coleta.cache import DiskCache

logger = logging.getLogger(__name__)


class RetryableAPIError(Exception):
    """Erro transitório (HTTP 429 ou 5xx) elegível para nova tentativa com backoff."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} em {url}")


class CamaraAPIClient:
    """Cliente gentil para a API v2 da Câmara: concorrência limitada, pausa, backoff e cache."""

    def __init__(
        self,
        base_url: str = "https://dadosabertos.camara.leg.br/api/v2",
        cache: DiskCache | None = None,
        user_agent: str = "camara-em-grafo/0.1 (projeto de estudo de dados abertos)",
        max_concorrencia: int = 2,
        pausa_segundos: float = 0.3,
        timeout_segundos: float = 30.0,
        max_tentativas: int = 5,
        backoff_min: float = 0.5,
        backoff_max: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.pausa_segundos = pausa_segundos
        self.max_tentativas = max_tentativas
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self._semaphore = asyncio.Semaphore(max_concorrencia)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent,
            },
            timeout=timeout_segundos,
            follow_redirects=True,
        )
        self.requests_rede: int = 0
        self.hits_cache: int = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_json(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Busca um endpoint JSON da API com cache em disco e retry em 429/5xx."""
        norm_endpoint = "/" + endpoint.lstrip("/")
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        if use_cache and self.cache is not None:
            cached = self.cache.get(norm_endpoint, clean_params or None)
            if cached is not None:
                self.hits_cache += 1
                return cached

        async with self._semaphore:
            payload = await self._request_with_retry(norm_endpoint, clean_params)

        if self.cache is not None:
            self.cache.set(norm_endpoint, payload, clean_params or None)

        return payload

    async def _request_with_retry(
        self,
        endpoint: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((RetryableAPIError, httpx.TransportError)),
            stop=stop_after_attempt(self.max_tentativas),
            wait=wait_exponential(multiplier=self.backoff_min, min=self.backoff_min, max=self.backoff_max),
            reraise=True,
        ):
            with attempt:
                self.requests_rede += 1
                response = await self._client.get(endpoint, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "Resposta transitória %s em %s (tentativa %s/%s)",
                        response.status_code,
                        response.request.url,
                        attempt.retry_state.attempt_number,
                        self.max_tentativas,
                    )
                    raise RetryableAPIError(response.status_code, str(response.request.url))
                response.raise_for_status()
                return response.json()
        raise RuntimeError("Loop de retry encerrado sem retorno.")

    async def get_paginated(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        itens: int = 100,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """Percorre todas as páginas seguindo os links rel='next' da API da Câmara."""
        query: dict[str, Any] = dict(params or {})
        query.setdefault("itens", itens)
        query.setdefault("pagina", 1)

        resultados: list[dict[str, Any]] = []
        paginas_visitadas: set[int] = set()

        while True:
            pagina_atual = int(query["pagina"])
            if pagina_atual in paginas_visitadas:
                break
            paginas_visitadas.add(pagina_atual)

            payload = await self.get_json(endpoint, params=query, use_cache=use_cache)
            dados = payload.get("dados", [])
            if isinstance(dados, list):
                resultados.extend(dados)

            next_href = _find_next_link(payload.get("links", []))
            if not next_href:
                break

            next_page = _extract_page_number(next_href)
            if next_page is None or next_page in paginas_visitadas:
                break

            query["pagina"] = next_page
            if self.pausa_segundos > 0:
                await asyncio.sleep(self.pausa_segundos)

        return resultados


def _find_next_link(links: list[dict[str, Any]]) -> str | None:
    for link in links:
        if link.get("rel") == "next" and link.get("href"):
            return str(link["href"])
    return None


def _extract_page_number(href: str) -> int | None:
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    raw = qs.get("pagina")
    if not raw:
        return None
    try:
        return int(raw[0])
    except ValueError:
        return None
