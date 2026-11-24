"""Repositório assíncrono de consultas Cypher escritas à mão para a API GraphQL."""

from __future__ import annotations

from typing import Any

from neo4j import AsyncDriver


class Neo4jGraphRepository:
    """Executa consultas Cypher parametrizadas e em lote (para DataLoaders) contra o Neo4j."""

    def __init__(self, driver: AsyncDriver) -> None:
        self.driver = driver
        self.consultas_executadas: int = 0

    async def _run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.consultas_executadas += 1
        async with self.driver.session() as session:
            result = await session.run(query, **params)
            return [dict(record) async for record in result]

    async def verificar_saude(self) -> bool:
        rows = await self._run("RETURN 1 AS ok")
        return bool(rows and rows[0].get("ok") == 1)

    async def buscar_deputado(self, dep_id: int) -> dict[str, Any] | None:
        rows = await self._run(
            """
            MATCH (d:Deputado {id: $id})
            RETURN d.id AS id, d.nome AS nome, d.uf AS uf,
                   d.urlFoto AS urlFoto, d.uri AS uri
            """,
            id=dep_id,
        )
        return rows[0] if rows else None

    async def listar_deputados(
        self,
        uf: str | None = None,
        partido: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await self._run(
            """
            MATCH (d:Deputado)
            WHERE ($uf IS NULL OR d.uf = $uf)
              AND ($partido IS NULL OR EXISTS {
                    MATCH (d)-[:FILIADO_A]->(p:Partido {sigla: $partido})
                  })
            RETURN d.id AS id, d.nome AS nome, d.uf AS uf,
                   d.urlFoto AS urlFoto, d.uri AS uri
            ORDER BY d.nome ASC, d.id ASC
            SKIP $offset
            LIMIT $limit
            """,
            uf=uf.upper() if uf else None,
            partido=partido.upper() if partido else None,
            offset=offset,
            limit=limit,
        )

    async def listar_proposicoes(
        self,
        tema: str | None = None,
        texto: str | None = None,
        ano: int | None = None,
        tipo: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if texto and texto.strip():
            return await self._run(
                """
                CALL db.index.fulltext.queryNodes('proposicao_ementa_ft', $texto)
                YIELD node AS p, score
                WHERE ($ano IS NULL OR p.ano = $ano)
                  AND ($tipo IS NULL OR p.siglaTipo = $tipo)
                  AND ($tema IS NULL OR EXISTS {
                        MATCH (p)-[:TRATA_DE]->(t:Tema)
                        WHERE toLower(t.nome) CONTAINS toLower($tema)
                      })
                RETURN p.id AS id, p.siglaTipo AS siglaTipo, p.numero AS numero,
                       p.ano AS ano, p.ementa AS ementa,
                       p.dataApresentacao AS dataApresentacao, p.uri AS uri
                ORDER BY score DESC, p.id DESC
                SKIP $offset
                LIMIT $limit
                """,
                texto=texto.strip(),
                tema=tema.strip() if tema else None,
                ano=ano,
                tipo=tipo.upper() if tipo else None,
                offset=offset,
                limit=limit,
            )

        return await self._run(
            """
            MATCH (p:Proposicao)
            WHERE ($ano IS NULL OR p.ano = $ano)
              AND ($tipo IS NULL OR p.siglaTipo = $tipo)
              AND ($tema IS NULL OR EXISTS {
                    MATCH (p)-[:TRATA_DE]->(t:Tema)
                    WHERE toLower(t.nome) CONTAINS toLower($tema)
                  })
            RETURN p.id AS id, p.siglaTipo AS siglaTipo, p.numero AS numero,
                   p.ano AS ano, p.ementa AS ementa,
                   p.dataApresentacao AS dataApresentacao, p.uri AS uri
            ORDER BY p.ano DESC, p.numero DESC, p.id DESC
            SKIP $offset
            LIMIT $limit
            """,
            tema=tema.strip() if tema else None,
            ano=ano,
            tipo=tipo.upper() if tipo else None,
            offset=offset,
            limit=limit,
        )

    async def listar_proposicoes_de_deputado(
        self,
        dep_id: int,
        offset: int = 0,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self._run(
            """
            MATCH (:Deputado {id: $depId})-[:AUTOR_DE]->(p:Proposicao)
            RETURN p.id AS id, p.siglaTipo AS siglaTipo, p.numero AS numero,
                   p.ano AS ano, p.ementa AS ementa,
                   p.dataApresentacao AS dataApresentacao, p.uri AS uri
            ORDER BY p.ano DESC, p.id DESC
            SKIP $offset
            LIMIT $limit
            """,
            depId=dep_id,
            offset=offset,
            limit=limit,
        )

    async def buscar_votacao(self, vot_id: str) -> dict[str, Any] | None:
        rows = await self._run(
            """
            MATCH (v:Votacao {id: $id})
            RETURN v.id AS id, v.data AS data, v.ano AS ano,
                   v.siglaOrgao AS siglaOrgao, v.descricao AS descricao,
                   v.aprovacao AS aprovacao, v.votosSim AS votosSim,
                   v.votosNao AS votosNao, v.votosOutros AS votosOutros,
                   v.uri AS uri
            """,
            id=vot_id,
        )
        return rows[0] if rows else None

    async def menor_caminho_coautoria(
        self,
        dep_a: int,
        dep_b: int,
        max_saltos: int = 6,
    ) -> list[dict[str, Any]]:
        if dep_a == dep_b:
            dep = await self.buscar_deputado(dep_a)
            return [dep] if dep else []

        rows = await self._run(
            f"""
            MATCH (a:Deputado {{id: $a}}), (b:Deputado {{id: $b}})
            MATCH path = shortestPath((a)-[:COAUTOR_COM*1..{int(max_saltos)}]-(b))
            RETURN [n IN nodes(path) | {{
                id: n.id, nome: n.nome, uf: n.uf, urlFoto: n.urlFoto, uri: n.uri
            }}] AS caminho
            """,
            a=dep_a,
            b=dep_b,
        )
        return rows[0]["caminho"] if rows else []

    # ---- Consultas em lote para DataLoaders (evitam N+1) ----

    async def batch_partidos_por_deputados(
        self, dep_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        rows = await self._run(
            """
            UNWIND $ids AS depId
            MATCH (d:Deputado {id: depId})-[:FILIADO_A]->(p:Partido)
            RETURN depId, collect({
                sigla: p.sigla, nome: p.nome, id: p.id, uri: p.uri
            }) AS partidos
            """,
            ids=dep_ids,
        )
        mapa = {int(r["depId"]): list(r["partidos"]) for r in rows}
        return {i: sorted(mapa.get(i, []), key=lambda x: x["sigla"]) for i in dep_ids}

    async def batch_coautores_por_deputados(
        self,
        chaves: list[tuple[int, int]],
    ) -> dict[tuple[int, int], list[dict[str, Any]]]:
        payload = [{"depId": dep_id, "limite": limite} for dep_id, limite in chaves]
        rows = await self._run(
            """
            UNWIND $itens AS item
            MATCH (d:Deputado {id: item.depId})-[c:COAUTOR_COM]-(outro:Deputado)
            WITH item, outro, c.n AS n
            ORDER BY n DESC, outro.nome ASC
            WITH item, collect({
                deputado: {
                    id: outro.id, nome: outro.nome, uf: outro.uf,
                    urlFoto: outro.urlFoto, uri: outro.uri
                },
                proposicoesEmComum: n
            })[0..item.limite] AS lista
            RETURN item.depId AS depId, item.limite AS limite, lista
            """,
            itens=payload,
        )
        mapa = {(int(r["depId"]), int(r["limite"])): list(r["lista"]) for r in rows}
        return {k: mapa.get(k, []) for k in chaves}

    async def batch_vota_como_por_deputados(
        self,
        chaves: list[tuple[int, int, int]],
    ) -> dict[tuple[int, int, int], list[dict[str, Any]]]:
        payload = [
            {"depId": dep_id, "limite": limite, "minimo": minimo}
            for dep_id, limite, minimo in chaves
        ]
        rows = await self._run(
            """
            UNWIND $itens AS item
            MATCH (d:Deputado {id: item.depId})-[v:VOTA_COMO]-(outro:Deputado)
            WHERE v.votacoesEmComum >= item.minimo
            WITH item, outro, v.concordancia AS conc, v.votacoesEmComum AS comum
            ORDER BY conc DESC, comum DESC, outro.nome ASC
            WITH item, collect({
                deputado: {
                    id: outro.id, nome: outro.nome, uf: outro.uf,
                    urlFoto: outro.urlFoto, uri: outro.uri
                },
                concordancia: conc,
                votacoesEmComum: comum
            })[0..item.limite] AS lista
            RETURN item.depId AS depId, item.limite AS limite, item.minimo AS minimo, lista
            """,
            itens=payload,
        )
        mapa = {
            (int(r["depId"]), int(r["limite"]), int(r["minimo"])): list(r["lista"])
            for r in rows
        }
        return {k: mapa.get(k, []) for k in chaves}

    async def batch_contra_orientacao_por_deputados(
        self, dep_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        rows = await self._run(
            """
            UNWIND $ids AS depId
            MATCH (d:Deputado {id: depId})-[c:CONTRA_ORIENTACAO]->(p:Partido)
            WITH depId, c, p
            ORDER BY c.taxaDivergencia DESC, c.n DESC
            RETURN depId, collect({
                partido: {sigla: p.sigla, nome: p.nome, id: p.id, uri: p.uri},
                contra: c.n,
                totalOrientadas: c.totalOrientadas,
                taxaDivergencia: c.taxaDivergencia
            }) AS lista
            """,
            ids=dep_ids,
        )
        mapa = {int(r["depId"]): list(r["lista"]) for r in rows}
        return {i: mapa.get(i, []) for i in dep_ids}

    async def batch_temas_por_proposicoes(
        self, prop_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        rows = await self._run(
            """
            UNWIND $ids AS propId
            MATCH (p:Proposicao {id: propId})-[:TRATA_DE]->(t:Tema)
            RETURN propId, collect({cod: t.cod, nome: t.nome}) AS temas
            """,
            ids=prop_ids,
        )
        mapa = {int(r["propId"]): list(r["temas"]) for r in rows}
        return {i: sorted(mapa.get(i, []), key=lambda x: x["nome"]) for i in prop_ids}

    async def batch_autores_por_proposicoes(
        self,
        chaves: list[tuple[int, int]],
    ) -> dict[tuple[int, int], list[dict[str, Any]]]:
        payload = [{"propId": prop_id, "limite": limite} for prop_id, limite in chaves]
        rows = await self._run(
            """
            UNWIND $itens AS item
            MATCH (d:Deputado)-[r:AUTOR_DE]->(p:Proposicao {id: item.propId})
            WITH item, d, coalesce(r.ordem, 9999) AS ordem
            ORDER BY ordem ASC, d.nome ASC
            WITH item, collect({
                id: d.id, nome: d.nome, uf: d.uf, urlFoto: d.urlFoto, uri: d.uri
            })[0..item.limite] AS autores
            RETURN item.propId AS propId, item.limite AS limite, autores
            """,
            itens=payload,
        )
        mapa = {(int(r["propId"]), int(r["limite"])): list(r["autores"]) for r in rows}
        return {k: mapa.get(k, []) for k in chaves}

    async def batch_proposicoes_por_votacoes(
        self, vot_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        rows = await self._run(
            """
            UNWIND $ids AS votId
            MATCH (v:Votacao {id: votId})-[:SOBRE]->(p:Proposicao)
            RETURN votId, collect({
                id: p.id, siglaTipo: p.siglaTipo, numero: p.numero, ano: p.ano,
                ementa: p.ementa, dataApresentacao: p.dataApresentacao, uri: p.uri
            }) AS props
            """,
            ids=vot_ids,
        )
        mapa = {str(r["votId"]): list(r["props"]) for r in rows}
        return {vid: mapa.get(vid, []) for vid in vot_ids}

    async def batch_orientacoes_por_votacoes(
        self, vot_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        rows = await self._run(
            """
            UNWIND $ids AS votId
            MATCH (p:Partido)-[o:ORIENTOU]->(v:Votacao {id: votId})
            WITH votId, p, o
            ORDER BY p.sigla ASC
            RETURN votId, collect({
                partido: {sigla: p.sigla, nome: p.nome, id: p.id, uri: p.uri},
                orientacao: o.orientacao,
                siglaBancada: o.siglaBancada
            }) AS orientacoes
            """,
            ids=vot_ids,
        )
        mapa = {str(r["votId"]): list(r["orientacoes"]) for r in rows}
        return {vid: mapa.get(vid, []) for vid in vot_ids}

    # ---- Consultas ricas para a interface HTMX (M7) ----

    async def resumo_geral_ui(self) -> dict[str, Any]:
        nos = await self._run(
            "MATCH (n) UNWIND labels(n) AS r RETURN r AS rotulo, count(*) AS total"
        )
        rels = await self._run(
            "MATCH ()-[r]->() RETURN type(r) AS tipo, count(*) AS total"
        )
        partidos = await self._run(
            "MATCH (p:Partido) RETURN p.sigla AS sigla, p.nome AS nome ORDER BY p.sigla"
        )
        comunidades = await self._run(
            """
            MATCH (d:Deputado)
            WHERE d.comunidadeVoto IS NOT NULL
            WITH d.comunidadeVoto AS id, collect(d) AS deps
            UNWIND deps AS d
            OPTIONAL MATCH (d)-[:FILIADO_A]->(p:Partido)
            WITH id, size(deps) AS total, p.sigla AS sigla, count(DISTINCT d) AS n
            ORDER BY total DESC, n DESC
            WITH id, total, collect(sigla)[0..3] AS principais
            RETURN id, total, principais
            ORDER BY total DESC
            """
        )
        grupos = []
        for idx, c in enumerate(comunidades, start=1):
            siglas = [s for s in (c.get("principais") or []) if s]
            resumo_partidos = ", ".join(siglas) if siglas else "Misto"
            grupos.append(
                {
                    "id": int(c["id"]),
                    "numero": idx,
                    "total": int(c["total"]),
                    "principais": siglas,
                    "rotulo": f"Grupo {idx} ({resumo_partidos})",
                }
            )
        return {
            "nos": {r["rotulo"]: int(r["total"]) for r in nos},
            "relacoes": {r["tipo"]: int(r["total"]) for r in rels},
            "partidos": partidos,
            "comunidades": grupos,
        }

    async def buscar_deputados_ui(
        self,
        busca: str | None = None,
        uf: str | None = None,
        partido: str | None = None,
        comunidade: int | None = None,
        ordenar: str = "nome",
        offset: int = 0,
        limit: int = 24,
    ) -> dict[str, Any]:
        ordem_sql = {
            "nome": "d.nome ASC",
            "pagerank": "coalesce(d.pagerankCoautoria, 0.0) DESC, d.nome ASC",
            "betweenness": "coalesce(d.betweennessCoautoria, 0.0) DESC, d.nome ASC",
            "divergencia": "taxaDivergencia DESC, totalContra DESC, d.nome ASC",
            "presenca_asc": "taxaPresenca ASC, d.nome ASC",
            "presenca_desc": "taxaPresenca DESC, d.nome ASC",
            "trocas": "numPartidos DESC, d.nome ASC",
        }.get(ordenar, "d.nome ASC")

        filtro = """
            MATCH (d:Deputado)
            WHERE ($busca IS NULL OR toLower(d.nome) CONTAINS toLower($busca))
              AND ($uf IS NULL OR d.uf = $uf)
              AND ($comunidade IS NULL OR d.comunidadeVoto = $comunidade)
              AND ($partido IS NULL OR EXISTS {
                    MATCH (d)-[:FILIADO_A]->(p:Partido {sigla: $partido})
                  })
        """
        params = {
            "busca": busca.strip() if busca and busca.strip() else None,
            "uf": uf.strip().upper() if uf and uf.strip() else None,
            "partido": partido.strip().upper() if partido and partido.strip() else None,
            "comunidade": comunidade,
            "offset": offset,
            "limit": limit,
        }
        total_rows = await self._run(f"{filtro} RETURN count(d) AS total", **params)
        total = int(total_rows[0]["total"]) if total_rows else 0

        vot_rows = await self._run("MATCH (v:Votacao) RETURN count(v) AS total")
        total_votacoes = max(1, int(vot_rows[0]["total"]) if vot_rows else 1)

        itens = await self._run(
            f"""
            {filtro}
            OPTIONAL MATCH (d)-[c:CONTRA_ORIENTACAO]->(:Partido)
            WITH d,
                 coalesce(max(c.taxaDivergencia), 0.0) AS taxaDivergencia,
                 coalesce(sum(c.n), 0) AS totalContra
            OPTIONAL MATCH (d)-[:VOTOU]->(v:Votacao)
            WITH d, taxaDivergencia, totalContra, count(DISTINCT v) AS totalVotos
            WITH d, taxaDivergencia, totalContra, totalVotos,
                 round(toFloat(totalVotos) / $totalVotacoes, 4) AS taxaPresenca,
                 [(d)-[:FILIADO_A]->(p:Partido) | p.sigla] AS partidos
            WITH d, taxaDivergencia, totalContra, totalVotos, taxaPresenca, partidos,
                 size(partidos) AS numPartidos
            RETURN d.id AS id,
                   d.nome AS nome,
                   d.uf AS uf,
                   d.urlFoto AS urlFoto,
                   d.uri AS uri,
                   d.comunidadeVoto AS comunidadeVoto,
                   round(coalesce(d.pagerankCoautoria, 0.0), 4) AS pagerank,
                   round(coalesce(d.betweennessCoautoria, 0.0), 1) AS betweenness,
                   round(taxaDivergencia, 4) AS taxaDivergencia,
                   totalContra,
                   totalVotos,
                   taxaPresenca,
                   partidos,
                   numPartidos
            ORDER BY {ordem_sql}
            SKIP $offset
            LIMIT $limit
            """,
            totalVotacoes=total_votacoes,
            **params,
        )
        return {"total": total, "itens": itens}

    async def buscar_ficha_grafo(self, dep_id: int) -> dict[str, Any] | None:
        rows = await self._run(
            """
            MATCH (d:Deputado {id: $id})
            OPTIONAL MATCH (d)-[:AUTOR_DE]->(pr:Proposicao)
            WITH d, count(DISTINCT pr) AS totalProposicoes
            OPTIONAL MATCH (d)-[:VOTOU]->(v:Votacao)
            WITH d, totalProposicoes, count(DISTINCT v) AS totalVotos
            RETURN d.id AS id, d.nome AS nome, d.uf AS uf,
                   d.urlFoto AS urlFoto, d.uri AS uri, d.email AS email,
                   d.comunidadeVoto AS comunidadeVoto,
                   d.comunidadeVotoLeiden AS comunidadeVotoLeiden,
                   round(coalesce(d.pagerankCoautoria, 0.0), 4) AS pagerank,
                   round(coalesce(d.betweennessCoautoria, 0.0), 2) AS betweenness,
                   totalProposicoes, totalVotos,
                   [(d)-[:FILIADO_A]->(p:Partido) | {sigla: p.sigla, nome: p.nome, id: p.id}] AS partidos
            """,
            id=dep_id,
        )
        return rows[0] if rows else None

    async def buscar_aliancas_deputado(self, dep_id: int) -> dict[str, Any]:
        coautores = await self._run(
            """
            MATCH (d:Deputado {id: $id})-[c:COAUTOR_COM]-(o:Deputado)
            RETURN o.id AS id, o.nome AS nome, o.uf AS uf, o.urlFoto AS urlFoto,
                   [(o)-[:FILIADO_A]->(p:Partido) | p.sigla] AS partidos,
                   c.n AS n
            ORDER BY c.n DESC, o.nome ASC
            LIMIT 12
            """,
            id=dep_id,
        )
        vota_como = await self._run(
            """
            MATCH (d:Deputado {id: $id})-[v:VOTA_COMO]-(o:Deputado)
            WHERE v.votacoesEmComum >= 50
            RETURN o.id AS id, o.nome AS nome, o.uf AS uf, o.urlFoto AS urlFoto,
                   [(o)-[:FILIADO_A]->(p:Partido) | p.sigla] AS partidos,
                   v.concordancia AS concordancia, v.votacoesEmComum AS votacoesEmComum
            ORDER BY v.concordancia DESC, v.votacoesEmComum DESC
            LIMIT 12
            """,
            id=dep_id,
        )
        temas_similares = await self._run(
            """
            MATCH (d:Deputado {id: $id})-[r:TEMAS_SIMILARES]->(o:Deputado)
            RETURN o.id AS id, o.nome AS nome, o.uf AS uf, o.urlFoto AS urlFoto,
                   [(o)-[:FILIADO_A]->(p:Partido) | p.sigla] AS partidos,
                   round(r.similaridade, 4) AS similaridade
            ORDER BY r.similaridade DESC
            LIMIT 10
            """,
            id=dep_id,
        )
        contra = await self._run(
            """
            MATCH (d:Deputado {id: $id})-[c:CONTRA_ORIENTACAO]->(p:Partido)
            RETURN p.sigla AS sigla, p.nome AS nome, c.n AS contra,
                   c.totalOrientadas AS totalOrientadas, c.taxaDivergencia AS taxaDivergencia
            ORDER BY c.taxaDivergencia DESC
            """,
            id=dep_id,
        )
        return {
            "coautores": coautores,
            "vota_como": vota_como,
            "temas_similares": temas_similares,
            "contra_orientacao": contra,
        }

    async def listar_todos_deputados_simples(self) -> list[dict[str, Any]]:
        return await self._run(
            """
            MATCH (d:Deputado)
            RETURN d.id AS id, d.nome AS nome, d.uf AS uf,
                   [(d)-[:FILIADO_A]->(p:Partido) | p.sigla] AS partidos
            ORDER BY d.nome ASC
            """
        )

    async def comparar_dois_deputados(self, dep_a: int, dep_b: int) -> dict[str, Any]:
        rows = await self._run(
            """
            MATCH (a:Deputado {id: $a}), (b:Deputado {id: $b})
            OPTIONAL MATCH (a)-[c:COAUTOR_COM]-(b)
            OPTIONAL MATCH (a)-[v:VOTA_COMO]-(b)
            RETURN coalesce(c.n, 0) AS projetosEmComum,
                   v.concordancia AS concordancia,
                   coalesce(v.votacoesEmComum, 0) AS votacoesEmComum
            """,
            a=dep_a,
            b=dep_b,
        )
        return rows[0] if rows else {"projetosEmComum": 0, "concordancia": None, "votacoesEmComum": 0}
