"""Testes do coletor de arquivos em lote CSV do M2 (sem rede, via respx)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from camara.coleta.arquivos import ArquivosCamaraClient, extrair_id_de_uri, ler_csv_camara

BASE_ARQUIVOS = "https://dadosabertos.camara.leg.br/arquivos"

CSV_PROPOSICOES = (
    '\ufeff"id";"uri";"siglaTipo";"numero";"ano";"ementa";"dataApresentacao"\n'
    '"1001";"https://dadosabertos.camara.leg.br/api/v2/proposicoes/1001";"PL";"10";"2025";"Projeto sobre saúde";"2025-02-10T10:00:00"\n'
    '"1002";"https://dadosabertos.camara.leg.br/api/v2/proposicoes/1002";"REQ";"50";"2025";"Requerimento de urgência";"2025-02-11T11:00:00"\n'
)

CSV_AUTORES = (
    '"idProposicao";"idDeputadoAutor";"tipoAutor";"ordemAssinatura";"proponente"\n'
    '"1001";"220593";"Deputado(a)";"1";"1"\n'
    '"1001";"100001";"Deputado(a)";"2";"0"\n'
    '"1001";"";"Poder Executivo";"3";"0"\n'
)

CSV_TEMAS = (
    '"uriProposicao";"siglaTipo";"numero";"ano";"codTema";"tema";"relevancia"\n'
    '"https://dadosabertos.camara.leg.br/api/v2/proposicoes/1001";"PL";"10";"2025";"56";"Saúde";"0"\n'
)


@pytest.mark.asyncio
async def test_download_arquivos_m2_com_retry_e_cache(tmp_path: Path) -> None:
    with respx.mock(base_url=BASE_ARQUIVOS) as router:
        r_prop = router.get("/proposicoes/csv/proposicoes-2025.csv").mock(
            side_effect=[
                httpx.Response(503, text="indisponível"),
                httpx.Response(200, text=CSV_PROPOSICOES),
            ]
        )
        r_aut = router.get("/proposicoesAutores/csv/proposicoesAutores-2025.csv").mock(
            return_value=httpx.Response(200, text=CSV_AUTORES)
        )
        r_tem = router.get("/proposicoesTemas/csv/proposicoesTemas-2025.csv").mock(
            return_value=httpx.Response(200, text=CSV_TEMAS)
        )

        async with ArquivosCamaraClient(
            cache_dir=tmp_path,
            base_url=BASE_ARQUIVOS,
            pausa_segundos=0.0,
            backoff_min=0.01,
            backoff_max=0.02,
        ) as client:
            arquivos = await client.coletar_m2(anos=[2025])
            assert len(arquivos) == 1
            assert client.downloads_rede == 4  # 1 falha 503 + 3 sucessos
            assert client.hits_cache == 0

            # Segunda chamada deve ler 100% do cache sem novas requisições
            arquivos_cache = await client.coletar_m2(anos=[2025])
            assert arquivos_cache == arquivos
            assert client.downloads_rede == 4
            assert client.hits_cache == 3

            # localizar_m2_no_cache funciona sem rede
            assert client.localizar_m2_no_cache([2025]) == arquivos

        assert r_prop.call_count == 2
        assert r_aut.call_count == 1
        assert r_tem.call_count == 1

    linhas = list(ler_csv_camara(arquivos[0].proposicoes))
    assert len(linhas) == 2
    assert linhas[0]["id"] == "1001"
    assert linhas[0]["siglaTipo"] == "PL"


def test_extrair_id_de_uri() -> None:
    assert extrair_id_de_uri("https://dadosabertos.camara.leg.br/api/v2/proposicoes/281460") == 281460
    assert extrair_id_de_uri("https://dadosabertos.camara.leg.br/api/v2/proposicoes/281460/") == 281460
    assert extrair_id_de_uri("") is None
    assert extrair_id_de_uri(None) is None
