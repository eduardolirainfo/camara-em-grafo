"""Serviço de ficha completa do deputado: combina Neo4j + API da Câmara (com cache) + funcionarios.csv."""

from __future__ import annotations

import asyncio
import csv
import html as html_lib
import io
import json
import logging
import re
import unicodedata
import zipfile
from collections import defaultdict
from difflib import SequenceMatcher
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


# Principais aeroportos brasileiros (IATA -> Cidade/UF) e internacionais comuns na CEAP
AEROPORTOS_IATA: dict[str, str] = {
    # Capitais e grandes centros do Brasil
    "BSB": "Brasília (DF)",
    "CGH": "São Paulo · Congonhas (SP)",
    "GRU": "Guarulhos (SP)",
    "VCP": "Campinas · Viracopos (SP)",
    "SDU": "Rio de Janeiro · Santos Dumont (RJ)",
    "GIG": "Rio de Janeiro · Galeão (RJ)",
    "CNF": "Belo Horizonte · Confins (MG)",
    "PLU": "Belo Horizonte · Pampulha (MG)",
    "SSA": "Salvador (BA)",
    "REC": "Recife (PE)",
    "FOR": "Fortaleza (CE)",
    "POA": "Porto Alegre (RS)",
    "CWB": "Curitiba (PR)",
    "FLN": "Florianópolis (SC)",
    "GYN": "Goiânia (GO)",
    "VIX": "Vitória (ES)",
    "BEL": "Belém (PA)",
    "MAO": "Manaus (AM)",
    "MCP": "Macapá (AP)",
    "NAT": "Natal (RN)",
    "MCZ": "Maceió (AL)",
    "AJU": "Aracaju (SE)",
    "JPA": "João Pessoa (PB)",
    "SLZ": "São Luís (MA)",
    "THE": "Teresina (PI)",
    "CGB": "Cuiabá (MT)",
    "CGR": "Campo Grande (MS)",
    "PVH": "Porto Velho (RO)",
    "RBR": "Rio Branco (AC)",
    "BVB": "Boa Vista (RR)",
    "PMW": "Palmas (TO)",
    # Interior do Brasil (frequentes na CEAP)
    "RAO": "Ribeirão Preto (SP)",
    "SJP": "São José do Rio Preto (SP)",
    "PPB": "Presidente Prudente (SP)",
    "JTC": "Bauru (SP)",
    "ARU": "Araçatuba (SP)",
    "MII": "Marília (SP)",
    "SJK": "São José dos Campos (SP)",
    "UDI": "Uberlândia (MG)",
    "UBA": "Uberaba (MG)",
    "MOC": "Montes Claros (MG)",
    "IZA": "Juiz de Fora (MG)",
    "JDF": "Juiz de Fora (MG)",
    "IPN": "Ipatinga (MG)",
    "GVR": "Governador Valadares (MG)",
    "VAG": "Varginha (MG)",
    "LDB": "Londrina (PR)",
    "MGF": "Maringá (PR)",
    "IGU": "Foz do Iguaçu (PR)",
    "CAC": "Cascavel (PR)",
    "PGZ": "Ponta Grossa (PR)",
    "NVT": "Navegantes (SC)",
    "JOI": "Joinville (SC)",
    "XAP": "Chapecó (SC)",
    "JJG": "Jaguaruna (SC)",
    "CXJ": "Caxias do Sul (RS)",
    "PFB": "Passo Fundo (RS)",
    "PET": "Pelotas (RS)",
    "RIA": "Santa Maria (RS)",
    "GEL": "Santo Ângelo (RS)",
    "URG": "Uruguaiana (RS)",
    "BPS": "Porto Seguro (BA)",
    "IOS": "Ilhéus (BA)",
    "VDC": "Vitória da Conquista (BA)",
    "BRA": "Barreiras (BA)",
    "FEC": "Feira de Santana (BA)",
    "LEC": "Lençóis (BA)",
    "PNZ": "Petrolina (PE)",
    "FEN": "Fernando de Noronha (PE)",
    "JDO": "Juazeiro do Norte (CE)",
    "JJD": "Jericoacoara (CE)",
    "CPV": "Campina Grande (PB)",
    "IMP": "Imperatriz (MA)",
    "PHB": "Parnaíba (PI)",
    "STM": "Santarém (PA)",
    "MAB": "Marabá (PA)",
    "CKS": "Carajás (PA)",
    "ATM": "Altamira (PA)",
    "ITB": "Itaituba (PA)",
    "OPS": "Sinop (MT)",
    "ROO": "Rondonópolis (MT)",
    "SMT": "Sorriso (MT)",
    "AFL": "Alta Floresta (MT)",
    "DOU": "Dourados (MS)",
    "BYO": "Bonito (MS)",
    "CMG": "Corumbá (MS)",
    "TJL": "Três Lagoas (MS)",
    "JPR": "Ji-Paraná (RO)",
    "OAL": "Cacoal (RO)",
    "BVH": "Vilhena (RO)",
    "CZS": "Cruzeiro do Sul (AC)",
    "AUX": "Araguaína (TO)",
    "CAW": "Campos dos Goytacazes (RJ)",
    "CFB": "Cabo Frio (RJ)",
    "MEA": "Macaé (RJ)",
    "CLV": "Caldas Novas (GO)",
    "RVD": "Rio Verde (GO)",
    "TFF": "Tefé (AM)",
    "TBT": "Tabatinga (AM)",
    "PIN": "Parintins (AM)",
    # Destinos internacionais comuns
    "MVD": "Montevidéu (Uruguai)",
    "ASU": "Assunção (Paraguai)",
    "EZE": "Buenos Aires · Ezeiza (Argentina)",
    "AEP": "Buenos Aires · Aeroparque (Argentina)",
    "SCL": "Santiago (Chile)",
    "LIM": "Lima (Peru)",
    "BOG": "Bogotá (Colômbia)",
    "PTY": "Cidade do Panamá (Panamá)",
    "MEX": "Cidade do México (México)",
    "CUN": "Cancún (México)",
    "MIA": "Miami (EUA)",
    "MCO": "Orlando (EUA)",
    "JFK": "Nova York (EUA)",
    "EWR": "Newark · Nova York (EUA)",
    "IAD": "Washington (EUA)",
    "ATL": "Atlanta (EUA)",
    "IAH": "Houston (EUA)",
    "DFW": "Dallas (EUA)",
    "LIS": "Lisboa (Portugal)",
    "OPO": "Porto (Portugal)",
    "MAD": "Madri (Espanha)",
    "BCN": "Barcelona (Espanha)",
    "CDG": "Paris (França)",
    "LHR": "Londres (Reino Unido)",
    "FCO": "Roma (Itália)",
    "FRA": "Frankfurt (Alemanha)",
    "AMS": "Amsterdã (Holanda)",
    "ZRH": "Zurique (Suíça)",
    "GVA": "Genebra (Suíça)",
    "DXB": "Dubai (Emirados Árabes)",
    "DOH": "Doha (Catar)",
}


def formatar_trecho_legivel(trecho: str | None) -> str:
    """Traduz códigos IATA (ex.: 'MCP/BSB') para 'Macapá (AP) → Brasília (DF)'."""
    if not trecho:
        return ""
    partes = [p.strip().upper() for p in re.split(r"[/\-]+", str(trecho)) if p.strip()]
    if not partes:
        return str(trecho)
    traduzidas = [AEROPORTOS_IATA.get(cod, cod) for cod in partes]
    return " → ".join(traduzidas)


def url_bandeira_uf(sigla: str | None) -> str | None:
    if not sigla:
        return None
    info = UF_INFO.get(sigla.strip().upper())
    return info["bandeira"] if info else None


_CONECTORES_NOME = {
    "DA", "DE", "DO", "DOS", "DAS", "E", "FILHO", "FILHA",
    "NETO", "NETA", "JUNIOR", "SOBRINHO", "SOBRINHA", "SEGUNDO", "TERCEIRO",
}


def _normalizar_texto_nome(texto: str) -> str:
    sem_acento = "".join(
        ch for ch in unicodedata.normalize("NFD", texto) if unicodedata.category(ch) != "Mn"
    )
    return re.sub(r"[^A-Z\s]", " ", sem_acento.upper()).strip()


def _token_similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 5 and len(b) >= 5:
        return SequenceMatcher(None, a, b).ratio() >= 0.84
    return False


def _mesma_pessoa_bilhete(nome_bilhete: str, nome_oficial: str) -> bool:
    """Compara nome impresso em bilhete aéreo (abreviado, invertido ou com pequeno erro de digitação) com nome oficial."""
    toks_b = [
        t for t in _normalizar_texto_nome(nome_bilhete).split() if t not in _CONECTORES_NOME
    ]
    toks_o = [
        t for t in _normalizar_texto_nome(nome_oficial).split() if t not in _CONECTORES_NOME
    ]
    if not toks_b or not toks_o:
        return False
    longos_b = [t for t in toks_b if len(t) >= 3]
    longos_o = [t for t in toks_o if len(t) >= 3]
    matches = sum(
        1 for tb in longos_b if any(_token_similar(tb, to) for to in longos_o)
    )
    if len(longos_b) >= 2 and matches == len(longos_b):
        return True
    return matches >= 2


def _classificar_passageiro(
    nome_bilhete: str,
    nomes_parlamentar: list[str],
    nomes_equipe: list[str],
) -> str:
    """Retorna 'parlamentar', 'equipe' ou 'externo'."""
    for np in nomes_parlamentar:
        if np and _mesma_pessoa_bilhete(nome_bilhete, np):
            return "parlamentar"
    for ne in nomes_equipe:
        if ne and _mesma_pessoa_bilhete(nome_bilhete, ne):
            return "equipe"
    return "externo"


def _parsear_pessoal_gabinete_html(html_text: str) -> list[dict[str, Any]]:
    """Extrai funcionários (ativos e exonerados) e links para o portal oficial da Câmara."""
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
        self._voos_por_ano: dict[int, dict[int, list[dict[str, Any]]]] = {}
        self._voo_por_doc: dict[int, dict[str, tuple[str, str]]] = {}

    def _carregar_voos_ano(self, ano: int) -> None:
        """Indexa trechos (origem/destino) e passageiros do CSV em lote Ano-{ano}.csv.zip."""
        if ano in self._voos_por_ano:
            return
        zpath = self.cache_dir / "arquivos" / "cotas" / f"Ano-{ano}.csv.zip"
        por_dep: dict[int, list[dict[str, Any]]] = defaultdict(list)
        por_doc: dict[str, tuple[str, str]] = {}
        if zpath.exists():
            try:
                with zipfile.ZipFile(zpath) as zf, zf.open(zf.namelist()[0]) as fh:
                    for row in csv.DictReader(
                        io.TextIOWrapper(fh, encoding="utf-8-sig"), delimiter=";"
                    ):
                        trecho_raw = (row.get("txtTrecho") or "").strip().upper()
                        trecho = "/".join(
                            p.strip() for p in trecho_raw.split("/") if p.strip()
                        )
                        passageiro = (row.get("txtPassageiro") or "").strip()
                        if not trecho and not passageiro:
                            continue
                        dep_str = (row.get("ideCadastro") or "").strip()
                        if not dep_str.isdigit():
                            continue
                        dep_id = int(dep_str)
                        cod_doc = (row.get("ideDocumento") or "").strip()
                        num_doc = (row.get("txtNumero") or "").strip()
                        mes_str = (row.get("numMes") or "0").strip()
                        mes_int = int(mes_str) if mes_str.isdigit() else 0
                        try:
                            val = float((row.get("vlrLiquido") or "0").replace(",", "."))
                        except ValueError:
                            val = 0.0
                        if cod_doc:
                            por_doc[cod_doc] = (trecho, passageiro)
                        if num_doc:
                            por_doc[f"num:{dep_id}:{num_doc}"] = (trecho, passageiro)
                        por_dep[dep_id].append(
                            {
                                "mes": mes_int,
                                "trecho": trecho,
                                "passageiro": passageiro,
                                "valor": val,
                                "data": (row.get("datEmissao") or "")[:10],
                            }
                        )
            except (OSError, zipfile.BadZipFile) as exc:
                logger.warning("Não foi possível ler trechos de Ano-%s.csv.zip: %s", ano, exc)
        self._voos_por_ano[ano] = dict(por_dep)
        self._voo_por_doc[ano] = por_doc

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
        self,
        dep_id: int,
        ano: int,
        mes: int | None = None,
        nomes_parlamentar: list[str] | None = None,
        anos_verificacao: list[int] | None = None,
    ) -> dict[str, Any]:
        """Retorna despesas CEAP de um deputado em um ano, com série mensal, categorias e filtro por mês."""
        async with self._criar_cliente() as client:
            itens_ano = await client.get_paginated(
                f"/deputados/{dep_id}/despesas",
                params={"ano": ano, "ordem": "DESC", "ordenarPor": "dataDocumento"},
                itens=100,
            )

        self._carregar_voos_ano(ano)
        mapa_doc = self._voo_por_doc.get(ano, {})
        voos_dep = [
            v
            for v in self._voos_por_ano.get(ano, {}).get(dep_id, [])
            if mes is None or v["mes"] == mes
        ]

        siglas_mes = [
            "JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
            "JUL", "AGO", "SET", "OUT", "NOV", "DEZ",
        ]
        totais_mes: dict[int, float] = {m: 0.0 for m in range(1, 13)}
        itens_normalizados: list[dict[str, Any]] = []
        for d in itens_ano:
            valor = _valor_liquido_despesa(d)
            cod_doc = str(d.get("codDocumento") or "").strip()
            num_doc = str(d.get("numDocumento") or "").strip()
            trecho, _passageiro = mapa_doc.get(
                cod_doc, mapa_doc.get(f"num:{dep_id}:{num_doc}", ("", ""))
            )
            item_norm = {
                **d,
                "valorLiquido": valor,
                "trecho": trecho,
            }
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
        cnpj_por_fornecedor: dict[str, str] = {}

        for d in itens:
            valor = float(d["valorLiquido"])
            total += valor
            tipo = (d.get("tipoDespesa") or "Outros").strip()
            forn = (d.get("nomeFornecedor") or "Não informado").strip()
            cnpj_raw = re.sub(r"\D+", "", str(d.get("cnpjCpfFornecedor") or ""))
            por_tipo[tipo] += valor
            por_fornecedor[forn] += valor
            if len(cnpj_raw) == 14 and forn not in cnpj_por_fornecedor:
                cnpj_por_fornecedor[forn] = cnpj_raw

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
            [
                {
                    "fornecedor": k,
                    "cnpj": cnpj_por_fornecedor.get(k, ""),
                    "valor": round(v, 2),
                }
                for k, v in por_fornecedor.items()
            ],
            key=lambda x: x["valor"],
            reverse=True,
        )[:10]

        por_trecho_qtd: dict[str, int] = defaultdict(int)
        por_trecho_val: dict[str, float] = defaultdict(float)
        for v in voos_dep:
            if v["trecho"]:
                por_trecho_qtd[v["trecho"]] += 1
                por_trecho_val[v["trecho"]] += v["valor"]

        top_trechos = sorted(
            [
                {"trecho": k, "voos": por_trecho_qtd[k], "valor": round(por_trecho_val[k], 2)}
                for k in por_trecho_qtd
            ],
            key=lambda x: (x["voos"], x["valor"]),
            reverse=True,
        )[:12]

        # Resumo agregado de passagens no ano (sem expor nomes de terceiros)
        nomes_dep = [n for n in (nomes_parlamentar or []) if n]
        equipe_ano_atual: list[str] = []
        if voos_dep:
            try:
                p_res = await self.obter_pessoal_gabinete(dep_id, ano=ano)
                equipe_ano_atual = [str(f.get("nome") or "") for f in p_res.get("itens") or []]
            except (httpx.HTTPError, OSError, ValueError):
                equipe_ano_atual = []

        tot_pass: dict[str, dict[str, Any]] = {
            "parlamentar": {"bilhetes": 0, "valor": 0.0},
            "equipe": {"bilhetes": 0, "valor": 0.0},
            "outros": {"bilhetes": 0, "valor": 0.0},
        }
        for v in voos_dep:
            p_nome = v.get("passageiro") or ""
            if not p_nome:
                continue
            cat = _classificar_passageiro(p_nome, nomes_dep, equipe_ano_atual)
            chave_cat = "outros" if cat == "externo" else cat
            tot_pass[chave_cat]["bilhetes"] += 1
            tot_pass[chave_cat]["valor"] += float(v.get("valor") or 0.0)

        for grupo in tot_pass.values():
            grupo["valor"] = round(float(grupo["valor"]), 2)
        resumo_passageiros = {
            **tot_pass,
            "total_bilhetes": sum(int(g["bilhetes"]) for g in tot_pass.values()),
        }

        return {
            "ano": ano,
            "mes": mes,
            "quantidade_notas": len(itens),
            "total": round(total, 2),
            "total_ano": round(sum(totais_mes.values()), 2),
            "mensal": mensal,
            "categorias": categorias,
            "top_fornecedores": top_fornecedores,
            "top_trechos": top_trechos,
            "resumo_passageiros": resumo_passageiros,
            "total_voos_com_trecho": sum(por_trecho_qtd.values()),
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
        discursos_limpos = []
        for disc in list(discursos_resp.get("dados") or []):
            d_copy = dict(disc)
            for campo in ("sumario", "transcricao", "keywords"):
                if isinstance(d_copy.get(campo), str):
                    d_copy[campo] = (
                        d_copy[campo]
                        .replace("\x91", "'")
                        .replace("\x92", "'")
                        .replace("\x93", '"')
                        .replace("\x94", '"')
                        .replace("\x96", "–")
                        .replace("\x97", "—")
                    )
            discursos_limpos.append(d_copy)
        return {
            "frentes": list(frentes_resp.get("dados") or []),
            "orgaos": list(orgaos_resp.get("dados") or []),
            "discursos": discursos_limpos,
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
        busca: str | None = None,
    ) -> dict[str, Any]:
        """Consulta a página oficial de pessoal de gabinete da Câmara (ativos + exonerados no ano)
        com link para o portal oficial de remuneração."""
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

        itens: list[dict[str, Any]] = []
        for r in registros:
            itens.append(
                {
                    "nome": str(r.get("nome") or "").strip(),
                    "grupo": str(r.get("grupo") or "").strip(),
                    "cargo": str(r.get("cargo") or "").strip(),
                    "periodo": str(r.get("periodo") or "").strip(),
                    "ativo": bool(r.get("ativo", True)),
                    "url_remuneracao": str(r.get("url_remuneracao") or "").strip(),
                }
            )

        itens.sort(key=lambda x: (not x.get("ativo", True), x["nome"]))
        total_sem_filtro = len(itens)
        termo = _normalizar_texto_nome(busca or "")
        if termo:
            tokens_busca = [t for t in termo.split() if t]
            itens = [
                it
                for it in itens
                if all(t in _normalizar_texto_nome(str(it.get("nome") or "")) for t in tokens_busca)
            ]
        ativos = sum(1 for x in itens if x.get("ativo", True))
        exonerados = len(itens) - ativos
        return {
            "ano": ano,
            "ativos": ativos,
            "exonerados": exonerados,
            "total": len(itens),
            "total_sem_filtro": total_sem_filtro,
            "itens": itens,
        }

    async def obter_pessoal_gabinete_todos_anos(
        self,
        dep_id: int,
        anos: list[int],
        busca: str | None = None,
    ) -> dict[str, Any]:
        """Consolida pessoal de gabinete (ativos e exonerados) em todos os anos da legislatura."""
        anos_ord = sorted(set(anos), reverse=True)
        resultados = await asyncio.gather(
            *[self.obter_pessoal_gabinete(dep_id, ano=y) for y in anos_ord],
            return_exceptions=True,
        )

        por_pessoa: dict[str, dict[str, Any]] = {}
        ano_mais_recente = anos_ord[0] if anos_ord else 0
        for y, res in zip(anos_ord, resultados, strict=False):
            if not isinstance(res, dict):
                continue
            for item in res.get("itens") or []:
                nome = str(item.get("nome") or "").strip()
                if not nome:
                    continue
                chave = _normalizar_texto_nome(nome)
                if chave not in por_pessoa:
                    por_pessoa[chave] = {
                        **item,
                        "anos": [y],
                        "periodos": [item["periodo"]] if item.get("periodo") else [],
                        "ativo": bool(item.get("ativo", False)) and y == ano_mais_recente,
                    }
                else:
                    atual = por_pessoa[chave]
                    if y not in atual["anos"]:
                        atual["anos"].append(y)
                    per = str(item.get("periodo") or "").strip()
                    if per and per not in atual["periodos"]:
                        atual["periodos"].append(per)
                    if not atual.get("url_remuneracao") and item.get("url_remuneracao"):
                        atual["url_remuneracao"] = item["url_remuneracao"]
                    if y == ano_mais_recente and item.get("ativo"):
                        atual["ativo"] = True

        itens = list(por_pessoa.values())
        for it in itens:
            it["anos"] = sorted(it["anos"], reverse=True)
            it["periodo"] = " · ".join(it["periodos"][:2]) if it["periodos"] else it.get("periodo", "")

        total_sem_filtro = len(itens)
        termo = _normalizar_texto_nome(busca or "")
        if termo:
            tokens_busca = [t for t in termo.split() if t]
            itens = [
                it
                for it in itens
                if all(t in _normalizar_texto_nome(str(it.get("nome") or "")) for t in tokens_busca)
            ]

        itens.sort(key=lambda x: (not x.get("ativo", False), x["nome"]))
        ativos = sum(1 for x in itens if x.get("ativo"))
        exonerados = len(itens) - ativos
        return {
            "ano": "todos",
            "anos_consultados": sorted(anos_ord),
            "ativos": ativos,
            "exonerados": exonerados,
            "total": len(itens),
            "total_sem_filtro": total_sem_filtro,
            "itens": itens,
        }

    async def obter_dados_cnpj(self, cnpj: str) -> dict[str, Any]:
        """Consulta dados cadastrais públicos da empresa na Receita Federal via BrasilAPI
        (sem exibir nem armazenar nomes de sócios pessoas físicas)."""
        digitos = re.sub(r"\D+", "", cnpj or "")
        if len(digitos) != 14:
            return {
                "erro": "Registro sem CNPJ de 14 dígitos — consulta cadastral indisponível."
            }

        cache_path = self.cache_dir / "ficha_api" / f"cnpj_{digitos}.json"
        bruto: dict[str, Any] | None = None
        if cache_path.exists() and cache_path.stat().st_size > 0:
            try:
                bruto = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                bruto = None

        if bruto is None:
            url = f"https://brasilapi.com.br/api/cnpj/v1/{digitos}"
            async with httpx.AsyncClient(
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=20.0,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                if resp.status_code == 404:
                    return {"erro": f"CNPJ {digitos} não encontrado na base pública da Receita Federal."}
                resp.raise_for_status()
                resp_json = dict(resp.json() or {})
            # Armazena exclusivamente dados cadastrais da pessoa jurídica (sem QSA / nomes de sócios)
            bruto = {
                "cnpj": digitos,
                "razao_social": str(resp_json.get("razao_social") or "").strip(),
                "nome_fantasia": str(resp_json.get("nome_fantasia") or "").strip(),
                "descricao_situacao_cadastral": str(resp_json.get("descricao_situacao_cadastral") or "").strip(),
                "data_inicio_atividade": str(resp_json.get("data_inicio_atividade") or "")[:10],
                "capital_social": float(resp_json.get("capital_social") or 0.0),
                "porte": str(resp_json.get("porte") or "").strip(),
                "cnae_fiscal_descricao": str(resp_json.get("cnae_fiscal_descricao") or "").strip(),
                "municipio": str(resp_json.get("municipio") or "").strip(),
                "uf": str(resp_json.get("uf") or "").strip(),
            }
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(bruto, ensure_ascii=False), encoding="utf-8")

        situacao = str(bruto.get("descricao_situacao_cadastral") or "—").strip().upper()
        abertura = str(bruto.get("data_inicio_atividade") or "")[:10]
        return {
            "cnpj": digitos,
            "razao_social": str(bruto.get("razao_social") or "").strip(),
            "nome_fantasia": str(bruto.get("nome_fantasia") or "").strip(),
            "situacao": situacao,
            "situacao_irregular": situacao not in {"ATIVA", "—", ""},
            "data_abertura": abertura,
            "capital_social": float(bruto.get("capital_social") or 0.0),
            "porte": str(bruto.get("porte") or "—").strip(),
            "cnae": str(bruto.get("cnae_fiscal_descricao") or "—").strip(),
            "municipio": str(bruto.get("municipio") or "").strip(),
            "uf": str(bruto.get("uf") or "").strip(),
            "erro": None,
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
