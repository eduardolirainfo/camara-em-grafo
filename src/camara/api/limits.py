"""Proteções da API GraphQL: limite de profundidade, custo, introspecção e rate limit."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Request, Response
from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    SelectionSetNode,
    ValidationRule,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from strawberry.extensions import AddValidationRules


class DomainValidationError(Exception):
    """Erro seguro de validação/domínio exibido diretamente ao cliente GraphQL."""


def criar_regra_profundidade(max_profundidade: int) -> type[ValidationRule]:
    """Cria uma ValidationRule do graphql-core que rejeita consultas acima da profundidade máxima."""

    class LimiteProfundidadeRule(ValidationRule):
        def enter_operation_definition(
            self, node: OperationDefinitionNode, *_args: Any
        ) -> None:
            fragments = {
                defn.name.value: defn
                for defn in self.context.document.definitions
                if isinstance(defn, FragmentDefinitionNode)
            }
            profundidade = _medir_profundidade(node.selection_set, fragments, atual=0)
            if profundidade > max_profundidade:
                self.report_error(
                    GraphQLError(
                        f"Consulta excede a profundidade máxima permitida ({max_profundidade}).",
                        nodes=[node],
                        extensions={"code": "MAX_DEPTH_EXCEEDED", "maxDepth": max_profundidade},
                    )
                )

    return LimiteProfundidadeRule


def _medir_profundidade(
    selection_set: SelectionSetNode | None,
    fragments: dict[str, FragmentDefinitionNode],
    atual: int,
    visitados: set[str] | None = None,
) -> int:
    if selection_set is None:
        return atual
    vistos = visitados or set()
    maior = atual
    for sel in selection_set.selections:
        if isinstance(sel, FieldNode):
            if sel.name.value.startswith("__"):
                continue
            prox = (
                _medir_profundidade(sel.selection_set, fragments, atual + 1, vistos)
                if sel.selection_set
                else atual + 1
            )
            maior = max(maior, prox)
        elif isinstance(sel, InlineFragmentNode):
            maior = max(maior, _medir_profundidade(sel.selection_set, fragments, atual, vistos))
        elif isinstance(sel, FragmentSpreadNode):
            nome = sel.name.value
            if nome in fragments and nome not in vistos:
                maior = max(
                    maior,
                    _medir_profundidade(
                        fragments[nome].selection_set, fragments, atual, vistos | {nome}
                    ),
                )
    return maior


PESOS_CAMPOS: dict[str, int] = {
    "deputados": 2,
    "proposicoes": 2,
    "coautores": 3,
    "votaComo": 3,
    "caminho": 10,
    "contraOrientacao": 2,
    "orientacoes": 2,
    "autores": 2,
    "temas": 1,
    "partidos": 1,
}

CAMPOS_LISTA: set[str] = {
    "deputados",
    "proposicoes",
    "coautores",
    "votaComo",
    "autores",
}


def criar_regra_custo(max_custo: int, default_lista: int = 10) -> type[ValidationRule]:
    """Cria uma ValidationRule que estima o custo da consulta e bloqueia consultas caras."""

    class LimiteCustoRule(ValidationRule):
        def enter_operation_definition(
            self, node: OperationDefinitionNode, *_args: Any
        ) -> None:
            fragments = {
                defn.name.value: defn
                for defn in self.context.document.definitions
                if isinstance(defn, FragmentDefinitionNode)
            }
            custo = _calcular_custo(node.selection_set, fragments, default_lista)
            if custo > max_custo:
                self.report_error(
                    GraphQLError(
                        f"Consulta excede o custo máximo permitido ({custo} > {max_custo}). "
                        "Reduza 'primeiro' ou o aninhamento.",
                        nodes=[node],
                        extensions={
                            "code": "MAX_COST_EXCEEDED",
                            "cost": custo,
                            "maxCost": max_custo,
                        },
                    )
                )

    return LimiteCustoRule


def _extrair_primeiro(field: FieldNode, default: int) -> int:
    for arg in field.arguments or ():
        if arg.name.value == "primeiro" and isinstance(arg.value, IntValueNode):
            return max(1, int(arg.value.value))
    return default


def _calcular_custo(
    selection_set: SelectionSetNode | None,
    fragments: dict[str, FragmentDefinitionNode],
    default_lista: int,
    visitados: set[str] | None = None,
) -> int:
    if selection_set is None:
        return 0
    vistos = visitados or set()
    total = 0
    for sel in selection_set.selections:
        if isinstance(sel, FieldNode):
            nome = sel.name.value
            if nome.startswith("__"):
                continue
            peso_base = PESOS_CAMPOS.get(nome, 1)
            custo_filhos = _calcular_custo(sel.selection_set, fragments, default_lista, vistos)
            if nome in CAMPOS_LISTA:
                fator = _extrair_primeiro(sel, default_lista)
                # Escala sublinear para não punir paginação normal, mas bloquear aninhamentos N x M
                total += peso_base + max(1, (fator + 4) // 5) * max(1, custo_filhos)
            else:
                total += peso_base + custo_filhos
        elif isinstance(sel, InlineFragmentNode):
            total += _calcular_custo(sel.selection_set, fragments, default_lista, vistos)
        elif isinstance(sel, FragmentSpreadNode):
            nome = sel.name.value
            if nome in fragments and nome not in vistos:
                total += _calcular_custo(
                    fragments[nome].selection_set, fragments, default_lista, vistos | {nome}
                )
    return total


def criar_regra_sem_introspeccao() -> type[ValidationRule]:
    """Bloqueia consultas de introspecção (__schema, __type) quando desabilitada em produção."""

    class SemIntrospeccaoRule(ValidationRule):
        def enter_field(self, node: FieldNode, *_args: Any) -> None:
            if node.name.value in {"__schema", "__type"}:
                self.report_error(
                    GraphQLError(
                        "Introspecção GraphQL desabilitada neste ambiente.",
                        nodes=[node],
                        extensions={"code": "INTROSPECTION_DISABLED"},
                    )
                )

    return SemIntrospeccaoRule


def extensoes_seguranca(
    max_profundidade: int,
    max_custo: int,
    introspeccao_habilitada: bool,
) -> list[Any]:
    regras: list[type[ValidationRule]] = [
        criar_regra_profundidade(max_profundidade),
        criar_regra_custo(max_custo),
    ]
    if not introspeccao_habilitada:
        regras.append(criar_regra_sem_introspeccao())
    return [lambda: AddValidationRules(regras)]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Limite de taxa por IP (janela deslizante em memória) para proteger a API."""

    def __init__(
        self,
        app: Any,
        max_requisicoes: int = 60,
        janela_segundos: int = 60,
        rotas_protegidas: tuple[str, ...] = ("/graphql",),
    ) -> None:
        super().__init__(app)
        self.max_requisicoes = max_requisicoes
        self.janela_segundos = janela_segundos
        self.rotas_protegidas = rotas_protegidas
        self._janelas: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not any(request.url.path.startswith(r) for r in self.rotas_protegidas):
            return await call_next(request)

        cliente = request.client.host if request.client else "desconhecido"
        agora = time.monotonic()
        limite_tempo = agora - self.janela_segundos
        fila = self._janelas[cliente]
        while fila and fila[0] <= limite_tempo:
            fila.popleft()

        if len(fila) >= self.max_requisicoes:
            retry_after = max(1, int(self.janela_segundos - (agora - fila[0])))
            return JSONResponse(
                status_code=429,
                content={
                    "errors": [
                        {
                            "message": "Limite de requisições excedido. Tente novamente em instantes.",
                            "extensions": {"code": "RATE_LIMIT_EXCEEDED"},
                        }
                    ]
                },
                headers={"Retry-After": str(retry_after)},
            )

        fila.append(agora)
        return await call_next(request)
