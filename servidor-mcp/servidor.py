"""Servidor MCP da Central de Salas (Streamable HTTP, sem sessao, porta 7301).

O ciclo de MRTR da reserva esta em `reservar_sala`:

1. conflito com alternativa: a tool termina a resposta com `input_required`,
   levando a elicitation e um `requestState` com o pedido e as alternativas;
2. retry: a tool le `inputResponses` e reconstroi o pedido a partir do
   `requestState`, sem consultar nada guardado em memoria.

O codigo escreve e le o `requestState` em texto claro. Quem sela na saida e
verifica na volta e o SDK (`RequestStateSecurity`), com a chave de
REQUEST_STATE_SECRET: adulterado, expirado ou reapresentado com outros
argumentos, o request e rejeitado com -32602 antes de chegar na tool.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import uvicorn
from mcp import MCPError
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestedSchema,
    ElicitRequestFormParams,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    MissingRequiredClientCapabilityErrorData,
)
from pydantic import BaseModel

from dominio import ERRO_SEM_ALTERNATIVA, CentralDeSalas, ErroDeNegocio

VALIDADE_DO_REQUEST_STATE = 15 * 60  # segundos; o enunciado pede entre 5 e 30 minutos
CHAVE_DA_PERGUNTA = "escolha_de_sala"
PERGUNTA = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


def chave_de_integridade() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes.\n"
            'Gere uma com: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    return segredo


central = CentralDeSalas()

mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[chave_de_integridade()], ttl=VALIDADE_DO_REQUEST_STATE),
)


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
async def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in central.salas])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        conflitos = central.conflitos(sala, central.validar(sala, inicio, fim))
    except ErroDeNegocio as e:
        raise ToolError(str(e)) from e
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[ConflitoOut(id=c["id"], inicio=c["inicio"], fim=c["fim"], responsavel=c["responsavel"]) for c in conflitos],
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
async def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    respostas = ctx.input_responses or {}
    try:
        if ctx.request_state is not None and CHAVE_DA_PERGUNTA in respostas:
            # Retry. O SDK ja verificou o selo; daqui em diante vale o que foi selado,
            # nao os argumentos que o cliente reenviou.
            return _retomar(json.loads(ctx.request_state), respostas[CHAVE_DA_PERGUNTA], ctx)
        pedido = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
        if central.conflitos(sala, central.validar(sala, inicio, fim)):
            return _perguntar(pedido, ctx)
        return _reservada(central.reservar(sala, inicio, fim, responsavel))
    except ErroDeNegocio as e:
        raise ToolError(str(e)) from e


def _perguntar(pedido: dict[str, str], ctx: Context) -> InputRequiredResult:
    """Termina a resposta pedindo a escolha. O servidor nao chama o cliente: devolve a pergunta."""
    alternativas = central.alternativas(pedido["sala"], central.validar(pedido["sala"], pedido["inicio"], pedido["fim"]))
    if not alternativas:
        raise ErroDeNegocio(ERRO_SEM_ALTERNATIVA)
    _exigir_elicitation_em_form(ctx)
    schema: ElicitRequestedSchema = {
        "type": "object",
        "properties": {
            "sala": {
                "type": "string",
                "title": "Sala",
                "description": "Sala alternativa escolhida",
                "enum": alternativas,
            }
        },
        "required": ["sala"],
    }
    # Tudo que o retry precisa viaja aqui. Nada fica em memoria esperando a resposta,
    # entao um retry depois de um restart do processo conclui do mesmo jeito.
    estado = {"pergunta": CHAVE_DA_PERGUNTA, "pedido": pedido, "alternativas": alternativas}
    return InputRequiredResult(
        input_requests={
            CHAVE_DA_PERGUNTA: ElicitRequest(params=ElicitRequestFormParams(message=PERGUNTA, requested_schema=schema))
        },
        request_state=json.dumps(estado, ensure_ascii=False),
    )


def _retomar(estado: dict[str, Any], resposta: Any, ctx: Context) -> ReservaOut | InputRequiredResult:
    pedido: dict[str, str] = estado["pedido"]
    if estado.get("pergunta") != CHAVE_DA_PERGUNTA or not isinstance(resposta, ElicitResult):
        raise ErroDeNegocio("Resposta que nao corresponde a pergunta feita")
    if resposta.action != "accept":
        return ReservaOut(reservado=False, motivo="recusado" if resposta.action == "decline" else "cancelado")
    escolhida = (resposta.content or {}).get("sala")
    if escolhida not in estado["alternativas"]:
        raise ErroDeNegocio(f"Escolha fora das alternativas oferecidas: {escolhida}")
    if central.conflitos(escolhida, central.validar(escolhida, pedido["inicio"], pedido["fim"])):
        # A alternativa foi ocupada entre a pergunta e a resposta: pergunta de novo.
        return _perguntar(pedido, ctx)
    return _reservada(central.reservar(escolhida, pedido["inicio"], pedido["fim"], pedido["responsavel"]))


def _reservada(reserva: dict[str, str]) -> ReservaOut:
    return ReservaOut(
        reserva=reserva["id"],
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=central.versao_da_politica,
    )


def _exigir_elicitation_em_form(ctx: Context) -> None:
    """Sem a capability de elicitation em form mode nao ha a quem perguntar: -32021.

    Mesma regra do SDK: `elicitation: {}` sem modos e a forma antiga e conta como form;
    quem declara so `url` nao declarou form.
    """
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    exigidas = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by tool 'reservar_sala'",
        data=MissingRequiredClientCapabilityErrorData(required_capabilities=exigidas).model_dump(
            by_alias=True, mode="json", exclude_none=True
        ),
    )


@mcp.resource("politica://uso", name="politica-de-uso", description="Politica de uso das salas.", mime_type="text/markdown")
def politica_de_uso() -> str:
    return central.politica


class RegistroDeRequests:
    """Middleware ASGI: escreve no stderr metodo, id e traceparent de cada request.

    Fica antes do SDK de proposito, para registrar tambem o que o SDK rejeita.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        partes: list[dict] = []
        while True:
            mensagem = await receive()
            partes.append(mensagem)
            if mensagem["type"] != "http.request" or not mensagem.get("more_body"):
                break
        self._registrar(b"".join(p.get("body", b"") for p in partes if p["type"] == "http.request"))

        async def repetir() -> dict:
            return partes.pop(0) if partes else await receive()

        await self.app(scope, repetir, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            request = json.loads(corpo)
            params = request.get("params") or {}
            meta = params.get("_meta") or {}
            alvo = params.get("name") or params.get("uri") or "-"
            retry = " retry=sim" if "requestState" in params else ""
            capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
            linha = (
                f"metodo={request.get('method')} id={request.get('id')!r} alvo={alvo}"
                f" traceparent={meta.get('traceparent', '-')}"
                f" capabilities={json.dumps(capabilities, separators=(',', ':'))}{retry}"
            )
        except (ValueError, AttributeError):
            linha = "request ilegivel"
        print(f"[mcp] {linha}", file=sys.stderr, flush=True)


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, host=host)
    uvicorn.run(RegistroDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
