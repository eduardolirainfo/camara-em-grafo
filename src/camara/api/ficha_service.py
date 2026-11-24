"""Serviço de ficha completa do deputado: combina Neo4j + API da Câmara (com cache) + funcionarios.csv."""

from __future__ import annotations

import csv
import logging
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
        for d in itens_ano:
            m = int(d.get("mes") or 0)
            if 1 <= m <= 12:
                totais_mes[m] += float(d.get("valorLiquido") or d.get("valorDocumento") or 0.0)

        mensal = [
            {"mes": m, "sigla": siglas_mes[m - 1], "valor": round(totais_mes[m], 2)}
            for m in range(1, 13)
        ]

        itens = [d for d in itens_ano if mes is None or int(d.get("mes") or 0) == mes]

        total = 0.0
        por_tipo: dict[str, float] = defaultdict(float)
        por_fornecedor: dict[str, float] = defaultdict(float)

        for d in itens:
            valor = float(d.get("valorLiquido") or d.get("valorDocumento") or 0.0)
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
