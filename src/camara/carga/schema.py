"""Restrições de unicidade e índices do grafo no Neo4j (M1 e M2)."""

CONSTRAINTS_M1: tuple[str, ...] = (
    (
        "CREATE CONSTRAINT deputado_id_unico IF NOT EXISTS "
        "FOR (d:Deputado) REQUIRE d.id IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT partido_sigla_unica IF NOT EXISTS "
        "FOR (p:Partido) REQUIRE p.sigla IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT legislatura_id_unico IF NOT EXISTS "
        "FOR (l:Legislatura) REQUIRE l.id IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT uf_sigla_unica IF NOT EXISTS "
        "FOR (u:UF) REQUIRE u.sigla IS UNIQUE"
    ),
)

CONSTRAINTS_M2: tuple[str, ...] = (
    (
        "CREATE CONSTRAINT proposicao_id_unico IF NOT EXISTS "
        "FOR (p:Proposicao) REQUIRE p.id IS UNIQUE"
    ),
    (
        "CREATE CONSTRAINT tema_cod_unico IF NOT EXISTS "
        "FOR (t:Tema) REQUIRE t.cod IS UNIQUE"
    ),
)

INDEXES_M2: tuple[str, ...] = (
    (
        "CREATE FULLTEXT INDEX proposicao_ementa_ft IF NOT EXISTS "
        "FOR (p:Proposicao) ON EACH [p.ementa]"
    ),
)

CONSTRAINTS_M3: tuple[str, ...] = (
    (
        "CREATE CONSTRAINT votacao_id_unico IF NOT EXISTS "
        "FOR (v:Votacao) REQUIRE v.id IS UNIQUE"
    ),
)
