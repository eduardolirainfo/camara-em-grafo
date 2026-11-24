"""Esquema GraphQL (Strawberry) do projeto Câmara em Grafo."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import strawberry
from graphql import GraphQLError
from strawberry.fastapi import BaseContext
from strawberry.types import ExecutionContext, Info

from camara.api.dataloaders import GraphDataLoaders
from camara.api.limits import DomainValidationError, extensoes_seguranca
from camara.api.neo4j_repo import Neo4jGraphRepository

logger = logging.getLogger(__name__)


@dataclass
class GraphQLContext(BaseContext):
    repo: Neo4jGraphRepository
    loaders: GraphDataLoaders
    max_itens_pagina: int = 50


def codificar_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode("ascii")


def decodificar_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        bruto = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        prefixo, valor = bruto.split(":", 1)
        if prefixo != "offset":
            raise ValueError("prefixo inválido")
        offset = int(valor)
        if offset < 0:
            raise ValueError("offset negativo")
        return offset
    except Exception as exc:
        raise DomainValidationError("Cursor 'depois' inválido.") from exc


def validar_primeiro(primeiro: int, maximo: int) -> int:
    if primeiro < 1 or primeiro > maximo:
        raise DomainValidationError(
            f"O parâmetro 'primeiro' deve estar entre 1 e {maximo}."
        )
    return primeiro


def _parse_int_id(valor: strawberry.ID, nome: str) -> int:
    try:
        return int(str(valor))
    except ValueError as exc:
        raise DomainValidationError(f"Identificador '{nome}' inválido.") from exc


@strawberry.type
class PageInfo:
    has_next_page: bool
    end_cursor: str | None


@strawberry.type
class Partido:
    sigla: str
    nome: str | None = None
    id: int | None = None
    uri: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Partido:
        return cls(
            sigla=data["sigla"],
            nome=data.get("nome"),
            id=data.get("id"),
            uri=data.get("uri"),
        )


@strawberry.type
class Tema:
    cod: int
    nome: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tema:
        return cls(cod=int(data["cod"]), nome=str(data["nome"]))


@strawberry.type
class Coautoria:
    deputado: Deputado
    proposicoes_em_comum: int


@strawberry.type
class Similaridade:
    deputado: Deputado
    concordancia: float
    votacoes_em_comum: int


@strawberry.type
class DivergenciaPartidaria:
    partido: Partido
    contra: int
    total_orientadas: int
    taxa_divergencia: float


@strawberry.type
class Orientacao:
    partido: Partido
    orientacao: str
    sigla_bancada: str | None = None


@strawberry.type
class Proposicao:
    id: strawberry.ID
    sigla_tipo: str
    numero: int | None
    ano: int | None
    ementa: str
    data_apresentacao: str | None
    uri: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Proposicao:
        return cls(
            id=strawberry.ID(str(data["id"])),
            sigla_tipo=str(data.get("siglaTipo") or ""),
            numero=data.get("numero"),
            ano=data.get("ano"),
            ementa=str(data.get("ementa") or ""),
            data_apresentacao=data.get("dataApresentacao"),
            uri=data.get("uri"),
        )

    @strawberry.field
    async def temas(self, info: Info[GraphQLContext, None]) -> list[Tema]:
        rows = await info.context.loaders.temas_por_proposicao.load(int(str(self.id)))
        return [Tema.from_dict(r) for r in rows]

    @strawberry.field
    async def autores(
        self,
        info: Info[GraphQLContext, None],
        primeiro: int = 10,
    ) -> list[Deputado]:
        limite = validar_primeiro(primeiro, info.context.max_itens_pagina)
        rows = await info.context.loaders.autores_por_proposicao.load(
            (int(str(self.id)), limite)
        )
        return [Deputado.from_dict(r) for r in rows]


@strawberry.type
class ProposicaoEdge:
    cursor: str
    node: Proposicao


@strawberry.type
class ProposicaoConexao:
    edges: list[ProposicaoEdge]
    page_info: PageInfo


@strawberry.type
class Deputado:
    id: strawberry.ID
    nome: str
    uf: str | None
    url_foto: str | None
    uri: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Deputado:
        return cls(
            id=strawberry.ID(str(data["id"])),
            nome=str(data["nome"]),
            uf=data.get("uf"),
            url_foto=data.get("urlFoto"),
            uri=data.get("uri"),
        )

    @strawberry.field
    async def partidos(self, info: Info[GraphQLContext, None]) -> list[Partido]:
        rows = await info.context.loaders.partidos_por_deputado.load(int(str(self.id)))
        return [Partido.from_dict(r) for r in rows]

    @strawberry.field
    async def partido(self, info: Info[GraphQLContext, None]) -> Partido | None:
        rows = await info.context.loaders.partidos_por_deputado.load(int(str(self.id)))
        return Partido.from_dict(rows[0]) if rows else None

    @strawberry.field
    async def proposicoes(
        self,
        info: Info[GraphQLContext, None],
        primeiro: int = 10,
        depois: str | None = None,
    ) -> ProposicaoConexao:
        limite = validar_primeiro(primeiro, info.context.max_itens_pagina)
        offset = decodificar_cursor(depois)
        rows = await info.context.repo.listar_proposicoes_de_deputado(
            dep_id=int(str(self.id)),
            offset=offset,
            limit=limite + 1,
        )
        return _montar_proposicao_conexao(rows, offset, limite)

    @strawberry.field
    async def coautores(
        self,
        info: Info[GraphQLContext, None],
        primeiro: int = 10,
    ) -> list[Coautoria]:
        limite = validar_primeiro(primeiro, info.context.max_itens_pagina)
        rows = await info.context.loaders.coautores_por_deputado.load(
            (int(str(self.id)), limite)
        )
        return [
            Coautoria(
                deputado=Deputado.from_dict(r["deputado"]),
                proposicoes_em_comum=int(r["proposicoesEmComum"]),
            )
            for r in rows
        ]

    @strawberry.field
    async def vota_como(
        self,
        info: Info[GraphQLContext, None],
        primeiro: int = 10,
        minimo_votacoes: int = 20,
    ) -> list[Similaridade]:
        limite = validar_primeiro(primeiro, info.context.max_itens_pagina)
        if minimo_votacoes < 1:
            raise DomainValidationError("'minimoVotacoes' deve ser maior ou igual a 1.")
        rows = await info.context.loaders.vota_como_por_deputado.load(
            (int(str(self.id)), limite, minimo_votacoes)
        )
        return [
            Similaridade(
                deputado=Deputado.from_dict(r["deputado"]),
                concordancia=float(r["concordancia"]),
                votacoes_em_comum=int(r["votacoesEmComum"]),
            )
            for r in rows
        ]

    @strawberry.field
    async def contra_orientacao(
        self,
        info: Info[GraphQLContext, None],
    ) -> list[DivergenciaPartidaria]:
        rows = await info.context.loaders.contra_orientacao_por_deputado.load(
            int(str(self.id))
        )
        return [
            DivergenciaPartidaria(
                partido=Partido.from_dict(r["partido"]),
                contra=int(r["contra"]),
                total_orientadas=int(r["totalOrientadas"]),
                taxa_divergencia=float(r["taxaDivergencia"]),
            )
            for r in rows
        ]


@strawberry.type
class DeputadoEdge:
    cursor: str
    node: Deputado


@strawberry.type
class DeputadoConexao:
    edges: list[DeputadoEdge]
    page_info: PageInfo


@strawberry.type
class Votacao:
    id: strawberry.ID
    data: str | None
    ano: int | None
    sigla_orgao: str | None
    descricao: str
    aprovacao: bool | None
    votos_sim: int
    votos_nao: int
    votos_outros: int
    uri: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Votacao:
        return cls(
            id=strawberry.ID(str(data["id"])),
            data=data.get("data"),
            ano=data.get("ano"),
            sigla_orgao=data.get("siglaOrgao"),
            descricao=str(data.get("descricao") or ""),
            aprovacao=data.get("aprovacao"),
            votos_sim=int(data.get("votosSim") or 0),
            votos_nao=int(data.get("votosNao") or 0),
            votos_outros=int(data.get("votosOutros") or 0),
            uri=data.get("uri"),
        )

    @strawberry.field
    async def proposicoes(self, info: Info[GraphQLContext, None]) -> list[Proposicao]:
        rows = await info.context.loaders.proposicoes_por_votacao.load(str(self.id))
        return [Proposicao.from_dict(r) for r in rows]

    @strawberry.field
    async def orientacoes(self, info: Info[GraphQLContext, None]) -> list[Orientacao]:
        rows = await info.context.loaders.orientacoes_por_votacao.load(str(self.id))
        return [
            Orientacao(
                partido=Partido.from_dict(r["partido"]),
                orientacao=str(r["orientacao"]),
                sigla_bancada=r.get("siglaBancada"),
            )
            for r in rows
        ]


def _montar_deputado_conexao(
    rows: list[dict[str, Any]], offset: int, limite: int
) -> DeputadoConexao:
    has_next = len(rows) > limite
    pagina = rows[:limite]
    edges = [
        DeputadoEdge(cursor=codificar_cursor(offset + idx + 1), node=Deputado.from_dict(r))
        for idx, r in enumerate(pagina)
    ]
    return DeputadoConexao(
        edges=edges,
        page_info=PageInfo(
            has_next_page=has_next,
            end_cursor=edges[-1].cursor if edges else None,
        ),
    )


def _montar_proposicao_conexao(
    rows: list[dict[str, Any]], offset: int, limite: int
) -> ProposicaoConexao:
    has_next = len(rows) > limite
    pagina = rows[:limite]
    edges = [
        ProposicaoEdge(
            cursor=codificar_cursor(offset + idx + 1), node=Proposicao.from_dict(r)
        )
        for idx, r in enumerate(pagina)
    ]
    return ProposicaoConexao(
        edges=edges,
        page_info=PageInfo(
            has_next_page=has_next,
            end_cursor=edges[-1].cursor if edges else None,
        ),
    )


@strawberry.type
class Query:
    @strawberry.field
    async def deputado(
        self, info: Info[GraphQLContext, None], id: strawberry.ID
    ) -> Deputado | None:
        dep_id = _parse_int_id(id, "id")
        row = await info.context.repo.buscar_deputado(dep_id)
        return Deputado.from_dict(row) if row else None

    @strawberry.field
    async def deputados(
        self,
        info: Info[GraphQLContext, None],
        uf: str | None = None,
        partido: str | None = None,
        primeiro: int = 20,
        depois: str | None = None,
    ) -> DeputadoConexao:
        limite = validar_primeiro(primeiro, info.context.max_itens_pagina)
        offset = decodificar_cursor(depois)
        rows = await info.context.repo.listar_deputados(
            uf=uf, partido=partido, offset=offset, limit=limite + 1
        )
        return _montar_deputado_conexao(rows, offset, limite)

    @strawberry.field
    async def proposicoes(
        self,
        info: Info[GraphQLContext, None],
        tema: str | None = None,
        texto: str | None = None,
        ano: int | None = None,
        tipo: str | None = None,
        primeiro: int = 20,
        depois: str | None = None,
    ) -> ProposicaoConexao:
        limite = validar_primeiro(primeiro, info.context.max_itens_pagina)
        offset = decodificar_cursor(depois)
        rows = await info.context.repo.listar_proposicoes(
            tema=tema, texto=texto, ano=ano, tipo=tipo, offset=offset, limit=limite + 1
        )
        return _montar_proposicao_conexao(rows, offset, limite)

    @strawberry.field
    async def votacao(
        self, info: Info[GraphQLContext, None], id: strawberry.ID
    ) -> Votacao | None:
        row = await info.context.repo.buscar_votacao(str(id))
        return Votacao.from_dict(row) if row else None

    @strawberry.field
    async def caminho(
        self,
        info: Info[GraphQLContext, None],
        deputado_a: strawberry.ID,
        deputado_b: strawberry.ID,
    ) -> list[Deputado]:
        id_a = _parse_int_id(deputado_a, "deputadoA")
        id_b = _parse_int_id(deputado_b, "deputadoB")
        rows = await info.context.repo.menor_caminho_coautoria(id_a, id_b)
        return [Deputado.from_dict(r) for r in rows]


class SafeSchema(strawberry.Schema):
    """Mascara erros internos para nunca vazar Cypher ou stack traces ao cliente."""

    def process_errors(
        self,
        errors: list[GraphQLError],
        execution_context: ExecutionContext | None = None,
    ) -> None:
        mascarados: list[GraphQLError] = []
        for err in errors:
            orig = err.original_error
            if orig is None or isinstance(orig, DomainValidationError):
                mascarados.append(err)
            else:
                logger.exception("Erro interno em consulta GraphQL", exc_info=orig)
                mascarados.append(
                    GraphQLError(
                        "Erro interno ao processar a consulta.",
                        nodes=err.nodes,
                        path=err.path,
                        extensions={"code": "INTERNAL_SERVER_ERROR"},
                    )
                )
        if execution_context is not None and execution_context.result is not None:
            execution_context.result.errors = mascarados


def criar_schema(
    max_profundidade: int = 6,
    max_custo: int = 200,
    introspeccao_habilitada: bool = True,
) -> SafeSchema:
    return SafeSchema(
        query=Query,
        extensions=extensoes_seguranca(
            max_profundidade=max_profundidade,
            max_custo=max_custo,
            introspeccao_habilitada=introspeccao_habilitada,
        ),
    )
