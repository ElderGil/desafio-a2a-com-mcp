"""Dominio da Central de Salas: salas, reservas em memoria e a politica de uso.

Nada aqui conhece MCP. As mensagens de erro sao o contrato do enunciado e nao
podem mudar de texto.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"

FUSO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_DE_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeNegocio(Exception):
    """Violacao de regra de sala ou de politica. Vira erro de execucao da tool."""


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime
    fim: datetime

    def sobrepoe(self, outro: Intervalo) -> bool:
        return self.inicio < outro.fim and outro.inicio < self.fim


def _instante(valor: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor)
    except ValueError as e:
        raise ErroDeNegocio(f"Data invalida: {valor}") from e
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=FUSO)
    return instante.astimezone(FUSO)


class CentralDeSalas:
    def __init__(self, pasta: Path = DADOS) -> None:
        self.salas: list[dict] = json.loads((pasta / "salas.json").read_text(encoding="utf-8"))
        self.reservas: list[dict] = json.loads((pasta / "reservas.json").read_text(encoding="utf-8"))
        self.politica: str = (pasta / "politica-de-uso.md").read_text(encoding="utf-8")
        self.versao_da_politica: str = self.politica.splitlines()[0].split(":", 1)[1].strip()
        self._proximo = 1 + max((int(r["id"].rsplit("-", 1)[1]) for r in self.reservas), default=0)

    def _sala(self, sala_id: str) -> dict:
        for sala in self.salas:
            if sala["id"] == sala_id:
                return sala
        raise ErroDeNegocio(f"Sala inexistente: {sala_id}")

    def validar(self, sala_id: str, inicio: str, fim: str) -> Intervalo:
        """Aplica sala e politica, nessa ordem, e devolve o intervalo normalizado."""
        self._sala(sala_id)
        intervalo = Intervalo(_instante(inicio), _instante(fim))
        if intervalo.fim <= intervalo.inicio:
            raise ErroDeNegocio(ERRO_INTERVALO)
        mesmo_dia = intervalo.inicio.date() == intervalo.fim.date()
        if not mesmo_dia or intervalo.inicio.time() < ABERTURA or intervalo.fim.time() > FECHAMENTO:
            raise ErroDeNegocio(ERRO_JANELA)
        if intervalo.fim - intervalo.inicio > DURACAO_MAXIMA:
            raise ErroDeNegocio(ERRO_DURACAO)
        return intervalo

    def conflitos(self, sala_id: str, intervalo: Intervalo) -> list[dict]:
        return [
            r
            for r in self.reservas
            if r["sala"] == sala_id and intervalo.sobrepoe(Intervalo(_instante(r["inicio"]), _instante(r["fim"])))
        ]

    def alternativas(self, sala_id: str, intervalo: Intervalo) -> list[str]:
        """Salas livres no intervalo com capacidade igual ou maior, no maximo tres."""
        minima = self._sala(sala_id)["capacidade"]
        candidatas = [
            s
            for s in self.salas
            if s["id"] != sala_id and s["capacidade"] >= minima and not self.conflitos(s["id"], intervalo)
        ]
        candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
        return [s["id"] for s in candidatas[:MAXIMO_DE_ALTERNATIVAS]]

    def reservar(self, sala_id: str, inicio: str, fim: str, responsavel: str) -> dict:
        intervalo = self.validar(sala_id, inicio, fim)
        if self.conflitos(sala_id, intervalo):
            raise ErroDeNegocio(ERRO_SEM_ALTERNATIVA)
        reserva = {
            "id": f"res-{self._proximo:04d}",
            "sala": sala_id,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
        }
        self._proximo += 1
        self.reservas.append(reserva)
        return reserva
