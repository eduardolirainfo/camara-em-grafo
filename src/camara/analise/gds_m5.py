"""Análises de grafo do Marco 5 (M5) usando Neo4j Graph Data Science (GDS)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from neo4j import Driver

PROJECTION_VOTACAO = "camara_vota_como_m5"
PROJECTION_COAUTORIA = "camara_coautoria_m5"
PROJECTION_TEMAS = "camara_temas_m5"


@dataclass
class ResumoAnaliseM5:
    legislatura_id: int
    parametros: dict[str, Any]
    comunidades_louvain: dict[str, Any]
    comunidades_leiden: dict[str, Any]
    centralidade_coautoria: dict[str, Any]
    similaridade_temas: dict[str, Any]
    tempo_total_segundos: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _drop_projection(driver: Driver, nome: str) -> None:
    with driver.session() as session:
        session.run(
            "CALL gds.graph.drop($nome, false) YIELD graphName RETURN graphName",
            nome=nome,
        ).consume()


def executar_comunidades_votacao(
    driver: Driver,
    legislatura_id: int = 57,
    min_votacoes_comum: int = 50,
    min_concordancia: float = 0.70,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Executa Louvain e Leiden sobre VOTA_COMO filtrado e compara blocos reais com partidos."""
    _drop_projection(driver, PROJECTION_VOTACAO)
    try:
        t0 = time.perf_counter()
        with driver.session() as session:
            proj = session.run(
                """
                MATCH (a:Deputado)-[r:VOTA_COMO {legislatura: $leg}]->(b:Deputado)
                WHERE r.votacoesEmComum >= $minVot
                  AND r.concordancia >= $minConc
                WITH gds.graph.project(
                    $nome,
                    a,
                    b,
                    {relationshipProperties: {peso: r.concordancia}},
                    {undirectedRelationshipTypes: ['*']}
                ) AS g
                RETURN g.nodeCount AS nos, g.relationshipCount AS relacoes
                """,
                nome=PROJECTION_VOTACAO,
                leg=legislatura_id,
                minVot=min_votacoes_comum,
                minConc=min_concordancia,
            ).single()

            louv = session.run(
                """
                CALL gds.louvain.write($nome, {
                    relationshipWeightProperty: 'peso',
                    writeProperty: 'comunidadeVoto'
                })
                YIELD communityCount, modularity, computeMillis, writeMillis
                RETURN communityCount, modularity, computeMillis, writeMillis
                """,
                nome=PROJECTION_VOTACAO,
            ).single()

            leid = session.run(
                """
                CALL gds.leiden.write($nome, {
                    relationshipWeightProperty: 'peso',
                    writeProperty: 'comunidadeVotoLeiden',
                    randomSeed: 42
                })
                YIELD communityCount, modularity, computeMillis, writeMillis
                RETURN communityCount, modularity, computeMillis, writeMillis
                """,
                nome=PROJECTION_VOTACAO,
            ).single()

            blocos_louvain = [
                dict(r)
                for r in session.run(
                    """
                    MATCH (d:Deputado)
                    WHERE d.comunidadeVoto IS NOT NULL
                    WITH d.comunidadeVoto AS comunidade, collect(d) AS deps
                    UNWIND deps AS d
                    OPTIONAL MATCH (d)-[:FILIADO_A]->(p:Partido)
                    WITH comunidade, size(deps) AS tamanho, p.sigla AS partido, count(DISTINCT d) AS n
                    ORDER BY tamanho DESC, n DESC
                    WITH comunidade, tamanho,
                         collect({partido: partido, deputados: n})[0..5] AS topPartidos
                    RETURN comunidade, tamanho, topPartidos
                    ORDER BY tamanho DESC
                    LIMIT 10
                    """
                )
            ]

            coesao_partidos = [
                dict(r)
                for r in session.run(
                    """
                    MATCH (d:Deputado)-[:FILIADO_A]->(p:Partido)
                    WHERE d.comunidadeVoto IS NOT NULL
                    WITH p.sigla AS partido, d.comunidadeVoto AS com, count(DISTINCT d) AS n
                    ORDER BY partido, n DESC
                    WITH partido, sum(n) AS total, collect({comunidade: com, n: n}) AS dist
                    WHERE total >= 10
                    RETURN partido, total,
                           dist[0].comunidade AS comunidadePrincipal,
                           dist[0].n AS naPrincipal,
                           round(toFloat(dist[0].n) / total, 4) AS taxaCoesao,
                           size(dist) AS numComunidades
                    ORDER BY taxaCoesao ASC, total DESC
                    """
                )
            ]

        duracao = round(time.perf_counter() - t0, 4)
        res_louvain = {
            "nos_projetados": int(proj["nos"]),
            "relacoes_projetadas": int(proj["relacoes"]),
            "community_count": int(louv["communityCount"]),
            "modularity": round(float(louv["modularity"]), 4),
            "compute_millis": int(louv["computeMillis"]),
            "write_millis": int(louv["writeMillis"]),
            "blocos": blocos_louvain,
            "coesao_partidos": coesao_partidos,
            "tempo_segundos": duracao,
        }
        res_leiden = {
            "community_count": int(leid["communityCount"]),
            "modularity": round(float(leid["modularity"]), 4),
            "compute_millis": int(leid["computeMillis"]),
            "write_millis": int(leid["writeMillis"]),
        }
        return res_louvain, res_leiden
    finally:
        _drop_projection(driver, PROJECTION_VOTACAO)


def executar_centralidade_coautoria(
    driver: Driver,
    legislatura_id: int = 57,
    min_coautorias: int = 5,
) -> dict[str, Any]:
    """Executa PageRank e Betweenness Centrality sobre COAUTOR_COM (n >= min_coautorias)."""
    _drop_projection(driver, PROJECTION_COAUTORIA)
    try:
        t0 = time.perf_counter()
        with driver.session() as session:
            proj = session.run(
                """
                MATCH (a:Deputado)-[r:COAUTOR_COM {legislatura: $leg}]->(b:Deputado)
                WHERE r.n >= $minN
                WITH gds.graph.project(
                    $nome,
                    a,
                    b,
                    {relationshipProperties: {peso: toFloat(r.n)}},
                    {undirectedRelationshipTypes: ['*']}
                ) AS g
                RETURN g.nodeCount AS nos, g.relationshipCount AS relacoes
                """,
                nome=PROJECTION_COAUTORIA,
                leg=legislatura_id,
                minN=min_coautorias,
            ).single()

            pr = session.run(
                """
                CALL gds.pageRank.write($nome, {
                    relationshipWeightProperty: 'peso',
                    writeProperty: 'pagerankCoautoria'
                })
                YIELD ranIterations, didConverge, computeMillis, writeMillis
                RETURN ranIterations, didConverge, computeMillis, writeMillis
                """,
                nome=PROJECTION_COAUTORIA,
            ).single()

            btw = session.run(
                """
                CALL gds.betweenness.write($nome, {
                    writeProperty: 'betweennessCoautoria'
                })
                YIELD computeMillis, writeMillis
                RETURN computeMillis, writeMillis
                """,
                nome=PROJECTION_COAUTORIA,
            ).single()

            top_pr = [
                dict(r)
                for r in session.run(
                    """
                    MATCH (d:Deputado)
                    WHERE d.pagerankCoautoria IS NOT NULL
                    RETURN d.id AS id, d.nome AS nome, d.uf AS uf,
                           [(d)-[:FILIADO_A]->(p:Partido) | p.sigla] AS partidos,
                           round(d.pagerankCoautoria, 4) AS pagerank
                    ORDER BY d.pagerankCoautoria DESC
                    LIMIT 10
                    """
                )
            ]

            top_btw = [
                dict(r)
                for r in session.run(
                    """
                    MATCH (d:Deputado)
                    WHERE d.betweennessCoautoria IS NOT NULL
                    RETURN d.id AS id, d.nome AS nome, d.uf AS uf,
                           [(d)-[:FILIADO_A]->(p:Partido) | p.sigla] AS partidos,
                           round(d.betweennessCoautoria, 2) AS betweenness
                    ORDER BY d.betweennessCoautoria DESC
                    LIMIT 10
                    """
                )
            ]

        return {
            "nos_projetados": int(proj["nos"]),
            "relacoes_projetadas": int(proj["relacoes"]),
            "pagerank_iteracoes": int(pr["ranIterations"]),
            "pagerank_convergiu": bool(pr["didConverge"]),
            "pagerank_compute_millis": int(pr["computeMillis"]),
            "betweenness_compute_millis": int(btw["computeMillis"]),
            "top_pagerank": top_pr,
            "top_betweenness": top_btw,
            "tempo_segundos": round(time.perf_counter() - t0, 4),
        }
    finally:
        _drop_projection(driver, PROJECTION_COAUTORIA)


def executar_similaridade_temas(
    driver: Driver,
    top_k: int = 5,
    min_similaridade: float = 0.5,
) -> dict[str, Any]:
    """Executa Node Similarity (GDS) entre deputados pelos temas das proposições que assinam."""
    _drop_projection(driver, PROJECTION_TEMAS)
    try:
        t0 = time.perf_counter()
        with driver.session() as session:
            # Limpa relações derivadas anteriores para idempotência
            session.run("MATCH ()-[r:TEMAS_SIMILARES]->() DELETE r").consume()

            proj = session.run(
                """
                MATCH (d:Deputado)-[:AUTOR_DE]->(p:Proposicao)-[:TRATA_DE]->(t:Tema)
                WITH d, t, count(DISTINCT p) AS peso
                WITH gds.graph.project(
                    $nome,
                    d,
                    t,
                    {relationshipProperties: {peso: toFloat(peso)}}
                ) AS g
                RETURN g.nodeCount AS nos, g.relationshipCount AS relacoes
                """,
                nome=PROJECTION_TEMAS,
            ).single()

            sim = session.run(
                """
                CALL gds.nodeSimilarity.write($nome, {
                    relationshipWeightProperty: 'peso',
                    topK: $topK,
                    similarityCutoff: $cutoff,
                    writeRelationshipType: 'TEMAS_SIMILARES',
                    writeProperty: 'similaridade'
                })
                YIELD nodesCompared, relationshipsWritten, computeMillis, writeMillis
                RETURN nodesCompared, relationshipsWritten, computeMillis, writeMillis
                """,
                nome=PROJECTION_TEMAS,
                topK=top_k,
                cutoff=min_similaridade,
            ).single()

            top_pares = [
                dict(r)
                for r in session.run(
                    """
                    MATCH (a:Deputado)-[r:TEMAS_SIMILARES]->(b:Deputado)
                    WHERE a.id < b.id
                      AND NOT EXISTS {
                        MATCH (a)-[:FILIADO_A]->(p:Partido)<-[:FILIADO_A]-(b)
                      }
                    RETURN a.nome AS deputado_a, a.uf AS uf_a,
                           [(a)-[:FILIADO_A]->(pa:Partido) | pa.sigla] AS partidos_a,
                           b.nome AS deputado_b, b.uf AS uf_b,
                           [(b)-[:FILIADO_A]->(pb:Partido) | pb.sigla] AS partidos_b,
                           round(r.similaridade, 4) AS similaridade
                    ORDER BY r.similaridade DESC
                    LIMIT 10
                    """
                )
            ]

        return {
            "nos_projetados": int(proj["nos"]),
            "relacoes_projetadas": int(proj["relacoes"]),
            "nodes_compared": int(sim["nodesCompared"]),
            "relationships_written": int(sim["relationshipsWritten"]),
            "compute_millis": int(sim["computeMillis"]),
            "top_interpartidarios": top_pares,
            "tempo_segundos": round(time.perf_counter() - t0, 4),
        }
    finally:
        _drop_projection(driver, PROJECTION_TEMAS)


def executar_analises_m5(
    driver: Driver,
    legislatura_id: int = 57,
    min_votacoes_comum: int = 50,
    min_concordancia: float = 0.70,
    min_coautorias: int = 5,
    top_k_temas: int = 5,
    min_similaridade_temas: float = 0.5,
) -> ResumoAnaliseM5:
    inicio = time.perf_counter()

    louvain, leiden = executar_comunidades_votacao(
        driver=driver,
        legislatura_id=legislatura_id,
        min_votacoes_comum=min_votacoes_comum,
        min_concordancia=min_concordancia,
    )
    centralidade = executar_centralidade_coautoria(
        driver=driver,
        legislatura_id=legislatura_id,
        min_coautorias=min_coautorias,
    )
    temas = executar_similaridade_temas(
        driver=driver,
        top_k=top_k_temas,
        min_similaridade=min_similaridade_temas,
    )

    return ResumoAnaliseM5(
        legislatura_id=legislatura_id,
        parametros={
            "min_votacoes_comum": min_votacoes_comum,
            "min_concordancia": min_concordancia,
            "min_coautorias": min_coautorias,
            "top_k_temas": top_k_temas,
            "min_similaridade_temas": min_similaridade_temas,
        },
        comunidades_louvain=louvain,
        comunidades_leiden=leiden,
        centralidade_coautoria=centralidade,
        similaridade_temas=temas,
        tempo_total_segundos=round(time.perf_counter() - inicio, 4),
    )
