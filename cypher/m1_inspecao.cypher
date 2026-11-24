// Consultas Cypher de inspeção do Marco 1 (M1)

// 1. Contagem de nós por rótulo
MATCH (n)
UNWIND labels(n) AS rotulo
RETURN rotulo, count(*) AS total
ORDER BY rotulo;

// 2. Contagem de relações por tipo
MATCH ()-[r]->()
RETURN type(r) AS tipo, count(*) AS total
ORDER BY tipo;

// 3. Bancada por partido na legislatura atual (inclui titulares e suplentes que exerceram mandato)
MATCH (d:Deputado)-[:FILIADO_A]->(p:Partido)
RETURN p.sigla AS partido, p.nome AS nome, count(DISTINCT d) AS deputados
ORDER BY deputados DESC;

// 4. Deputados que aparecem filiados a mais de um partido na mesma legislatura
MATCH (d:Deputado)-[:FILIADO_A]->(p:Partido)
WITH d, collect(DISTINCT p.sigla) AS partidos
WHERE size(partidos) > 1
RETURN d.id, d.nome, d.uf, partidos
ORDER BY size(partidos) DESC, d.nome;

// 5. Distribuição de deputados por UF
MATCH (d:Deputado)-[:REPRESENTA]->(u:UF)
RETURN u.sigla AS uf, count(d) AS deputados
ORDER BY deputados DESC;
