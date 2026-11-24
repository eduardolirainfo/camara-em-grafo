"""Respostas gravadas da API v2 da Câmara para testes sem acesso à rede."""

LEGISLATURA_57 = {
    "dados": {
        "id": 57,
        "uri": "https://dadosabertos.camara.leg.br/api/v2/legislaturas/57",
        "dataInicio": "2023-02-01",
        "dataFim": "2027-01-31",
    },
    "links": [
        {
            "rel": "self",
            "href": "https://dadosabertos.camara.leg.br/api/v2/legislaturas/57",
        }
    ],
}

PARTIDOS_PAGINA_1 = {
    "dados": [
        {
            "id": 36898,
            "sigla": "AVANTE",
            "nome": "Avante",
            "uri": "https://dadosabertos.camara.leg.br/api/v2/partidos/36898",
        },
        {
            "id": 37906,
            "sigla": "PL",
            "nome": "Partido Liberal",
            "uri": "https://dadosabertos.camara.leg.br/api/v2/partidos/37906",
        },
    ],
    "links": [
        {
            "rel": "self",
            "href": "https://dadosabertos.camara.leg.br/api/v2/partidos?idLegislatura=57&itens=2&pagina=1",
        },
        {
            "rel": "first",
            "href": "https://dadosabertos.camara.leg.br/api/v2/partidos?idLegislatura=57&pagina=1&itens=2",
        },
        {
            "rel": "last",
            "href": "https://dadosabertos.camara.leg.br/api/v2/partidos?idLegislatura=57&pagina=1&itens=2",
        },
    ],
}

DEPUTADOS_PAGINA_1 = {
    "dados": [
        {
            "id": 220593,
            "uri": "https://dadosabertos.camara.leg.br/api/v2/deputados/220593",
            "nome": "Abilio Brunini",
            "siglaPartido": "PL",
            "uriPartido": "https://dadosabertos.camara.leg.br/api/v2/partidos/37906",
            "siglaUf": "MT",
            "idLegislatura": 57,
            "urlFoto": "https://www.camara.leg.br/internet/deputado/bandep/220593.jpg",
            "email": None,
        },
        {
            "id": 100001,
            "uri": "https://dadosabertos.camara.leg.br/api/v2/deputados/100001",
            "nome": "Deputada Exemplo",
            "siglaPartido": "AVANTE",
            "uriPartido": "https://dadosabertos.camara.leg.br/api/v2/partidos/36898",
            "siglaUf": "MG",
            "idLegislatura": 57,
            "urlFoto": "https://www.camara.leg.br/internet/deputado/bandep/100001.jpg",
            "email": "dep.exemplo@camara.leg.br",
        },
    ],
    "links": [
        {
            "rel": "self",
            "href": "https://dadosabertos.camara.leg.br/api/v2/deputados?idLegislatura=57&itens=2&pagina=1",
        },
        {
            "rel": "next",
            "href": "https://dadosabertos.camara.leg.br/api/v2/deputados?idLegislatura=57&pagina=2&itens=2",
        },
        {
            "rel": "last",
            "href": "https://dadosabertos.camara.leg.br/api/v2/deputados?idLegislatura=57&pagina=2&itens=2",
        },
    ],
}

DEPUTADOS_PAGINA_2 = {
    "dados": [
        {
            "id": 100001,
            "uri": "https://dadosabertos.camara.leg.br/api/v2/deputados/100001",
            "nome": "Deputada Exemplo",
            "siglaPartido": "PL",
            "uriPartido": "https://dadosabertos.camara.leg.br/api/v2/partidos/37906",
            "siglaUf": "MG",
            "idLegislatura": 57,
            "urlFoto": "https://www.camara.leg.br/internet/deputado/bandep/100001.jpg",
            "email": "dep.exemplo@camara.leg.br",
        },
        {
            "id": 100002,
            "uri": "https://dadosabertos.camara.leg.br/api/v2/deputados/100002",
            "nome": "Deputado Sem Partido",
            "siglaPartido": None,
            "uriPartido": None,
            "siglaUf": "SP",
            "idLegislatura": 57,
            "urlFoto": "https://www.camara.leg.br/internet/deputado/bandep/100002.jpg",
            "email": None,
        },
    ],
    "links": [
        {
            "rel": "self",
            "href": "https://dadosabertos.camara.leg.br/api/v2/deputados?idLegislatura=57&pagina=2&itens=2",
        },
        {
            "rel": "previous",
            "href": "https://dadosabertos.camara.leg.br/api/v2/deputados?idLegislatura=57&pagina=1&itens=2",
        },
        {
            "rel": "last",
            "href": "https://dadosabertos.camara.leg.br/api/v2/deputados?idLegislatura=57&pagina=2&itens=2",
        },
    ],
}
