// Consultas Cypher de inspeção do Marco 3 (M3)

// 1. Votações nominais carregadas por ano e órgão
MATCH (v:Votacao)
RETURN v.ano AS ano, v.siglaOrgao AS orgao, count(*) AS votacoes
ORDER BY ano, votacoes DESC;

// 2. Distribuição dos tipos de voto registrados
MATCH ()-[r:VOTOU]->()
RETURN r.voto AS voto, count(*) AS total
ORDER BY total DESC;

// 3. Pares de deputados que mais votam igual (mesmo ou diferente partido, >= 100 votações em comum)
MATCH (a:Deputado)-[v:VOTA_COMO]->(b:Deputado)
WHERE v.votacoesEmComum >= 100
RETURN a.nome AS deputado_a, a.uf AS uf_a,
       [(a)-[:FILIADO_A]->(pa:Partido) | pa.sigla] AS partidos_a,
       b.nome AS deputado_b, b.uf AS uf_b,
       [(b)-[:FILIADO_A]->(pb:Partido) | pb.sigla] AS partidos_b,
       v.concordancia AS concordancia,
       v.votacoesEmComum AS votacoes_em_comum
ORDER BY v.concordancia DESC, v.votacoesEmComum DESC
LIMIT 15;

// 4. Maior concordância de votos entre deputados SEM partido em comum na 57ª legislatura
MATCH (a:Deputado)-[v:VOTA_COMO]->(b:Deputado)
WHERE v.votacoesEmComum >= 100
  AND NOT EXISTS {
    MATCH (a)-[:FILIADO_A]->(p:Partido)<-[:FILIADO_A]-(b)
  }
RETURN a.nome AS deputado_a, a.uf AS uf_a,
       [(a)-[:FILIADO_A]->(pa:Partido) | pa.sigla] AS partidos_a,
       b.nome AS deputado_b, b.uf AS uf_b,
       [(b)-[:FILIADO_A]->(pb:Partido) | pb.sigla] AS partidos_b,
       v.concordancia AS concordancia,
       v.votacoesEmComum AS votacoes_em_comum
ORDER BY v.concordancia DESC, v.votacoesEmComum DESC
LIMIT 15;

// 5. Deputados que mais votaram contra a orientação do próprio partido (mínimo 50 votações orientadas)
MATCH (d:Deputado)-[c:CONTRA_ORIENTACAO]->(p:Partido)
WHERE c.totalOrientadas >= 50
RETURN d.nome AS deputado, d.uf AS uf, p.sigla AS partido,
       c.n AS contra, c.totalOrientadas AS total_orientadas,
       c.taxaDivergencia AS taxa_divergencia
ORDER BY c.taxaDivergencia DESC, c.n DESC
LIMIT 15;
