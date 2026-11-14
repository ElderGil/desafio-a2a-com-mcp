#!/usr/bin/env bash
# Sobe o servidor MCP (7301) e o agente (7300), sempre do zero.
# O stderr dos dois fica em logs/, e o do servidor MCP tambem aparece neste terminal.
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${REQUEST_STATE_SECRET:-}" ]; then
  echo 'Defina REQUEST_STATE_SECRET antes de subir:' >&2
  echo '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")' >&2
  exit 1
fi

mkdir -p logs
# Encerra o que ja estiver escutando nas duas portas (uma execucao anterior).
for porta in "${MCP_PORT:-7301}" "${AGENTE_PORT:-7300}"; do
  lsof -ti "tcp:$porta" -sTCP:LISTEN 2>/dev/null | xargs kill 2>/dev/null || true
done
sleep 1

uv run servidor-mcp/servidor.py 2> >(tee logs/servidor-mcp.log >&2) &
MCP_PID=$!
uv run agente/app.py 2> logs/agente.log &
AGENTE_PID=$!
trap 'kill $MCP_PID $AGENTE_PID 2>/dev/null || true' EXIT INT TERM

echo "servidor MCP em http://localhost:7301/mcp (pid $MCP_PID)"
echo "agente A2A   em http://localhost:7300/a2a (pid $AGENTE_PID)"
echo "Ctrl+C encerra os dois."
wait
