"""Coleta do Marco 1 (M1): legislatura atual, partidos e deputados."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from camara.coleta.cache import DiskCache
from camara.coleta.cliente import CamaraAPIClient


@dataclass(frozen=True)
class DadosM1:
    legislatura: dict[str, Any]
    partidos: list[dict[str, Any]]
    deputados: list[dict[str, Any]]
    coletado_em: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "coletado_em": self.coletado_em,
            "legislatura": self.legislatura,
            "partidos": self.partidos,
            "deputados": self.deputados,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DadosM1:
        return cls(
            legislatura=raw["legislatura"],
            partidos=raw["partidos"],
            deputados=raw["deputados"],
            coletado_em=raw.get("coletado_em", ""),
        )


def bundle_name_m1(legislatura_id: int) -> str:
    return f"m1_legislatura_{legislatura_id}"


async def coletar_m1(
    client: CamaraAPIClient,
    legislatura_id: int,
    itens_por_pagina: int = 100,
    use_cache: bool = True,
) -> DadosM1:
    """Coleta legislatura, partidos e deputados da legislatura informada."""
    leg_resp = await client.get_json(f"/legislaturas/{legislatura_id}", use_cache=use_cache)
    legislatura = leg_resp["dados"]

    partidos = await client.get_paginated(
        "/partidos",
        params={"idLegislatura": legislatura_id},
        itens=itens_por_pagina,
        use_cache=use_cache,
    )

    deputados = await client.get_paginated(
        "/deputados",
        params={"idLegislatura": legislatura_id},
        itens=itens_por_pagina,
        use_cache=use_cache,
    )

    dados = DadosM1(
        legislatura=legislatura,
        partidos=partidos,
        deputados=deputados,
        coletado_em=datetime.now(UTC).isoformat(),
    )

    if client.cache is not None:
        client.cache.save_bundle(bundle_name_m1(legislatura_id), dados.to_dict())

    return dados


def carregar_m1_do_cache(cache: DiskCache, legislatura_id: int) -> DadosM1:
    """Lê o bundle M1 previamente salvo no cache em disco."""
    raw = cache.load_bundle(bundle_name_m1(legislatura_id))
    return DadosM1.from_dict(raw)
