# Câmara em Grafo

Dados abertos da Câmara dos Deputados (57ª Legislatura, 2023–2027) modelados em grafo no **Neo4j + Graph Data Science (GDS)**, com coleta assíncrona em **Python**, API **GraphQL** (FastAPI + Strawberry), rotas **REST** de benchmark e interface editorial responsiva em **HTMX + Chart.js** (tema Claro e Escuro).

O objetivo é tornar auditável a atuação parlamentar além da sigla do partido — gastos da Cota Parlamentar (CEAP), rede de fornecedores (CNPJs), presença real em plenário, votos contra o próprio partido, trechos aéreos, evolução salarial de funcionários de gabinete, projetos e discursos — sempre com dados públicos oficiais, fórmulas transparentes e link direto para o registro de origem na Câmara.

- **Fontes oficiais:** [API v2 de Dados Abertos da Câmara](https://dadosabertos.camara.leg.br/swagger/api.html), [Arquivos em Lote (CSV/ZIP)](https://dadosabertos.camara.leg.br/arquivos/) e [Portal de Transparência / Pessoal de Gabinete](https://www.camara.leg.br/transparencia/)

---

## Pré-requisitos

- **Python 3.12+** (testado em Python 3.13.5)
- **Docker** e **Docker Compose**

---

## Como subir o ambiente local

### 1. Clonar e configurar variáveis de ambiente

```powershell
cp .env.example .env
```

Abra o arquivo `.env` e ajuste `NEO4J_PASSWORD` se desejar (o arquivo `.env` está no `.gitignore` e não é versionado).

### 2. Subir o Neo4j (Community + Graph Data Science)

```powershell
docker compose up -d
```

Aguarde o contêiner ficar `healthy` (na primeira execução ele baixa e habilita o plugin Graph Data Science automaticamente):

```powershell
docker ps --filter name=camara-neo4j
```

Quando estiver pronto:
- **Neo4j Browser:** http://localhost:7474 (usuário `neo4j` e senha definida no `.env`)
- **Bolt:** `bolt://localhost:7687`

### 3. Criar o ambiente virtual e instalar o projeto

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\pip.exe install -e ".[dev]"
```

*(No Linux/macOS: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.)*

---

## Como executar a coleta e a carga

O CLI `camara` fica disponível dentro do `.venv`:

### Marco 1 — Deputados, partidos, UFs e 57ª legislatura

```powershell
# 1. Coletar dados da 57ª legislatura (API -> cache em disco) e carregar no Neo4j
.\.venv\Scripts\camara.exe carregar-m1

# 2. Recarregar tudo do M1 a partir do cache em disco, sem acessar a rede
.\.venv\Scripts\camara.exe recarregar-m1
.\.venv\Scripts\camara.exe recarregar-m1 --limpar   # recarga limpa do zero
```

### Marco 2 — Proposições, temas, autores e `COAUTOR_COM`

Usa os arquivos anuais em lote (CSV) de `dadosabertos.camara.leg.br/arquivos` (anos 2023–2026; recorte padrão: `PL,PLP,PEC,PDL,PRC`, ajustável via `--tipos`):

```powershell
# 1. Baixar CSVs em lote (para cache/arquivos/), carregar no Neo4j e calcular COAUTOR_COM
.\.venv\Scripts\camara.exe carregar-m2

# 2. Recarregar o M2 exclusivamente do cache em disco, sem acessar a rede
.\.venv\Scripts\camara.exe recarregar-m2
.\.venv\Scripts\camara.exe recarregar-m2 --limpar-m2   # limpa apenas Proposicao/Tema/COAUTOR_COM

# 3. Verificar status do banco, versões do Neo4j/GDS e contagem atual de nós/relações
.\.venv\Scripts\camara.exe status
```

### Marco 3 — Votações, votos, orientações, `VOTA_COMO` e `CONTRA_ORIENTACAO`

Usa os arquivos anuais em lote (CSV) de `votacoes`, `votacoesVotos`, `votacoesOrientacoes` e `votacoesProposicoes` (2023–2026):

```powershell
# 1. Baixar CSVs em lote (para cache/arquivos/), carregar no Neo4j e calcular VOTA_COMO + CONTRA_ORIENTACAO
.\.venv\Scripts\camara.exe carregar-m3

# 2. Recarregar o M3 exclusivamente do cache em disco, sem acessar a rede
.\.venv\Scripts\camara.exe recarregar-m3
.\.venv\Scripts\camara.exe recarregar-m3 --limpar-m3   # limpa apenas Votacao/VOTA_COMO/CONTRA_ORIENTACAO
```

### Marco 5 — Análises com Graph Data Science (GDS)

Executa Louvain e Leiden sobre `VOTA_COMO`, PageRank e Betweenness sobre `COAUTOR_COM` e Node Similarity por temas, gravando as propriedades nos nós `Deputado` e criando `TEMAS_SIMILARES`:

```powershell
.\.venv\Scripts\camara.exe analisar-m5 --salvar-medicao medicoes/m5_analises.json
```

Consultas Cypher prontas para inspecionar os resultados em [`cypher/m5_analises.cypher`](cypher/m5_analises.cypher).

### Marco 6 — Benchmark GraphQL × REST

Com a API rodando em `http://localhost:8001` (recomendado elevar `API_RATE_LIMIT_REQUISICOES` durante o teste de carga):

```powershell
.\.venv\Scripts\camara.exe medir-m6 --url http://localhost:8001 --repeticoes 30 --concorrencia 5 --salvar-json medicoes/m6_benchmark.json --salvar-csv medicoes/m6_benchmark.csv
```

### Carga de Cota Parlamentar (CEAP), Rede de Fornecedores e Indicadores (`carregar-gastos`)

Baixa os arquivos anuais em lote `Ano-{ano}.csv.zip` da Câmara (`2023`–`2026`), cria os nós `Fornecedor` (CNPJs) e relações `(Deputado)-[:PAGOU]->(Fornecedor)`, marca quem está em exercício hoje (`d.emExercicio`), reconstrói os intervalos exatos de exercício via `/historico` (`d.votacoesAtivo`, `d.diasEmExercicio`, `d.teveLicencaCargo`) e calcula os indicadores objetivos (`d.hhiFornecedores`, `d.gastoVsMedianaUf`, `d.projetosVotados`, `d.projetosAprovados`):

```powershell
.\.venv\Scripts\camara.exe carregar-gastos
```

Outras opções úteis:
- `coletar-m1` / `coletar-m2` — baixa apenas para `cache/` sem gravar no Neo4j (`--forcar` ignora o cache existente).
- `--anos 2023 2024 2025 2026` e `--tipos PL,PLP,PEC,PDL,PRC` (ou `--tipos TODOS`) no M2.
- `--salvar-medicao medicoes/arquivo.json` — salva o resumo JSON com contagens e tempo de carga.

---

## Como explorar e simular consultas no Neo4j

Abra o **Neo4j Browser** em http://localhost:7474, faça login com o usuário/senha do `.env` e execute qualquer consulta de [`cypher/m1_inspecao.cypher`](cypher/m1_inspecao.cypher), [`cypher/m2_inspecao.cypher`](cypher/m2_inspecao.cypher) ou [`cypher/m3_inspecao.cypher`](cypher/m3_inspecao.cypher).

---

## Como subir e testar a API GraphQL (M4)

Com o Neo4j rodando e populado, suba a API localmente via Uvicorn (ou junto com o Neo4j via `docker compose up -d --build`):

```powershell
.\.venv\Scripts\uvicorn.exe camara.api.app:app --reload --port 8001
```

- **Interface Visual HTMX (Explorador & Ficha Completa):** http://localhost:8001/ui (ou http://localhost:8001)
- **Saúde:** http://localhost:8001/health
- **GraphiQL (interface interativa):** http://localhost:8001/graphql

*(A porta no Docker Compose é configurável via `API_PORT` no `.env`, padrão `8001`.)*

### Recursos da Interface Visual (M7)

Interface editorial própria em **tipografia suíça** (`Inter` + `IBM Plex Mono` com números tabulares), **tema Claro e Escuro**, responsiva para desktop/celular e com barra global de carregamento:

- **Explorador de Deputados (`/ui`):**
  - Filtros combináveis por nome, situação (*Apenas em exercício* por padrão, *Todos*, *Fora de exercício*), UF (com bandeira), partido (com logo oficial), grupo de votação (descoberto automaticamente via GDS Louvain/Leiden) e 16 ordenações (gasto CEAP, viagens, divulgação, aeronaves, consultorias, concentração HHI, gasto acima da mediana da UF, custo por voto, presença, votos contra o partido, equipe, trocas de partido e projetos aprovados).
  - Persistência completa dos filtros, ordenação, aba ativa e paginação na URL (`/ui?...`) e no botão *Voltar*.
  - **Selos objetivos em SVG** (sem estrelas subjetivas) e **Modal de Metodologia** acessível no cabeçalho.
- **Ficha completa do deputado (`/ui/deputado/{id}`)** com 6 abas carregadas sob demanda via HTMX:
  1. **Alianças & Votos:** principais coautores, quem vota mais parecido, mesmos assuntos legislativos e taxa de votos contra a orientação do próprio partido.
  2. **Dinheiro (Cota Parlamentar):** gráficos interativos em **Chart.js** (gasto mensal `JAN`–`DEZ` e divisão por categoria com legenda completa), filtros de Ano + Mês, top fornecedores, **Trechos Aéreos traduzidos** (ex.: `MCP/BSB` → *Macapá (AP) → Brasília (DF)*), **Passageiros nas passagens** e tabela de notas fiscais com link para o PDF oficial.
  3. **Funcionários:** pessoal de gabinete por ano (`pessoal-gabinete?ano=...`), contagem de ativos vs. exonerados (rotatividade), sinalização factual de mesmo sobrenome não-comum, link para o portal oficial e acesso à **página dedicada de Evolução Salarial (`/ui/funcionario/{codigo}`)** com gráfico empilhado mês a mês (2023–2026) de salário do cargo, auxílios/diárias e 13º/férias.
  4. **Projetos:** subfiltros por resultado de mérito em plenário (*Aprovados*, *Rejeitados*, *Qualquer votação nominal*, *Sem votação*), tipo (`PL`, `PLP`, `PEC`, `PDL`, `PRC`), ordenação, distinção explícita entre votação de **Mérito** vs. **Req./Destaque Procedimental**, descrição oficial da votação e consulta sob demanda da situação atual.
  5. **Comissões & Discursos:** órgãos, frentes parlamentares e **leitor modal de discurso completo** com transcrição limpa.
  6. **Carreira & Histórico:** profissões, ocupações anteriores, mandatos externos e histórico cronológico de filiações e licenças.
- **Painéis extras na página inicial:** **Buscar Projetos por Assunto** (índice fulltext), **Conexão entre Deputados** (menor caminho por coautoria + concordância em votações) e **Empresas & CNPJs** (rede de fornecedores compartilhados com opção *Ocultar fornecedores comuns* para revelar agências, consultorias e gráficas).

---

## Metodologia dos Indicadores e Critérios Científicos

Seguindo boas práticas de Ciência Política e auditoria de dados públicos, a plataforma **não atribui estrelas nem nota única subjetiva** (que dependem de pesos arbitrários). Todos os selos e indicadores são matemáticos, reproduzíveis e auditáveis:

1. **Concentração de Fornecedores — Índice HHI (*Herfindahl-Hirschman Index*):**
   - Fórmula: $HHI = \sum_{i} \left(\frac{v_i}{\text{Total}}\right)^2 \times 10000$ (escala de `0` a `10.000`), usando o limiar clássico de alta concentração (**`≥ 2.500`**, adotado por órgãos antitruste como CADE e DoJ).
   - **Regra de materialidade estatística:** calculado apenas para deputados com **`≥ 365 dias em exercício`** **e** **`≥ R$ 300.000` gastos** na Cota Parlamentar, evitando distorção por amostra pequena em suplentes recém-empossados ou parlamentares que quase não utilizam a cota.
2. **Gasto Relativo à Própria UF (*Peer Benchmarking*):**
   - Como o teto da CEAP varia conforme o estado (devido ao custo das passagens aéreas), calculamos o **gasto diário em exercício** (`gastoTotalCeap / diasEmExercicio`) de cada parlamentar com **`≥ 365 dias ativos`** e comparamos com a **mediana da sua própria UF**.
   - `≥ +15%` acima da mediana da UF aciona o selo **`+X% UF`**; `≤ −35%` abaixo aciona **`Economia UF`**.
3. **Presença Efetiva em Plenário (ajustada por intervalos reais de mandato):**
   - Reconstruímos os intervalos exatos de `Entrada` (posse/reassunção) e `Saída` (licença/afastamento/renúncia) de cada deputado a partir de `/deputados/{id}/historico`, descontando integralmente períodos fora como Ministro ou Secretário de Estado e posses tardias de suplentes.
   - `< 50%` (com `≥ 50` votações no período ativo) aciona **`Baixa presença`**; `≥ 85%` aciona **`Alta presença`**.
4. **Efetividade Legislativa, Autor Principal ("Carona") e Projetos Simbólicos (Volden & Wiseman, 2014):**
   - Separamos votações nominais de **Mérito** das votações **Procedimentais** (requerimentos de retirada de pauta, destaques, emendas e adiamentos — ~96% das rejeições nominais ligadas via `SOBRE`), exibindo sempre a descrição oficial da votação.
   - Separamos **autor principal** (`ordemAssinatura = 1`) de **coautorias ("efeito carona")** (`ordem > 1`) e contabilizamos **projetos simbólicos/homenagens** (*Dia Nacional*, denominações de vias, títulos e Livro dos Heróis).
5. **Anomalias na Cota Parlamentar (sobre os 802.861 lançamentos de 2023–2026):**
   - **Pico mensal de combustível:** maior gasto em 1 mês convertido em tanques equivalentes de 50L (referência R$ 6,00/L = R$ 300/tanque).
   - **Notas repetidas no mesmo dia/CNPJ (*salami slicing*):** ocorrências de `≥ 2` pagamentos positivos no mesmo dia para o mesmo fornecedor (excluindo passagens aéreas, telefonia, correios e táxi).
   - **Sazonalidade pré-eleitoral:** razão entre a média mensal de *Divulgação da Atividade Parlamentar* em anos eleitorais (`2024`/`2026`) e anos não eleitorais (`2023`/`2025`).
   - **Cruzamento Passageiro × Equipe do Gabinete:** cada bilhete aéreo é cruzado com o próprio parlamentar e com `pessoal-gabinete` **do mesmo ano** da passagem, com alerta multi-ano clicável.
   - **Enriquecimento Receita Federal (CNPJs / QSA via BrasilAPI):** consulta sob demanda (com cache em disco) da data de abertura, situação cadastral, capital social, CNAE principal e **quadro societário** de qualquer fornecedor CNPJ, sinalizando empresas abertas na 57ª legislatura, situação irregular ou coincidência de sobrenome com o deputado pagador.
6. **Tratamento documentado de inconsistências conhecidas na API oficial:**
   - A API REST v2 `/deputados/{id}/despesas` ocasionalmente devolve `valorLiquido` com sinal negativo quando `valorDocumento > 0` e `valorGlosa = 0` (enquanto o CSV em lote e o portal oficial trazem positivo). A função `_valor_liquido_despesa()` normaliza esses casos preservando estornos reais (`valorDocumento <= 0`).
   - Os trechos aéreos (`txtTrecho`) e passageiros (`txtPassageiro`) vêm dos arquivos em lote `Ano-{ano}.csv.zip` (mais de 153 mil passagens detalhadas em 2023–2025) com tradução automática de códigos IATA para Cidade/UF/País.

Exemplos prontos para colar e executar no GraphiQL (http://localhost:8001/graphql):

**1. Deputados de SP com partidos, coautores e similaridade de votos (testa DataLoader + paginação):**
```graphql
query DeputadosSP {
  deputados(uf: "SP", primeiro: 5) {
    edges {
      cursor
      node {
        id
        nome
        partidos { sigla }
        coautores(primeiro: 3) {
          proposicoesEmComum
          deputado { nome uf partidos { sigla } }
        }
        votaComo(primeiro: 3, minimoVotacoes: 100) {
          concordancia
          votacoesEmComum
          deputado { nome uf partidos { sigla } }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

**2. Perfil de um deputado específico (com proposições e votos contra a orientação do partido):**
```graphql
query PerfilDeputado {
  deputado(id: "204501") {
    id
    nome
    uf
    uri
    partidos { sigla nome }
    contraOrientacao {
      partido { sigla }
      contra
      totalOrientadas
      taxaDivergencia
    }
    proposicoes(primeiro: 5) {
      edges {
        node {
          siglaTipo
          numero
          ano
          ementa
          temas { nome }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
```

**3. Busca fulltext em proposições (usando o índice `proposicao_ementa_ft`):**
```graphql
query BuscaProposicoesIA {
  proposicoes(texto: "inteligência artificial", primeiro: 5) {
    edges {
      node {
        id
        siglaTipo
        numero
        ano
        ementa
        temas { nome }
        autores(primeiro: 3) { nome uf partidos { sigla } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

**4. Menor caminho por coautoria entre dois deputados (`shortestPath`):**
```graphql
query MenorCaminhoCoautoria {
  caminho(deputadoA: "220593", deputadoB: "204501") {
    id
    nome
    uf
    partidos { sigla }
  }
}
```

**5. Detalhes de uma votação nominal com proposições votadas e orientações partidárias:**
```graphql
query DetalheVotacao {
  votacao(id: "1197773-90") {
    id
    data
    siglaOrgao
    descricao
    aprovacao
    votosSim
    votosNao
    proposicoes { siglaTipo numero ano ementa }
    orientacoes {
      partido { sigla }
      orientacao
      siglaBancada
    }
  }
}
```

**6. Testar as proteções da API (deve retornar erro `MAX_DEPTH_EXCEEDED`):**
```graphql
query TesteLimiteProfundidade {
  deputado(id: "204501") {
    coautores(primeiro: 1) {
      deputado {
        coautores(primeiro: 1) {
          deputado {
            coautores(primeiro: 1) {
              deputado {
                partidos { sigla }
              }
            }
          }
        }
      }
    }
  }
}
```

### Exemplos de consultas Cypher no Neo4j Browser

**Contagem de nós por rótulo:**
```cypher
MATCH (n)
UNWIND labels(n) AS rotulo
RETURN rotulo, count(*) AS total
ORDER BY rotulo;
```

**Coautoria entre partidos diferentes (sem nenhum partido em comum na 57ª legislatura):**
```cypher
MATCH (a:Deputado)-[c:COAUTOR_COM]->(b:Deputado)
WHERE NOT EXISTS {
  MATCH (a)-[:FILIADO_A]->(p:Partido)<-[:FILIADO_A]-(b)
}
RETURN a.nome AS deputado_a, a.uf AS uf_a,
       b.nome AS deputado_b, b.uf AS uf_b,
       c.n AS proposicoes_em_comum
ORDER BY c.n DESC
LIMIT 15;
```

**Busca em texto completo na ementa de proposições:**
```cypher
CALL db.index.fulltext.queryNodes('proposicao_ementa_ft', 'inteligência artificial')
YIELD node, score
RETURN node.siglaTipo + ' ' + toString(node.numero) + '/' + toString(node.ano) AS proposicao,
       left(node.ementa, 120) AS ementa,
       round(score, 2) AS score
LIMIT 10;
```

**Confirmar que o plugin Graph Data Science (GDS) está ativo:**
```cypher
RETURN gds.version();
```

---

## Como rodar os testes (sem acesso à rede)

Os testes do cliente da API usam respostas gravadas com `respx` (nenhuma chamada real à Câmara é feita) e os testes da carga validam normalização, criação prévia de constraints e escrita em lotes com `UNWIND`/`MERGE`:

```powershell
.\.venv\Scripts\pytest.exe -v
```

Verificação de estilo/lint com `ruff`:

```powershell
.\.venv\Scripts\ruff.exe check src tests
```

---

## Estrutura do repositório

```text
camara-em-grafo/
├── docker-compose.yml      # Neo4j 5.26 Community + plugin GDS + API
├── Dockerfile              # Imagem da API FastAPI + Strawberry + HTMX
├── pyproject.toml          # Dependências e configuração do pacote/pytest/ruff
├── .env.example            # Modelo de variáveis de ambiente
├── src/camara/
│   ├── config.py           # Configurações via .env
│   ├── cli.py              # CLI (coleta, carga, recarga offline, GDS e benchmark)
│   ├── coleta/             # Cliente assíncrono httpx, arquivos em lote CSV e cache
│   ├── carga/              # Constraints, índice fulltext e carga idempotente no Neo4j
│   ├── analise/            # Relações derivadas e algoritmos Graph Data Science
│   ├── medicao/            # Benchmark reproduzível GraphQL x REST
│   └── api/                # FastAPI + Strawberry GraphQL + Interface HTMX
├── cypher/                 # Consultas Cypher comentadas
├── medicoes/               # Resultados JSON/CSV de medições de carga e benchmark
└── tests/                  # Testes automatizados com pytest
```

---

## Como atualizar nos próximos anos (ex.: 2027 / 58ª Legislatura)

Você **não precisa alterar o código Python nem os templates** para virar o ano ou iniciar uma nova legislatura — tudo é dirigido pelas variáveis do `.env`:

1. **Para incluir apenas um novo ano na mesma legislatura** (ex.: adicionar `2027` na 57ª):
   - Edite `.env` e adicione o ano em `CAMARA_M2_ANOS=2023,2024,2025,2026,2027`.
   - Rode os comandos de carga e análise novamente (o seletor de anos da interface HTMX e da Cota Parlamentar CEAP se ajusta automaticamente):
     ```powershell
     .\.venv\Scripts\camara.exe carregar-m1 --forcar-coleta
     .\.venv\Scripts\camara.exe carregar-m2
     .\.venv\Scripts\camara.exe carregar-m3
     .\.venv\Scripts\camara.exe carregar-gastos
     .\.venv\Scripts\camara.exe analisar-m5
     ```
2. **Para virar para a 58ª Legislatura (a partir de fev/2027):**
   - Atualize no `.env`:
     ```env
     CAMARA_LEGISLATURA_ID=58
     CAMARA_M2_ANOS=2027
     ```
   - Apague `cache/arquivos/funcionarios/funcionarios.csv` (para baixar a lista atualizada de secretários parlamentares) e rode:
     ```powershell
     .\.venv\Scripts\camara.exe carregar-m1 --limpar
     .\.venv\Scripts\camara.exe carregar-m2
     .\.venv\Scripts\camara.exe carregar-m3
     .\.venv\Scripts\camara.exe carregar-gastos
     .\.venv\Scripts\camara.exe analisar-m5
     ```
   - Reinicie o contêiner da API para recarregar as variáveis do `.env`:
     ```powershell
     docker compose up -d api
     ```

---

## Status dos marcos

- [x] **M1 — Coleta base:** deputados, partidos, UFs e 57ª legislatura no Neo4j + recarga offline do cache
- [x] **M2 — Proposições e autoria:** proposições, autores, temas, índice fulltext e relação derivada `COAUTOR_COM`
- [x] **M3 — Votações:** votações nominais, votos, orientações (com expansão de federações), `VOTA_COMO` e `CONTRA_ORIENTACAO`
- [x] **M4 — API GraphQL:** FastAPI + Strawberry com DataLoader, paginação por cursor, limites de profundidade/custo/taxa e testes
- [x] **M5 — Análises:** comunidades (Louvain/Leiden), centralidade (PageRank/Betweenness) e similaridade por temas com GDS
- [x] **M6 — Medição:** benchmarks da API GraphQL × rotas REST equivalentes sobre Neo4j (CSV/JSON)
- [x] **M7 — Interface Visual (HTMX):** explorador com filtros, fotos, bandeiras, logos de partidos e ficha completa (grafo, CEAP, funcionários, comissões, frentes, discursos e histórico)
