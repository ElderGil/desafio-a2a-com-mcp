"""Task do A2A: identidade, estado e produto. Guardadas em memoria.

A pausa (o que o servidor MCP devolveu no `input_required`) fica presa a Task e
nunca entra no JSON que sai para o cliente A2A: `para_wire` nao a serializa.
"""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"

TERMINAIS = {COMPLETED, CANCELED, FAILED}


class TransicaoInvalida(Exception):
    pass


@dataclass
class Pausa:
    """Estado do MRTR ligado a uma Task. O `request_state` e opaco: guardar e ecoar."""

    request_state: str
    chave: str
    alternativas: list[str]
    argumentos: dict[str, Any]


@dataclass
class Task:
    id: str
    context_id: str
    trace_id: str
    estado: str = SUBMITTED
    mensagem: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    pausa: Pausa | None = None

    def transitar(self, estado: str) -> None:
        if self.estado in TERMINAIS:
            raise TransicaoInvalida(f"a Task {self.id} esta em {self.estado}, que e terminal")
        print(f"[a2a] task={self.id} {self.estado} -> {estado}", file=sys.stderr, flush=True)
        self.estado = estado

    def registrar_usuario(self, mensagem: dict[str, Any]) -> None:
        self.history.append(mensagem)

    def responder(self, estado: str, texto: str) -> None:
        """Muda o estado e anexa a mensagem do agente ao historico da mesma Task."""
        self.transitar(estado)
        self.mensagem = {
            "messageId": f"msg-{secrets.token_hex(6)}",
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.history.append(self.mensagem)

    def entregar(self, nome: str, texto: str) -> None:
        self.artifacts.append({"artifactId": f"art-{secrets.token_hex(6)}", "name": nome, "parts": [{"text": texto}]})

    def para_wire(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem is not None:
            status["message"] = self.mensagem
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.history,
            "artifacts": self.artifacts,
        }


class Tarefas:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def abrir(self, trace_id: str, context_id: str | None = None) -> Task:
        task = Task(
            id=f"task-{secrets.token_hex(6)}",
            context_id=context_id or f"ctx-{secrets.token_hex(6)}",
            trace_id=trace_id,
        )
        self._tasks[task.id] = task
        print(f"[a2a] task={task.id} aberta em {task.estado}", file=sys.stderr, flush=True)
        return task

    def obter(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)
