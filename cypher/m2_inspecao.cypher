// Consultas Cypher de inspeção do Marco 2 (M2)

// 1. Proposições por tipo e ano
MATCH (p:Proposicao)
RETURN p.siglaTipo AS tipo, p.ano AS ano, count(*) AS total
ORDER BY tipo, ano;

// 2. Temas com mais proposições no recorte
MATCH (p:Proposicao)-[:TRATA_DE]->(t:Tema)
RETURN t.cod AS cod, t.nome AS tema, count(p) AS proposicoes
ORDER BY proposicoes DESC
LIMIT 15;

// 3. Pares de deputados com mais proposições em coautoria (qualquer partido)
MATCH (a:Deputado)-[c:COAUTOR_COM]->(b:Deputado)
RETURN a.nome AS deputado_a, a.uf AS uf_a,
       [(a)-[:FILIADO_A]->(pa:Partido) | pa.sigla] AS partidos_a,
       b.nome AS deputado_b, b.uf AS uf_b,
       [(b)-[:FILIADO_A]->(pb:Partido) | pb.sigla] AS partidos_b,
       c.n AS proposicoes_em_comum
ORDER BY c.n DESC
LIMIT 15;

// 4. Coautoria entre partidos diferentes (sem nenhum partido em comum na 57ª legislatura)
MATCH (a:Deputado)-[c:COAUTOR_COM]->(b:Deputado)
WHERE NOT EXISTS {
  MATCH (a)-[:FILIADO_A]->(p:Partido)<-[:FILIADO_A]-(b)
}
RETURN a.nome AS deputado_a, a.uf AS uf_a,
       [(a)-[:FILIADO_A]->(pa:Partido) | pa.sigla] AS partidos_a,
       b.nome AS deputado_b, b.uf AS uf_b,
       [(b)-[:FILIADO_A]->(pb:Partido) | pb.sigla] AS partidos_b,
       c.n AS proposicoes_em_comum
ORDER BY c.n DESC
LIMIT 15;

// 5. Busca em texto completo na ementa (usando o índice proposicao_ementa_ft)
CALL db.index.fulltext.queryNodes('proposicao_ementa_ft', 'inteligência artificial')
YIELD node, score
RETURN node.siglaTipo + ' ' + toString(node.numero) + '/' + toString(node.ano) AS proposicao,
       left(node.ementa, 120) AS ementa,
       round(score, 2) AS score
LIMIT 10;
