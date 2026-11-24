"""Carga idempotente dos gastos consolidados da Cota Parlamentar (CEAP) e equipe de gabinete."""

from __future__ import annotations

import csv
import io
import logging
import time
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from neo4j import Driver

logger = logging.getLogger(__name__)

CATEGORIAS_VIAGEM: set[str] = {
    "PASSAGEM AÉREA - MISSÃO OFICIAL",
    "PASSAGEM AÉREA - REEMBOLSO",
    "PASSAGEM AÉREA - RPA",
    "PASSAGENS TERRESTRES, MARÍTIMAS OU FLUVIAIS",
    "HOSPEDAGEM ,EXCETO DO PARLAMENTAR NO DISTRITO FEDERAL.",
    "LOCAÇÃO OU FRETAMENTO DE AERONAVES",
    "LOCAÇÃO OU FRETAMENTO DE EMBARCAÇÕES",
    "LOCAÇÃO OU FRETAMENTO DE VEÍCULOS AUTOMOTORES",
    "COMBUSTÍVEIS E LUBRIFICANTES.",
    "SERVIÇO DE TÁXI, PEDÁGIO E ESTACIONAMENTO",
}

CATEGORIA_DIVULGACAO = "DIVULGAÇÃO DA ATIVIDADE PARLAMENTAR."

CYPHER_ATUALIZAR_GASTOS = """
UNWIND $linhas AS row
MATCH (d:Deputado {id: row.id})
SET d.gastoTotalCeap = row.gastoTotalCeap,
    d.gastoViagens = row.gastoViagens,
    d.gastoDivulgacao = row.gastoDivulgacao,
    d.qtdNotasCeap = row.qtdNotasCeap,
    d.numFuncionarios = row.numFuncionarios,
    d.anosCeap = $anos
"""


@dataclass
class ResumoCargaGastos:
    anos: list[int]
    notas_lidas: int
    deputados_atualizados: int
    total_geral_ceap: float
    total_viagens: float
    total_divulgacao: float
    tempo_segundos: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_float(val: str | None) -> float:
    if not val:
        return 0.0
    s = val.strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def baixar_cotas_zip(
    cache_dir: Path,
    anos: list[int],
    user_agent: str = "camara-em-grafo/0.1 (projeto de estudo de dados abertos)",
    forcar: bool = False,
) -> list[Path]:
    pasta = Path(cache_dir) / "arquivos" / "cotas"
    pasta.mkdir(parents=True, exist_ok=True)
    arquivos: list[Path] = []

    with httpx.Client(
        headers={"User-Agent": user_agent}, timeout=120.0, follow_redirects=True
    ) as client:
        for ano in anos:
            dest = pasta / f"Ano-{ano}.csv.zip"
            if not forcar and dest.exists() and dest.stat().st_size > 0:
                arquivos.append(dest)
                continue
            url = f"https://www.camara.leg.br/cotas/Ano-{ano}.csv.zip"
            logger.info("Baixando CEAP em lote: %s", url)
            resp = client.get(url)
            resp.raise_for_status()
            tmp = dest.with_suffix(".zip.tmp")
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
            arquivos.append(dest)
    return arquivos


def contar_funcionarios_por_deputado(cache_dir: Path) -> dict[int, int]:
    path = Path(cache_dir) / "arquivos" / "funcionarios" / "funcionarios.csv"
    if not path.exists():
        return {}
    contagem: dict[int, int] = defaultdict(int)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            uri = (row.get("uriLotacao") or "").strip()
            if "/deputados/" not in uri:
                continue
            parte = uri.rstrip("/").split("/")[-1]
            if parte.isdigit():
                contagem[int(parte)] += 1
    return dict(contagem)


def carregar_gastos_e_equipe(
    driver: Driver,
    cache_dir: Path,
    anos: list[int],
    user_agent: str = "camara-em-grafo/0.1 (projeto de estudo de dados abertos)",
    forcar_download: bool = False,
) -> ResumoCargaGastos:
    inicio = time.perf_counter()
    zips = baixar_cotas_zip(cache_dir, anos, user_agent=user_agent, forcar=forcar_download)
    func_por_dep = contar_funcionarios_por_deputado(cache_dir)

    totais: dict[int, dict[str, Any]] = {}
    notas_lidas = 0

    for zp in zips:
        with zipfile.ZipFile(zp) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"), delimiter=";")
                for row in reader:
                    notas_lidas += 1
                    dep_str = (row.get("ideCadastro") or "").strip()
                    if not dep_str.isdigit():
                        continue
                    dep_id = int(dep_str)
                    val = _to_float(row.get("vlrLiquido"))
                    desc = (row.get("txtDescricao") or "").strip().upper()

                    acc = totais.get(dep_id)
                    if acc is None:
                        acc = {
                            "id": dep_id,
                            "gastoTotalCeap": 0.0,
                            "gastoViagens": 0.0,
                            "gastoDivulgacao": 0.0,
                            "qtdNotasCeap": 0,
                        }
                        totais[dep_id] = acc

                    acc["gastoTotalCeap"] += val
                    acc["qtdNotasCeap"] += 1
                    if desc in CATEGORIAS_VIAGEM:
                        acc["gastoViagens"] += val
                    elif desc == CATEGORIA_DIVULGACAO:
                        acc["gastoDivulgacao"] += val

    # Garante também deputados que tenham funcionários mesmo sem notas CEAP
    todos_ids = set(totais.keys()) | set(func_por_dep.keys())
    linhas = []
    for dep_id in todos_ids:
        acc = totais.get(
            dep_id,
            {
                "id": dep_id,
                "gastoTotalCeap": 0.0,
                "gastoViagens": 0.0,
                "gastoDivulgacao": 0.0,
                "qtdNotasCeap": 0,
            },
        )
        linhas.append(
            {
                "id": dep_id,
                "gastoTotalCeap": round(acc["gastoTotalCeap"], 2),
                "gastoViagens": round(acc["gastoViagens"], 2),
                "gastoDivulgacao": round(acc["gastoDivulgacao"], 2),
                "qtdNotasCeap": int(acc["qtdNotasCeap"]),
                "numFuncionarios": int(func_por_dep.get(dep_id, 0)),
            }
        )

    with driver.session() as session:
        session.execute_write(
            lambda tx: tx.run(CYPHER_ATUALIZAR_GASTOS, linhas=linhas, anos=anos).consume()
        )
        atualizados = int(
            session.run(
                "MATCH (d:Deputado) WHERE d.gastoTotalCeap IS NOT NULL RETURN count(d) AS n"
            ).single()["n"]
        )

    return ResumoCargaGastos(
        anos=anos,
        notas_lidas=notas_lidas,
        deputados_atualizados=atualizados,
        total_geral_ceap=round(sum(x["gastoTotalCeap"] for x in linhas), 2),
        total_viagens=round(sum(x["gastoViagens"] for x in linhas), 2),
        total_divulgacao=round(sum(x["gastoDivulgacao"] for x in linhas), 2),
        tempo_segundos=round(time.perf_counter() - inicio, 4),
    )
