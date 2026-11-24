// Consultas Cypher de inspeção das análises GDS do Marco 5 (M5)
// (Requer execução prévia de: camara analisar-m5)

// 1. Composição partidária de cada comunidade de votação (Louvain)
MATCH (d:Deputado)
WHERE d.comunidadeVoto IS NOT NULL
WITH d.comunidadeVoto AS comunidade, collect(d) AS deps
UNWIND deps AS d
OPTIONAL MATCH (d)-[:FILIADO_A]->(p:Partido)
WITH comunidade, size(deps) AS tamanho, p.sigla AS partido, count(DISTINCT d) AS n
ORDER BY tamanho DESC, n DESC
WITH comunidade, tamanho, collect({partido: partido, deputados: n})[0..6] AS topPartidos
RETURN comunidade, tamanho, topPartidos
ORDER BY tamanho DESC;

// 2. Coesão dos partidos em relação às comunidades reais de votação
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
ORDER BY taxaCoesao ASC, total DESC;

// 3. Deputados mais centrais na rede de coautoria (PageRank e Betweenness)
MATCH (d:Deputado)
WHERE d.betweennessCoautoria IS NOT NULL
RETURN d.nome AS deputado, d.uf AS uf,
       [(d)-[:FILIADO_A]->(p:Partido) | p.sigla] AS partidos,
       round(d.pagerankCoautoria, 4) AS pagerank,
       round(d.betweennessCoautoria, 2) AS betweenness
ORDER BY d.betweennessCoautoria DESC
LIMIT 15;

// 4. Similaridade de pauta temática entre deputados de partidos diferentes
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
LIMIT 15;
