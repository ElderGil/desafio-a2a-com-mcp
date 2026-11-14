"""A ponte: onde o estado do MCP e o estado do A2A se encontram.

O agente traduz protocolo, nao dominio. Conflito, politica e alternativas sao
decisao do servidor MCP; aqui so se decide o que cada resposta dele significa
para a Task.

    input_required (MCP)  ->  TASK_STATE_INPUT_REQUIRED (A2A)   em `_aplicar`
    escolha=<id>  (A2A)   ->  retry do tools/call (MCP)         em `continuar`
"""

from __future__ import annotations

import json
import re
from typing import Any

import mcp.types as types
from mcp import MCPError

from cliente_mcp import ClienteMCP
from tasks import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, Pausa, Task

TOOL = "reservar_sala"
RECUSAR = "recusar"

_PEDIDO = re.compile(r"^reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+)$")
_ESCOLHA = re.compile(r"^escolha=(?P<valor>\S+)$")

FORMATO_DO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"


class PedidoInvalido(Exception):
    pass


def interpretar_pedido(texto: str) -> dict[str, str]:
    """Formato fixo, sem linguagem natural e sem LLM: o mesmo texto da sempre o mesmo pedido."""
    encontrado = _PEDIDO.match(texto.strip())
    if not encontrado:
        raise PedidoInvalido(f"pedido fora do formato: {FORMATO_DO_PEDIDO}")
    return {campo: valor.strip() for campo, valor in encontrado.groupdict().items()}


def _alternativas(pedido: types.ElicitRequest) -> list[str]:
    """As opcoes na ordem em que o servidor as colocou no schema da elicitation."""
    campo = pedido.params.requested_schema["properties"]["sala"]
    return list(campo.get("enum") or [campo["const"]])


def _linha_de_alternativas(pausa: Pausa) -> str:
    return "alternativas: " + ", ".join(pausa.alternativas)


def _texto(resultado: types.CallToolResult) -> str:
    return " ".join(bloco.text for bloco in resultado.content if isinstance(bloco, types.TextContent))


async def iniciar(task: Task, argumentos: dict[str, str], mcp: ClienteMCP) -> None:
    """Primeiro `SendMessage` de uma Task: chama a tool com o pedido."""
    task.transitar(WORKING)
    await _chamar(task, argumentos, mcp)


async def continuar(task: Task, texto: str, mcp: ClienteMCP) -> None:
    """`SendMessage` em uma Task pausada: devolve a resposta e o `requestState` ao MCP."""
    pausa = task.pausa
    assert pausa is not None
    encontrado = _ESCOLHA.match(texto.strip())
    valor = encontrado["valor"] if encontrado else None
    if valor != RECUSAR and valor not in pausa.alternativas:
        # Continua pausada: mesma pergunta, mesmo requestState.
        task.responder(INPUT_REQUIRED, _linha_de_alternativas(pausa))
        return

    if valor == RECUSAR:
        resposta = types.ElicitResult(action="decline")
    else:
        resposta = types.ElicitResult(action="accept", content={"sala": valor})

    task.transitar(WORKING)
    task.pausa = None
    # O retry: mesma tool, mesmos argumentos, id JSON-RPC novo, a mesma chave que
    # veio no inputRequests e o requestState ecoado sem abrir nem modificar.
    await _chamar(
        task,
        pausa.argumentos,
        mcp,
        input_responses={pausa.chave: resposta},
        request_state=pausa.request_state,
    )


async def _chamar(task: Task, argumentos: dict[str, Any], mcp: ClienteMCP, **retomada: Any) -> None:
    try:
        resultado = await mcp.chamar(TOOL, argumentos, task.trace_id, **retomada)
        await _aplicar(task, argumentos, resultado, mcp)
    except MCPError as e:
        task.responder(FAILED, f"Erro do servidor MCP ({e.error.code}): {e.error.message}")
    except Exception as e:  # servidor MCP fora do ar, resposta inesperada
        task.responder(FAILED, f"Falha ao falar com o servidor MCP: {e}")


async def _aplicar(
    task: Task,
    argumentos: dict[str, Any],
    resultado: types.CallToolResult | types.InputRequiredResult,
    mcp: ClienteMCP,
) -> None:
    if isinstance(resultado, types.InputRequiredResult):
        # AQUI o input_required do MCP vira TASK_STATE_INPUT_REQUIRED no A2A.
        # O requestState fica na Task; para fora so sai a lista de alternativas.
        chave, pedido = next(iter((resultado.input_requests or {}).items()))
        if not isinstance(pedido, types.ElicitRequest) or resultado.request_state is None:
            task.responder(FAILED, "O servidor MCP pediu uma entrada que este agente nao sabe repassar")
            return
        task.pausa = Pausa(
            request_state=resultado.request_state,
            chave=chave,
            alternativas=_alternativas(pedido),
            argumentos=argumentos,
        )
        task.responder(INPUT_REQUIRED, _linha_de_alternativas(task.pausa))
        return

    if resultado.is_error:
        task.responder(FAILED, _texto(resultado))
        return

    dados = resultado.structured_content or {}
    if not dados.get("reservado"):
        task.responder(CANCELED, "Reserva nao realizada: as alternativas foram recusadas.")
        return

    reserva = {
        "reserva": dados["reserva"],
        "sala": dados["sala"],
        "inicio": dados["inicio"],
        "fim": dados["fim"],
        "responsavel": dados["responsavel"],
        "politica": await mcp.versao_da_politica(task.trace_id),
    }
    task.entregar("reserva", json.dumps(reserva, ensure_ascii=False))
    task.responder(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")
