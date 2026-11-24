"""Serviço de ficha completa do deputado: combina Neo4j + API da Câmara (com cache) + funcionarios.csv."""

from __future__ import annotations

import csv
import html as html_lib
import json
import logging
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from camara.coleta.cache import DiskCache
from camara.coleta.cliente import CamaraAPIClient

logger = logging.getLogger(__name__)

# Bandeiras oficiais das 27 UFs (Wikimedia Commons) + metadados
UF_INFO: dict[str, dict[str, str]] = {
    "AC": {"nome": "Acre", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/4/4c/Bandeira_do_Acre.svg"},
    "AL": {"nome": "Alagoas", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/8/88/Bandeira_de_Alagoas.svg"},
    "AP": {"nome": "Amapá", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/0/0c/Bandeira_do_Amap%C3%A1.svg"},
    "AM": {"nome": "Amazonas", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/6/6b/Bandeira_do_Amazonas.svg"},
    "BA": {"nome": "Bahia", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/2/28/Bandeira_da_Bahia.svg"},
    "CE": {"nome": "Ceará", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/2/2e/Bandeira_do_Cear%C3%A1.svg"},
    "DF": {"nome": "Distrito Federal", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/3/3c/Bandeira_do_Distrito_Federal_%28Brasil%29.svg"},
    "ES": {"nome": "Espírito Santo", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/4/43/Bandeira_do_Esp%C3%ADrito_Santo.svg"},
    "GO": {"nome": "Goiás", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/b/be/Flag_of_Goi%C3%A1s.svg"},
    "MA": {"nome": "Maranhão", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/4/45/Bandeira_do_Maranh%C3%A3o.svg"},
    "MT": {"nome": "Mato Grosso", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/0/0b/Bandeira_de_Mato_Grosso.svg"},
    "MS": {"nome": "Mato Grosso do Sul", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/6/64/Bandeira_de_Mato_Grosso_do_Sul.svg"},
    "MG": {"nome": "Minas Gerais", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/f/f4/Bandeira_de_Minas_Gerais.svg"},
    "PA": {"nome": "Pará", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/0/02/Bandeira_do_Par%C3%A1.svg"},
    "PB": {"nome": "Paraíba", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/b/bb/Bandeira_da_Para%C3%ADba.svg"},
    "PR": {"nome": "Paraná", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/9/93/Bandeira_do_Paran%C3%A1.svg"},
    "PE": {"nome": "Pernambuco", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/5/59/Bandeira_de_Pernambuco.svg"},
    "PI": {"nome": "Piauí", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/3/33/Bandeira_do_Piau%C3%AD.svg"},
    "RJ": {"nome": "Rio de Janeiro", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/7/73/Bandeira_do_estado_do_Rio_de_Janeiro.svg"},
    "RN": {"nome": "Rio Grande do Norte", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/3/30/Bandeira_do_Rio_Grande_do_Norte.svg"},
    "RS": {"nome": "Rio Grande do Sul", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/6/63/Bandeira_do_Rio_Grande_do_Sul.svg"},
    "RO": {"nome": "Rondônia", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/f/fa/Bandeira_de_Rond%C3%B4nia.svg"},
    "RR": {"nome": "Roraima", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/9/98/Bandeira_de_Roraima.svg"},
    "SC": {"nome": "Santa Catarina", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/1/1a/Bandeira_de_Santa_Catarina.svg"},
    "SP": {"nome": "São Paulo", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/2/2b/Bandeira_do_estado_de_S%C3%A3o_Paulo.svg"},
    "SE": {"nome": "Sergipe", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/b/be/Bandeira_de_Sergipe.svg"},
    "TO": {"nome": "Tocantins", "bandeira": "https://upload.wikimedia.org/wikipedia/commons/f/ff/Bandeira_do_Tocantins.svg"},
}


def url_logo_partido(sigla: str | None) -> str | None:
    if not sigla:
        return None
    limpa = sigla.strip().replace(" ", "")
    return f"https://www.camara.leg.br/internet/Deputado/img/partidos/{limpa}.gif"


def url_bandeira_uf(sigla: str | None) -> str | None:
    if not sigla:
        return None
    info = UF_INFO.get(sigla.strip().upper())
    return info["bandeira"] if info else None


_CONECTORES_NOME = {
    "DA", "DE", "DO", "DOS", "DAS", "E", "FILHO", "FILHA",
    "NETO", "NETA", "JUNIOR", "SOBRINHO", "SOBRINHA", "SEGUNDO", "TERCEIRO",
}
_SOBRENOMES_MUITO_COMUNS = {
    "SILVA", "SANTOS", "OLIVEIRA", "SOUZA", "SOUSA", "PEREIRA", "COSTA",
    "RODRIGUES", "ALMEIDA", "LIMA", "FERREIRA", "ALVES", "GOMES", "RIBEIRO",
    "MARTINS", "ARAUJO", "NASCIMENTO", "CARVALHO",
}


def _normalizar_texto_nome(texto: str) -> str:
    sem_acento = "".join(
        ch for ch in unicodedata.normalize("NFD", texto) if unicodedata.category(ch) != "Mn"
    )
    return re.sub(r"[^A-Z\s]", " ", sem_acento.upper()).strip()


def _extrair_sobrenomes_relevantes(nome: str) -> set[str]:
    tokens = _normalizar_texto_nome(nome).split()
    if len(tokens) <= 1:
        return set()
    return {
        t
        for t in tokens[1:]
        if len(t) >= 4 and t not in _CONECTORES_NOME and t not in _SOBRENOMES_MUITO_COMUNS
    }


def _parsear_pessoal_gabinete_html(html_text: str) -> list[dict[str, Any]]:
    """Extrai funcionários (ativos e exonerados) e links individuais de remuneração do HTML da Câmara."""
    linhas = re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, flags=re.DOTALL | re.IGNORECASE)
    resultado: list[dict[str, Any]] = []
    for linha in linhas:
        celulas = re.findall(r"<td[^>]*>(.*?)</td>", linha, flags=re.DOTALL | re.IGNORECASE)
        if len(celulas) < 4:
            continue
        limpas = [
            re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
            for c in celulas
        ]
        nome = limpas[0]
        if not nome or nome.lower() == "nome":
            continue
        grupo = limpas[1] if len(limpas) > 1 else ""
        cargo = limpas[2] if len(limpas) > 2 else ""
        periodo = limpas[3] if len(limpas) > 3 else ""
        href_match = re.search(r'href="([^"]*remuneracao/[^"]+)"', linha, flags=re.IGNORECASE)
        url_rem = href_match.group(1) if href_match else ""
        if url_rem.startswith("/"):
            url_rem = f"https://www.camara.leg.br{url_rem}"
        ativo = periodo.lower().startswith("desde ") and " a " not in periodo.lower()
        resultado.append(
            {
                "nome": nome,
                "grupo": grupo,
                "cargo": cargo,
                "periodo": periodo,
                "ativo": ativo,
                "url_remuneracao": url_rem,
            }
        )
    return resultado


def _valor_liquido_despesa(item: dict[str, Any]) -> float:
    """Normaliza o valor líquido de um lançamento da API CEAP.

    A API v2 da Câmara ocasionalmente devolve ``valorLiquido`` com sinal invertido
    (ex.: ``valorDocumento = 95000.0``, ``valorGlosa = 0.0``, ``valorLiquido = -95000.0``),
    enquanto o CSV em lote e o portal oficial exibem ``+95000.0``.
    Estornos reais (ex.: passagem aérea cancelada) possuem ``valorDocumento <= 0``.
    """
    liq = float(item.get("valorLiquido") or 0.0)
    doc = float(item.get("valorDocumento") or 0.0)
    glosa = float(item.get("valorGlosa") or 0.0)
    if doc > 0 and liq < 0:
        corrigido = doc - glosa
        return round(corrigido if corrigido >= 0 else abs(liq), 2)
    if liq == 0.0 and doc != 0.0:
        return round(doc - glosa, 2)
    return round(liq, 2)


class FichaDeputadoService:
    """Busca e faz cache em disco dos dados detalhados da Câmara para a ficha completa."""

    def __init__(
        self,
        cache_dir: Path,
        api_base_url: str = "https://dadosabertos.camara.leg.br/api/v2",
        arquivos_base_url: str = "https://dadosabertos.camara.leg.br/arquivos",
        user_agent: str = "camara-em-grafo/0.1 (projeto de estudo de dados abertos)",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.api_cache = DiskCache(self.cache_dir / "ficha_api")
        self.api_base_url = api_base_url
        self.arquivos_base_url = arquivos_base_url.rstrip("/")
        self.user_agent = user_agent
        self._funcionarios_por_dep: dict[int, list[dict[str, Any]]] | None = None

    def _criar_cliente(self) -> CamaraAPIClient:
        return CamaraAPIClient(
            base_url=self.api_base_url,
            cache=self.api_cache,
            user_agent=self.user_agent,
            max_concorrencia=2,
            pausa_segundos=0.15,
            timeout_segundos=25.0,
            max_tentativas=3,
        )

    async def obter_detalhes_api(self, dep_id: int) -> dict[str, Any]:
        """Retorna /deputados/{id} (nome civil, gabinete, redes sociais, escolaridade, etc.)."""
        async with self._criar_cliente() as client:
            resp = await client.get_json(f"/deputados/{dep_id}")
            return dict(resp.get("dados") or {})

    async def obter_status_proposicao(self, prop_id: int) -> dict[str, Any]:
        """Retorna situação atual e inteiro teor de /proposicoes/{id} (com cache em disco)."""
        async with self._criar_cliente() as client:
            resp = await client.get_json(f"/proposicoes/{prop_id}")
            dados = dict(resp.get("dados") or {})
            status = dict(dados.get("statusProposicao") or {})
            return {
                "id": prop_id,
                "situacao": status.get("descricaoSituacao") or status.get("descricaoTramitacao") or "Sem situação informada",
                "orgao": status.get("siglaOrgao") or "—",
                "data": status.get("dataHora") or "",
                "regime": status.get("regime") or "—",
                "despacho": status.get("despacho") or "",
                "urlInteiroTeor": dados.get("urlInteiroTeor") or "",
            }

    async def obter_despesas(
        self, dep_id: int, ano: int, mes: int | None = None
    ) -> dict[str, Any]:
        """Retorna despesas CEAP de um deputado em um ano, com série mensal, categorias e filtro por mês."""
        async with self._criar_cliente() as client:
            itens_ano = await client.get_paginated(
                f"/deputados/{dep_id}/despesas",
                params={"ano": ano, "ordem": "DESC", "ordenarPor": "dataDocumento"},
                itens=100,
            )

        siglas_mes = [
            "JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
            "JUL", "AGO", "SET", "OUT", "NOV", "DEZ",
        ]
        totais_mes: dict[int, float] = {m: 0.0 for m in range(1, 13)}
        itens_normalizados: list[dict[str, Any]] = []
        for d in itens_ano:
            valor = _valor_liquido_despesa(d)
            item_norm = {**d, "valorLiquido": valor}
            itens_normalizados.append(item_norm)
            m = int(d.get("mes") or 0)
            if 1 <= m <= 12:
                totais_mes[m] += valor

        mensal = [
            {"mes": m, "sigla": siglas_mes[m - 1], "valor": round(totais_mes[m], 2)}
            for m in range(1, 13)
        ]

        itens = [
            d for d in itens_normalizados if mes is None or int(d.get("mes") or 0) == mes
        ]

        total = 0.0
        por_tipo: dict[str, float] = defaultdict(float)
        por_fornecedor: dict[str, float] = defaultdict(float)

        for d in itens:
            valor = float(d["valorLiquido"])
            total += valor
            tipo = (d.get("tipoDespesa") or "Outros").strip()
            forn = (d.get("nomeFornecedor") or "Não informado").strip()
            por_tipo[tipo] += valor
            por_fornecedor[forn] += valor

        categorias = sorted(
            [
                {
                    "tipo": k,
                    "valor": round(v, 2),
                    "percentual": round((v / total) * 100, 1) if total > 0 else 0.0,
                }
                for k, v in por_tipo.items()
            ],
            key=lambda x: x["valor"],
            reverse=True,
        )
        top_fornecedores = sorted(
            [{"fornecedor": k, "valor": round(v, 2)} for k, v in por_fornecedor.items()],
            key=lambda x: x["valor"],
            reverse=True,
        )[:10]

        return {
            "ano": ano,
            "mes": mes,
            "quantidade_notas": len(itens),
            "total": round(total, 2),
            "total_ano": round(sum(totais_mes.values()), 2),
            "mensal": mensal,
            "categorias": categorias,
            "top_fornecedores": top_fornecedores,
            "itens": itens[:100],
        }

    async def obter_atuacao(self, dep_id: int) -> dict[str, Any]:
        """Retorna frentes parlamentares, órgãos/comissões e discursos recentes."""
        async with self._criar_cliente() as client:
            frentes_resp = await client.get_json(f"/deputados/{dep_id}/frentes")
            orgaos_resp = await client.get_json(
                f"/deputados/{dep_id}/orgaos", params={"itens": 50, "ordem": "DESC", "ordenarPor": "dataInicio"}
            )
            discursos_resp = await client.get_json(
                f"/deputados/{dep_id}/discursos",
                params={"itens": 20, "ordem": "DESC", "ordenarPor": "dataHoraInicio"},
            )
        return {
            "frentes": list(frentes_resp.get("dados") or []),
            "orgaos": list(orgaos_resp.get("dados") or []),
            "discursos": list(discursos_resp.get("dados") or []),
        }

    async def obter_carreira(self, dep_id: int) -> dict[str, Any]:
        """Retorna profissões, ocupações, mandatos externos e histórico parlamentar."""
        async with self._criar_cliente() as client:
            prof = await client.get_json(f"/deputados/{dep_id}/profissoes")
            ocup = await client.get_json(f"/deputados/{dep_id}/ocupacoes")
            mand = await client.get_json(f"/deputados/{dep_id}/mandatosExternos")
            hist = await client.get_json(f"/deputados/{dep_id}/historico")
        historico = list(hist.get("dados") or [])
        historico.sort(key=lambda x: str(x.get("dataHora") or ""), reverse=True)
        return {
            "profissoes": list(prof.get("dados") or []),
            "ocupacoes": list(ocup.get("dados") or []),
            "mandatos_externos": list(mand.get("dados") or []),
            "historico": historico,
        }

    async def obter_funcionarios(self, dep_id: int) -> list[dict[str, Any]]:
        """Retorna secretários parlamentares e CNEs lotados no gabinete via funcionarios.csv."""
        if self._funcionarios_por_dep is None:
            await self._carregar_indice_funcionarios()
        return list((self._funcionarios_por_dep or {}).get(dep_id, []))

    async def obter_pessoal_gabinete(
        self,
        dep_id: int,
        ano: int,
        nome_civil: str | None = None,
    ) -> dict[str, Any]:
        """Consulta a página oficial de pessoal de gabinete da Câmara (ativos + exonerados no ano)
        com link direto para o contracheque individual e sinalização factual de mesmo sobrenome."""
        cache_path = self.cache_dir / "ficha_api" / f"pessoal_gabinete_{dep_id}_{ano}.json"
        registros: list[dict[str, Any]] | None = None
        if cache_path.exists() and cache_path.stat().st_size > 0:
            try:
                registros = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                registros = None

        if registros is None:
            url = f"https://www.camara.leg.br/deputados/{dep_id}/pessoal-gabinete?ano={ano}"
            try:
                async with httpx.AsyncClient(
                    headers={"User-Agent": self.user_agent},
                    timeout=20.0,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                registros = _parsear_pessoal_gabinete_html(resp.text)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(registros, ensure_ascii=False), encoding="utf-8"
                )
            except httpx.HTTPError as exc:
                logger.warning("Falha ao consultar pessoal-gabinete (%s/%s): %s", dep_id, ano, exc)
                # Fallback para funcionarios.csv
                csv_funcs = await self.obter_funcionarios(dep_id)
                registros = [
                    {
                        "nome": f["nome"],
                        "grupo": f["grupo"],
                        "cargo": f["cargo"] or f["funcao"] or "—",
                        "periodo": f"Desde {f['dataNomeacao']}" if f.get("dataNomeacao") else "Ativo",
                        "ativo": True,
                        "url_remuneracao": "",
                    }
                    for f in csv_funcs
                ]

        sobrenomes_dep = _extrair_sobrenomes_relevantes(nome_civil or "")
        enriquecidos: list[dict[str, Any]] = []
        ativos = 0
        exonerados = 0
        mesmo_sobrenome_qtd = 0
        for r in registros:
            sobrenomes_func = _extrair_sobrenomes_relevantes(r.get("nome", ""))
            coinc = sorted(sobrenomes_dep & sobrenomes_func) if sobrenomes_dep else []
            tem_coinc = bool(coinc)
            if r.get("ativo", True):
                ativos += 1
            else:
                exonerados += 1
            if tem_coinc:
                mesmo_sobrenome_qtd += 1
            enriquecidos.append({**r, "mesmo_sobrenome": tem_coinc, "sobrenomes_comuns": coinc})

        enriquecidos.sort(key=lambda x: (not x.get("ativo", True), not x["mesmo_sobrenome"], x["nome"]))
        return {
            "ano": ano,
            "ativos": ativos,
            "exonerados": exonerados,
            "total": len(enriquecidos),
            "mesmo_sobrenome_qtd": mesmo_sobrenome_qtd,
            "itens": enriquecidos,
        }

    async def _carregar_indice_funcionarios(self) -> None:
        path = self.cache_dir / "arquivos" / "funcionarios" / "funcionarios.csv"
        if not path.exists() or path.stat().st_size == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"{self.arquivos_base_url}/funcionarios/csv/funcionarios.csv"
            try:
                async with httpx.AsyncClient(
                    headers={"User-Agent": self.user_agent}, timeout=60.0, follow_redirects=True
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    tmp = path.with_suffix(".csv.tmp")
                    tmp.write_bytes(resp.content)
                    tmp.replace(path)
            except httpx.HTTPError as exc:
                logger.warning("Não foi possível baixar funcionarios.csv: %s", exc)
                self._funcionarios_por_dep = {}
                return

        indice: dict[int, list[dict[str, Any]]] = defaultdict(list)
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh, delimiter=";"):
                uri = (row.get("uriLotacao") or "").strip()
                if "/deputados/" not in uri:
                    continue
                parte = uri.rstrip("/").split("/")[-1]
                if not parte.isdigit():
                    continue
                indice[int(parte)].append(
                    {
                        "nome": (row.get("nome") or "").strip(),
                        "grupo": (row.get("grupo") or "").strip(),
                        "cargo": (row.get("cargo") or "").strip(),
                        "funcao": (row.get("funcao") or "").strip(),
                        "lotacao": (row.get("lotacao") or "").strip(),
                        "dataNomeacao": (row.get("dataNomeacao") or "").strip(),
                    }
                )

        for lista in indice.values():
            lista.sort(key=lambda x: (x["grupo"], x["nome"]))
        self._funcionarios_por_dep = dict(indice)
