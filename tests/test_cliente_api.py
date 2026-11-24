"""Testes do cliente assíncrono da API da Câmara (sem rede, via respx)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fixtures_m1 import (
    DEPUTADOS_PAGINA_1,
    DEPUTADOS_PAGINA_2,
    LEGISLATURA_57,
    PARTIDOS_PAGINA_1,
)

from camara.coleta.cache import DiskCache
from camara.coleta.cliente import CamaraAPIClient, RetryableAPIError
from camara.coleta.m1 import carregar_m1_do_cache, coletar_m1

BASE_URL = "https://dadosabertos.camara.leg.br/api/v2"


@pytest.mark.asyncio
async def test_paginacao_e_cache_em_disco(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache")

    with respx.mock(base_url=BASE_URL) as router:
        route_p1 = router.get(
            "/deputados", params={"idLegislatura": "57", "itens": "2", "pagina": "1"}
        ).mock(return_value=httpx.Response(200, json=DEPUTADOS_PAGINA_1))
        route_p2 = router.get(
            "/deputados", params={"idLegislatura": "57", "itens": "2", "pagina": "2"}
        ).mock(return_value=httpx.Response(200, json=DEPUTADOS_PAGINA_2))

        async with CamaraAPIClient(
            base_url=BASE_URL,
            cache=cache,
            pausa_segundos=0.0,
        ) as client:
            primeira = await client.get_paginated(
                "/deputados", params={"idLegislatura": 57}, itens=2
            )
            assert len(primeira) == 4
            assert client.requests_rede == 2
            assert client.hits_cache == 0

            # Segunda chamada deve vir 100% do cache em disco
            segunda = await client.get_paginated(
                "/deputados", params={"idLegislatura": 57}, itens=2
            )
            assert segunda == primeira
            assert client.requests_rede == 2
            assert client.hits_cache == 2

        assert route_p1.call_count == 1
        assert route_p2.call_count == 1
        req = route_p1.calls[0].request
        assert req.headers["accept"] == "application/json"
        assert "camara-em-grafo" in req.headers["user-agent"]


@pytest.mark.asyncio
async def test_retry_em_429_e_503() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get("/legislaturas/57").mock(
            side_effect=[
                httpx.Response(429, json={"detail": "rate limit"}),
                httpx.Response(503, json={"detail": "temporariamente indisponível"}),
                httpx.Response(200, json=LEGISLATURA_57),
            ]
        )

        async with CamaraAPIClient(
            base_url=BASE_URL,
            pausa_segundos=0.0,
            max_tentativas=4,
            backoff_min=0.01,
            backoff_max=0.02,
        ) as client:
            payload = await client.get_json("/legislaturas/57", use_cache=False)

        assert payload["dados"]["id"] == 57
        assert route.call_count == 3


@pytest.mark.asyncio
async def test_desiste_apos_limite_de_tentativas() -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get("/legislaturas/57").mock(
            return_value=httpx.Response(500, text="erro interno")
        )

        async with CamaraAPIClient(
            base_url=BASE_URL,
            pausa_segundos=0.0,
            max_tentativas=3,
            backoff_min=0.01,
            backoff_max=0.02,
        ) as client:
            with pytest.raises(RetryableAPIError):
                await client.get_json("/legislaturas/57", use_cache=False)

        assert route.call_count == 3


@pytest.mark.asyncio
async def test_coletar_m1_e_recarregar_bundle_do_cache(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "cache")

    with respx.mock(base_url=BASE_URL) as router:
        router.get("/legislaturas/57").mock(
            return_value=httpx.Response(200, json=LEGISLATURA_57)
        )
        router.get(
            "/partidos", params={"idLegislatura": "57", "itens": "2", "pagina": "1"}
        ).mock(return_value=httpx.Response(200, json=PARTIDOS_PAGINA_1))
        router.get(
            "/deputados", params={"idLegislatura": "57", "itens": "2", "pagina": "1"}
        ).mock(return_value=httpx.Response(200, json=DEPUTADOS_PAGINA_1))
        router.get(
            "/deputados", params={"idLegislatura": "57", "itens": "2", "pagina": "2"}
        ).mock(return_value=httpx.Response(200, json=DEPUTADOS_PAGINA_2))

        async with CamaraAPIClient(
            base_url=BASE_URL,
            cache=cache,
            pausa_segundos=0.0,
        ) as client:
            dados = await coletar_m1(client, legislatura_id=57, itens_por_pagina=2)

    assert dados.legislatura["id"] == 57
    assert len(dados.partidos) == 2
    assert len(dados.deputados) == 4

    recarregado = carregar_m1_do_cache(cache, legislatura_id=57)
    assert recarregado.legislatura == dados.legislatura
    assert recarregado.partidos == dados.partidos
    assert recarregado.deputados == dados.deputados
