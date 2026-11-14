"""O agente como host MCP: um cliente de verdade, falando HTTP com o servidor.

Tres cuidados que o desafio cobra moram aqui:

- as tools sao descobertas por `tools/list` antes da primeira chamada;
- a elicitation nunca e respondida pelo cliente. As chamadas saem pelo nivel de
  sessao com `allow_input_required=True`, que devolve o `input_required` cru
  para a ponte decidir. O callback so existe para declarar a capability: e o
  registro dele que faz o SDK anunciar `elicitation` no `_meta` de cada request;
- todo request leva o `traceparent` da Task dentro do `_meta`.
"""

from __future__ import annotations

import secrets
from contextlib import AsyncExitStack
from typing import Any

import mcp.types as types
from mcp.client import Client, ClientRequestContext

POLITICA = "politica://uso"
PROTOCOLO = "2026-07-28"


async def _nunca_responder(context: ClientRequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:
    raise RuntimeError("o agente nao responde elicitation: a pergunta pertence ao cliente A2A")


def traceparent(trace_id: str) -> str:
    """Mesmo trace-id da Task, span-id novo a cada request."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._pilha = AsyncExitStack()
        self._client: Client | None = None
        self._tools: set[str] | None = None

    async def abrir(self) -> None:
        """Cria o cliente. Nao ha handshake nem sessao: a versao do protocolo e fixada
        aqui e viaja no `_meta` de cada request, entao o servidor MCP pode subir depois."""
        client = Client(
            self.url,
            mode=PROTOCOLO,
            client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
            elicitation_callback=_nunca_responder,
        )
        self._client = await self._pilha.enter_async_context(client)

    async def fechar(self) -> None:
        await self._pilha.aclose()

    async def _conectado(self) -> Client:
        assert self._client is not None, "ClienteMCP.abrir() nao foi chamado"
        return self._client

    @staticmethod
    def _meta(trace_id: str) -> Any:
        return {"traceparent": traceparent(trace_id)}

    async def tools(self, trace_id: str) -> set[str]:
        """Descoberta em runtime: nenhuma lista de tools esta escrita no agente."""
        if self._tools is None:
            client = await self._conectado()
            resultado = await client.list_tools(meta=self._meta(trace_id), cache_mode="bypass")
            self._tools = {tool.name for tool in resultado.tools}
        return self._tools

    async def versao_da_politica(self, trace_id: str) -> str:
        client = await self._conectado()
        resultado = await client.session.read_resource(POLITICA, meta=self._meta(trace_id))
        texto = next(c.text for c in resultado.contents if isinstance(c, types.TextResourceContents))
        return texto.splitlines()[0].split(":", 1)[1].strip()

    async def chamar(
        self,
        tool: str,
        argumentos: dict[str, Any],
        trace_id: str,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """Um `tools/call`. Cada chamada e um request JSON-RPC novo, com id novo."""
        if tool not in await self.tools(trace_id):
            raise LookupError(f"o servidor MCP nao anuncia a tool {tool}")
        client = await self._conectado()
        return await client.session.call_tool(
            tool,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta=self._meta(trace_id),
            allow_input_required=True,
        )
