"""Carga idempotente dos gastos CEAP (2023–2026), equipe e rede (Deputado)-[:PAGOU]->(Fornecedor)."""

from __future__ import annotations

import csv
import io
import logging
import re
import time
import zipfile
from collections import defaultdict
from collections.abc import Iterable
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
CATEGORIA_AERONAVES = "LOCAÇÃO OU FRETAMENTO DE AERONAVES"
CATEGORIA_CONSULTORIAS = "CONSULTORIAS, PESQUISAS E TRABALHOS TÉCNICOS."

CYPHER_CONSTRAINT_FORNECEDOR = (
    "CREATE CONSTRAINT fornecedor_cnpj_unico IF NOT EXISTS "
    "FOR (f:Fornecedor) REQUIRE f.cnpj IS UNIQUE"
)

CYPHER_ATUALIZAR_GASTOS = """
UNWIND $linhas AS row
MATCH (d:Deputado {id: row.id})
SET d.gastoTotalCeap = row.gastoTotalCeap,
    d.gastoViagens = row.gastoViagens,
    d.gastoDivulgacao = row.gastoDivulgacao,
    d.gastoAeronaves = row.gastoAeronaves,
    d.gastoConsultorias = row.gastoConsultorias,
    d.maiorFornecedorNome = row.maiorFornecedorNome,
    d.maiorFornecedorCnpj = row.maiorFornecedorCnpj,
    d.maiorFornecedorValor = row.maiorFornecedorValor,
    d.maiorFornecedorPct = row.maiorFornecedorPct,
    d.qtdNotasCeap = row.qtdNotasCeap,
    d.numFuncionarios = row.numFuncionarios,
    d.anosCeap = $anos
"""

CYPHER_MERGE_FORNECEDORES = """
UNWIND $linhas AS row
MERGE (f:Fornecedor {cnpj: row.cnpj})
SET f.nome = row.nome,
    f.totalRecebido = row.totalRecebido,
    f.qtdNotas = row.qtdNotas,
    f.qtdDeputados = row.qtdDeputados
"""

CYPHER_MERGE_PAGOU = """
UNWIND $linhas AS row
MATCH (d:Deputado {id: row.depId})
MATCH (f:Fornecedor {cnpj: row.cnpj})
MERGE (d)-[r:PAGOU]->(f)
SET r.total = row.total,
    r.notas = row.notas
"""


@dataclass
class ResumoCargaGastos:
    anos: list[int]
    notas_lidas: int
    deputados_atualizados: int
    fornecedores_carregados: int
    relacoes_pagou: int
    total_geral_ceap: float
    total_viagens: float
    total_divulgacao: float
    total_aeronaves: float
    total_consultorias: float
    tempo_segundos: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _em_lotes(itens: list[dict[str, Any]], tamanho: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(itens), tamanho):
        yield itens[i : i + tamanho]


def _to_float(val: str | None) -> float:
    if not val:
        return 0.0
    s = val.strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _normalizar_cnpj(raw: str | None) -> str:
    if not raw:
        return ""
    return re.sub(r"\D+", "", raw)


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
    minimo_fornecedor_total: float = 10000.0,
) -> ResumoCargaGastos:
    inicio = time.perf_counter()
    zips = baixar_cotas_zip(cache_dir, anos, user_agent=user_agent, forcar=forcar_download)
    func_por_dep = contar_funcionarios_por_deputado(cache_dir)

    totais: dict[int, dict[str, Any]] = {}
    dep_forn_valor: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    dep_forn_notas: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    forn_nome: dict[str, str] = {}
    forn_total: dict[str, float] = defaultdict(float)
    forn_notas: dict[str, int] = defaultdict(int)
    forn_deps: dict[str, set[int]] = defaultdict(set)
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
                    cnpj = _normalizar_cnpj(row.get("txtCNPJCPF"))
                    nome_f = (row.get("txtFornecedor") or "").strip()

                    acc = totais.get(dep_id)
                    if acc is None:
                        acc = {
                            "id": dep_id,
                            "gastoTotalCeap": 0.0,
                            "gastoViagens": 0.0,
                            "gastoDivulgacao": 0.0,
                            "gastoAeronaves": 0.0,
                            "gastoConsultorias": 0.0,
                            "qtdNotasCeap": 0,
                        }
                        totais[dep_id] = acc

                    acc["gastoTotalCeap"] += val
                    acc["qtdNotasCeap"] += 1
                    if desc in CATEGORIAS_VIAGEM:
                        acc["gastoViagens"] += val
                    if desc == CATEGORIA_DIVULGACAO:
                        acc["gastoDivulgacao"] += val
                    if desc == CATEGORIA_AERONAVES:
                        acc["gastoAeronaves"] += val
                    if desc == CATEGORIA_CONSULTORIAS:
                        acc["gastoConsultorias"] += val

                    if len(cnpj) in {11, 14} and val > 0:
                        dep_forn_valor[dep_id][cnpj] += val
                        dep_forn_notas[dep_id][cnpj] += 1
                        forn_total[cnpj] += val
                        forn_notas[cnpj] += 1
                        forn_deps[cnpj].add(dep_id)
                        if nome_f and cnpj not in forn_nome:
                            forn_nome[cnpj] = nome_f

    todos_ids = set(totais.keys()) | set(func_por_dep.keys())
    linhas_deps = []
    for dep_id in todos_ids:
        acc = totais.get(
            dep_id,
            {
                "id": dep_id,
                "gastoTotalCeap": 0.0,
                "gastoViagens": 0.0,
                "gastoDivulgacao": 0.0,
                "gastoAeronaves": 0.0,
                "gastoConsultorias": 0.0,
                "qtdNotasCeap": 0,
            },
        )
        total_dep = acc["gastoTotalCeap"]
        maior_cnpj = ""
        maior_val = 0.0
        for cnpj, v in dep_forn_valor.get(dep_id, {}).items():
            if v > maior_val:
                maior_val = v
                maior_cnpj = cnpj
        maior_pct = round(maior_val / total_dep, 4) if total_dep > 0 and maior_val > 0 else 0.0

        linhas_deps.append(
            {
                "id": dep_id,
                "gastoTotalCeap": round(total_dep, 2),
                "gastoViagens": round(acc["gastoViagens"], 2),
                "gastoDivulgacao": round(acc["gastoDivulgacao"], 2),
                "gastoAeronaves": round(acc["gastoAeronaves"], 2),
                "gastoConsultorias": round(acc["gastoConsultorias"], 2),
                "maiorFornecedorNome": forn_nome.get(maior_cnpj, ""),
                "maiorFornecedorCnpj": maior_cnpj,
                "maiorFornecedorValor": round(maior_val, 2),
                "maiorFornecedorPct": maior_pct,
                "qtdNotasCeap": int(acc["qtdNotasCeap"]),
                "numFuncionarios": int(func_por_dep.get(dep_id, 0)),
            }
        )

    # Filtra fornecedores relevantes (>= R$ 10 mil ou pagos por >= 2 deputados) para manter o grafo ágil
    cnpjs_relevantes = {
        cnpj
        for cnpj, tot in forn_total.items()
        if tot >= minimo_fornecedor_total or len(forn_deps[cnpj]) >= 2
    }
    linhas_forn = [
        {
            "cnpj": cnpj,
            "nome": forn_nome.get(cnpj, cnpj),
            "totalRecebido": round(forn_total[cnpj], 2),
            "qtdNotas": int(forn_notas[cnpj]),
            "qtdDeputados": len(forn_deps[cnpj]),
        }
        for cnpj in cnpjs_relevantes
    ]
    linhas_pagou = []
    for dep_id, mapa in dep_forn_valor.items():
        for cnpj, v in mapa.items():
            if cnpj in cnpjs_relevantes and v > 0:
                linhas_pagou.append(
                    {
                        "depId": dep_id,
                        "cnpj": cnpj,
                        "total": round(v, 2),
                        "notas": int(dep_forn_notas[dep_id][cnpj]),
                    }
                )

    with driver.session() as session:
        session.run(CYPHER_CONSTRAINT_FORNECEDOR).consume()
        session.execute_write(
            lambda tx: tx.run(CYPHER_ATUALIZAR_GASTOS, linhas=linhas_deps, anos=anos).consume()
        )
        for lote in _em_lotes(linhas_forn, 2000):
            session.execute_write(
                lambda tx, rows=lote: tx.run(CYPHER_MERGE_FORNECEDORES, linhas=rows).consume()
            )
        for lote in _em_lotes(linhas_pagou, 3000):
            session.execute_write(
                lambda tx, rows=lote: tx.run(CYPHER_MERGE_PAGOU, linhas=rows).consume()
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
        fornecedores_carregados=len(linhas_forn),
        relacoes_pagou=len(linhas_pagou),
        total_geral_ceap=round(sum(x["gastoTotalCeap"] for x in linhas_deps), 2),
        total_viagens=round(sum(x["gastoViagens"] for x in linhas_deps), 2),
        total_divulgacao=round(sum(x["gastoDivulgacao"] for x in linhas_deps), 2),
        total_aeronaves=round(sum(x["gastoAeronaves"] for x in linhas_deps), 2),
        total_consultorias=round(sum(x["gastoConsultorias"] for x in linhas_deps), 2),
        tempo_segundos=round(time.perf_counter() - inicio, 4),
    )
