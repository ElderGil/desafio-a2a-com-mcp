"""O agente como servidor A2A v1.0 (binding JSON-RPC 2.0 sobre HTTP, porta 7300).

    GET  /.well-known/agent-card.json   Agent Card
    POST /a2a                           SendMessage e GetTask

Sem streaming e sem autenticacao: os dois estao fora do escopo do desafio.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import ponte
from cliente_mcp import ClienteMCP
from tasks import INPUT_REQUIRED, TERMINAIS, Tarefas

HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://127.0.0.1:{PORTA}").rstrip("/")
URL_DO_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

# Codigos de erro: JSON-RPC 2.0 e os especificos do A2A.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")

AGENT_CARD: dict[str, Any] = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}

tarefas = Tarefas()
mcp = ClienteMCP(URL_DO_MCP)


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


def _trace_id(request: Request) -> str:
    """O trace-id do cliente A2A, quando ele mandou um `traceparent` valido."""
    encontrado = _TRACEPARENT.match(request.headers.get("traceparent", "").strip().lower())
    return encontrado["trace"] if encontrado else secrets.token_hex(16)


def _texto(mensagem: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts") or [] if isinstance(p, dict))


async def send_message(params: dict[str, Any], request: Request) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not _texto(mensagem).strip():
        raise ErroA2A(INVALID_PARAMS, "params.message com uma parte de texto e obrigatorio")
    texto = _texto(mensagem)
    task_id = mensagem.get("taskId")

    if task_id is None:
        try:
            argumentos = ponte.interpretar_pedido(texto)
        except ponte.PedidoInvalido as e:
            raise ErroA2A(INVALID_PARAMS, str(e)) from e
        task = tarefas.abrir(_trace_id(request), mensagem.get("contextId"))
        task.registrar_usuario(mensagem)
        await ponte.iniciar(task, argumentos, mcp)
        return {"task": task.para_wire()}

    task = tarefas.obter(task_id)
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
    if task.estado in TERMINAIS:
        raise ErroA2A(UNSUPPORTED_OPERATION, f"A Task {task.id} esta em {task.estado}, que e terminal, e nao aceita mensagens")
    if task.estado != INPUT_REQUIRED:
        raise ErroA2A(UNSUPPORTED_OPERATION, f"A Task {task.id} esta em {task.estado} e nao espera entrada")
    task.registrar_usuario(mensagem)
    await ponte.continuar(task, texto, mcp)
    return {"task": task.para_wire()}


async def get_task(params: dict[str, Any], request: Request) -> dict[str, Any]:
    task_id = params.get("id")
    task = tarefas.obter(task_id) if isinstance(task_id, str) else None
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
    return {"task": task.para_wire()}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


def _erro(id_: Any, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensagem}})


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except ValueError:
        return _erro(None, PARSE_ERROR, "JSON invalido")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return _erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "request JSON-RPC 2.0 invalido")
    id_ = corpo.get("id")
    metodo = METODOS.get(corpo["method"])
    if metodo is None:
        return _erro(id_, METHOD_NOT_FOUND, f"metodo nao suportado: {corpo['method']}")
    params = corpo.get("params")
    if not isinstance(params, dict):
        return _erro(id_, INVALID_PARAMS, "params deve ser um objeto")
    try:
        resultado = await metodo(params, request)
    except ErroA2A as e:
        return _erro(id_, e.codigo, e.mensagem)
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})


async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


@contextlib.asynccontextmanager
async def ciclo_de_vida(app: Starlette):
    await mcp.abrir()
    try:
        yield
    finally:
        await mcp.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
