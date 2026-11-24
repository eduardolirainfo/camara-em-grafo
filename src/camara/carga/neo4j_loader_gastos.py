"""Carga idempotente dos gastos CEAP (2023–2026), equipe e rede (Deputado)-[:PAGOU]->(Fornecedor)."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import statistics
import time
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date
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


CYPHER_ATUALIZAR_EXERCICIO = """
MATCH (d:Deputado)
SET d.emExercicio = d.id IN $idsAtivos
RETURN count(CASE WHEN d.emExercicio THEN 1 END) AS emExercicio,
       count(CASE WHEN NOT d.emExercicio THEN 1 END) AS foraExercicio
"""


def atualizar_em_exercicio(
    driver: Driver,
    api_base_url: str = "https://dadosabertos.camara.leg.br/api/v2",
    user_agent: str = "camara-em-grafo/0.1 (projeto de estudo de dados abertos)",
) -> dict[str, int]:
    """Consulta /deputados (em exercício hoje) em 1 chamada e grava d.emExercicio no Neo4j."""
    url = f"{api_base_url.rstrip('/')}/deputados"
    with httpx.Client(
        headers={"Accept": "application/json", "User-Agent": user_agent},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        resp = client.get(url, params={"itens": 1000})
        resp.raise_for_status()
        dados = resp.json().get("dados") or []
    ids_ativos = [int(d["id"]) for d in dados if d.get("id") is not None]
    with driver.session() as session:
        rec = session.execute_write(
            lambda tx: tx.run(CYPHER_ATUALIZAR_EXERCICIO, idsAtivos=ids_ativos).single()
        )
    return {
        "em_exercicio": int(rec["emExercicio"]) if rec else 0,
        "fora_exercicio": int(rec["foraExercicio"]) if rec else 0,
    }


CYPHER_ATUALIZAR_PRESENCA_HISTORICA = """
UNWIND $linhas AS row
MATCH (d:Deputado {id: row.id})
SET d.votacoesAtivo = row.votacoesAtivo,
    d.diasEmExercicio = row.diasEmExercicio,
    d.teveLicencaCargo = row.teveLicencaCargo
"""


def calcular_intervalos_exercicio(
    historico: list[dict[str, Any]],
    legislatura: int,
    data_fim_ref: str,
    em_exercicio_atual: bool = True,
) -> tuple[list[tuple[str, str]], bool]:
    """Reconstrói os intervalos exatos (inicio, fim) em que o deputado esteve em exercício na legislatura."""
    eventos = [
        ev for ev in historico if int(ev.get("idLegislatura") or 0) == legislatura
    ]
    eventos.sort(key=lambda x: str(x.get("dataHora") or ""))

    intervalos: list[tuple[str, str]] = []
    aberto_em: str | None = None
    teve_licenca_cargo = False
    viu_movimentacao = False

    for ev in eventos:
        dt = str(ev.get("dataHora") or "")[:10]
        if len(dt) != 10:
            continue
        desc = str(ev.get("descricaoStatus") or "").strip()
        desc_low = desc.lower()
        sit = str(ev.get("situacao") or "").strip().lower()

        if any(k in desc_low for k in ("ministro", "secretário", "secretario")):
            teve_licenca_cargo = True

        eh_entrada = desc_low.startswith("entrada")
        eh_saida = desc_low.startswith(("saída", "saida"))

        if eh_entrada:
            viu_movimentacao = True
            if aberto_em is None:
                aberto_em = dt
        elif eh_saida:
            viu_movimentacao = True
            if aberto_em is not None:
                intervalos.append((aberto_em, dt))
                aberto_em = None
        elif sit == "exercício" and aberto_em is None and "início da legislatura" not in desc_low and "diverso" not in desc_low:
            viu_movimentacao = True
            aberto_em = dt

    if aberto_em is not None:
        intervalos.append((aberto_em, data_fim_ref))
    elif not viu_movimentacao and em_exercicio_atual:
        intervalos.append(("2023-02-01", data_fim_ref))

    return intervalos, teve_licenca_cargo


def atualizar_presenca_historica(
    driver: Driver,
    cache_dir: Path,
    legislatura: int = 57,
    api_base_url: str = "https://dadosabertos.camara.leg.br/api/v2",
    user_agent: str = "camara-em-grafo/0.1 (projeto de estudo de dados abertos)",
) -> dict[str, int]:
    """Calcula d.votacoesAtivo, d.diasEmExercicio e d.teveLicencaCargo usando /deputados/{id}/historico."""
    with driver.session() as session:
        dep_rows = session.run(
            "MATCH (d:Deputado) RETURN d.id AS id, coalesce(d.emExercicio, true) AS ex"
        ).data()
        vot_datas = [
            str(r["dt"])[:10]
            for r in session.run("MATCH (v:Votacao) RETURN v.data AS dt").data()
            if r.get("dt")
        ]

    if not dep_rows or not vot_datas:
        return {"atualizados": 0, "com_licenca_cargo": 0}

    max_data = max(vot_datas)
    hist_cache_dir = Path(cache_dir) / "ficha_api" / "historico"
    hist_cache_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "application/json", "User-Agent": user_agent}

    def _carregar_hist(dep_id: int) -> tuple[int, list[dict[str, Any]]]:
        cpath = hist_cache_dir / f"{dep_id}.json"
        if cpath.exists() and cpath.stat().st_size > 0:
            try:
                return dep_id, json.loads(cpath.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        url = f"{api_base_url.rstrip('/')}/deputados/{dep_id}/historico"
        for tentativa in range(4):
            try:
                time.sleep(0.18)
                with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
                    resp = client.get(url)
                    if resp.status_code in {429, 500, 502, 503, 504}:
                        espera = float(resp.headers.get("Retry-After") or (1.5 * (tentativa + 1)))
                        time.sleep(min(espera, 8.0))
                        continue
                    resp.raise_for_status()
                    dados = list(resp.json().get("dados") or [])
                cpath.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
                return dep_id, dados
            except httpx.HTTPError as exc:
                if tentativa == 3:
                    logger.warning("Falha ao buscar histórico do deputado %s: %s", dep_id, exc)
                else:
                    time.sleep(1.5 * (tentativa + 1))
        return dep_id, []

    ex_map = {int(r["id"]): bool(r["ex"]) for r in dep_rows}
    linhas: list[dict[str, Any]] = []
    com_licenca = 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        for dep_id, hist in pool.map(_carregar_hist, list(ex_map.keys())):
            intervalos, teve_licenca = calcular_intervalos_exercicio(
                historico=hist,
                legislatura=legislatura,
                data_fim_ref=max_data,
                em_exercicio_atual=ex_map[dep_id],
            )
            if teve_licenca:
                com_licenca += 1
            vot_ativo = sum(
                1 for dt in vot_datas if any(ini <= dt <= fim for ini, fim in intervalos)
            )
            dias = 0
            for ini, fim in intervalos:
                try:
                    d0 = date.fromisoformat(ini)
                    d1 = date.fromisoformat(fim)
                    dias += max(1, (d1 - d0).days + 1)
                except ValueError:
                    pass
            linhas.append(
                {
                    "id": dep_id,
                    "votacoesAtivo": vot_ativo,
                    "diasEmExercicio": dias,
                    "teveLicencaCargo": teve_licenca,
                }
            )

    with driver.session() as session:
        for lote in _em_lotes(linhas, 500):
            session.execute_write(
                lambda tx, rows=lote: tx.run(
                    CYPHER_ATUALIZAR_PRESENCA_HISTORICA, linhas=rows
                ).consume()
            )
    return {"atualizados": len(linhas), "com_licenca_cargo": com_licenca}


CYPHER_ATUALIZAR_INDICADORES = """
UNWIND $linhas AS row
MATCH (d:Deputado {id: row.id})
SET d.hhiFornecedores = row.hhiFornecedores,
    d.gastoVsMedianaUf = row.gastoVsMedianaUf,
    d.projetosVotados = row.projetosVotados,
    d.projetosAprovados = row.projetosAprovados
"""


def atualizar_indicadores_cientificos(driver: Driver) -> dict[str, int]:
    """Calcula e grava indicadores objetivos normalizados no Neo4j:
    - hhiFornecedores: Índice Herfindahl-Hirschman (0–10000) de concentração de fornecedores.
    - gastoVsMedianaUf: razão entre o gasto diário do deputado e a mediana da própria UF.
    - projetosVotados / projetosAprovados: funil de efetividade legislativa.
    """
    with driver.session() as session:
        deps = session.run(
            """
            MATCH (d:Deputado)
            OPTIONAL MATCH (d)-[r:PAGOU]->(:Fornecedor)
            WITH d, collect(r.total) AS valoresPagos
            OPTIONAL MATCH (d)-[:AUTOR_DE]->(p:Proposicao)<-[:SOBRE]-(v:Votacao)
            WITH d, valoresPagos,
                 count(DISTINCT p) AS projetosVotados,
                 count(DISTINCT CASE
                   WHEN v IS NOT NULL AND v.aprovacao = true AND NOT (
                     toLower(coalesce(v.descricao, '')) CONTAINS 'requerimento'
                     OR toLower(coalesce(v.descricao, '')) CONTAINS 'destaque'
                     OR toLower(coalesce(v.descricao, '')) CONTAINS 'emenda'
                     OR toLower(coalesce(v.descricao, '')) CONTAINS 'retirada de pauta'
                     OR toLower(coalesce(v.descricao, '')) CONTAINS 'adiamento'
                     OR toLower(coalesce(v.descricao, '')) CONTAINS 'preferência'
                     OR toLower(coalesce(v.descricao, '')) CONTAINS 'inversão'
                     OR toLower(coalesce(v.descricao, '')) CONTAINS 'encerramento da discussão'
                   ) THEN p END) AS projetosAprovados
            RETURN d.id AS id,
                   d.uf AS uf,
                   coalesce(d.gastoTotalCeap, 0.0) AS gastoTotalCeap,
                   coalesce(d.diasEmExercicio, 0) AS diasEmExercicio,
                   valoresPagos,
                   projetosVotados,
                   projetosAprovados
            """
        ).data()

    por_uf: dict[str, list[float]] = defaultdict(list)
    diario_por_dep: dict[int, float] = {}
    for row in deps:
        gasto = float(row["gastoTotalCeap"] or 0.0)
        dias = int(row["diasEmExercicio"] or 0)
        uf = str(row["uf"] or "").strip().upper()
        # Regra de materialidade estatística: ao menos 1 ano (365 dias) em exercício
        if gasto > 0 and dias >= 365 and uf:
            diario = gasto / dias
            diario_por_dep[int(row["id"])] = diario
            por_uf[uf].append(diario)

    mediana_uf = {uf: statistics.median(vals) for uf, vals in por_uf.items() if vals}

    linhas: list[dict[str, Any]] = []
    alta_conc = 0
    acima_uf = 0
    for row in deps:
        dep_id = int(row["id"])
        uf = str(row["uf"] or "").strip().upper()
        gasto = float(row["gastoTotalCeap"] or 0.0)
        dias = int(row["diasEmExercicio"] or 0)
        pagos = [float(v) for v in (row["valoresPagos"] or []) if v and float(v) > 0]
        denom = max(gasto, sum(pagos))
        # HHI só tem relevância estatística/fiscal com >= 365 dias ativos e >= R$ 300 mil gastos
        hhi = (
            round(sum((v / denom) ** 2 for v in pagos) * 10000)
            if denom >= 300_000.0 and dias >= 365 and pagos
            else 0
        )
        med = mediana_uf.get(uf, 0.0)
        diario = diario_por_dep.get(dep_id)
        ratio_uf = round(diario / med, 4) if (diario is not None and med > 0) else None

        if hhi >= 2500:
            alta_conc += 1
        if ratio_uf is not None and ratio_uf >= 1.25:
            acima_uf += 1

        linhas.append(
            {
                "id": dep_id,
                "hhiFornecedores": hhi,
                "gastoVsMedianaUf": ratio_uf,
                "projetosVotados": int(row["projetosVotados"] or 0),
                "projetosAprovados": int(row["projetosAprovados"] or 0),
            }
        )

    with driver.session() as session:
        for lote in _em_lotes(linhas, 500):
            session.execute_write(
                lambda tx, rows=lote: tx.run(
                    CYPHER_ATUALIZAR_INDICADORES, linhas=rows
                ).consume()
            )
    return {
        "atualizados": len(linhas),
        "alta_concentracao_hhi": alta_conc,
        "acima_mediana_uf_25pct": acima_uf,
    }


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

    atualizar_em_exercicio(driver=driver, user_agent=user_agent)
    atualizar_presenca_historica(
        driver=driver,
        cache_dir=cache_dir,
        user_agent=user_agent,
    )
    atualizar_indicadores_cientificos(driver=driver)

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
