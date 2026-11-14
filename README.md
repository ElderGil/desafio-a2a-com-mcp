# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio da Hill Valley Tech. São dois processos:

- `servidor-mcp/`: servidor MCP em Streamable HTTP (porta 7301, `/mcp`), sem sessão, com as tools `listar_salas`, `consultar_disponibilidade` e `reservar_sala`, o resource `politica://uso` e o ciclo de MRTR na reserva.
- `agente/`: agente que é host MCP por dentro e servidor A2A v1.0 por fora (porta 7300, `/a2a`). Não usa LLM: interpreta o pedido em formato fixo e traduz protocolo.

Stack: Python 3.12, SDK oficial `mcp` 2.3.0 (spec 2026-07-28) e Starlette. Versões travadas em `pyproject.toml` e `uv.lock`.

## Como rodar

Pré-requisitos: [uv](https://docs.astral.sh/uv/) e Python 3.10+ no sistema para o validador. O `uv` baixa o Python 3.12 do projeto sozinho.

```bash
git clone <url-deste-repositorio> a-ponte && cd a-ponte
uv sync
```

Gere a chave que protege o `requestState` e exporte-a **nos terminais onde o servidor MCP vai rodar**. Ela precisa ter pelo menos 32 bytes e ser a mesma entre reinícios do servidor, senão um `requestState` emitido antes do restart deixa de ser aceito.

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

Terminal 1, servidor MCP (o stderr mostra método, id, `traceparent` e capabilities de cada request):

```bash
uv run servidor-mcp/servidor.py
```

Terminal 2, agente:

```bash
uv run agente/app.py
```

Terminal 3, validador (sempre com os dois processos recém-iniciados; as reservas de uma execução mudam o resultado da seguinte):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Atalho: `./subir.sh` encerra o que estiver nas duas portas e sobe os dois processos do zero, com o stderr do servidor MCP no terminal e cópias em `logs/`.

Sem `uv`: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`, e troque `uv run` por `.venv/bin/python`.

Variáveis opcionais, com os padrões do enunciado: `MCP_HOST` (127.0.0.1), `MCP_PORT` (7301), `AGENTE_HOST` (127.0.0.1), `AGENTE_PORT` (7300), `AGENTE_URL` (URL publicada no Agent Card, `http://127.0.0.1:7300`) e `MCP_URL` (`http://127.0.0.1:7301/mcp`).

Teste manual da ponte, com os corpos de `exemplos/wire/`:

```bash
curl -s http://localhost:7300/.well-known/agent-card.json

# sala ocupada: a Task para em TASK_STATE_INPUT_REQUIRED com "alternativas: sala-fusca, sala-mirante"
python3 -c "import json;print(json.dumps(json.load(open('exemplos/wire/08-a2a-send-message.json'))['request']['body']))" \
  | curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' \
      -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' -d @-

# continuação: troque <task-id> pelo id devolvido acima
curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' -d '{
  "jsonrpc":"2.0","id":2,"method":"SendMessage",
  "params":{"message":{"messageId":"msg-2","role":"ROLE_USER","taskId":"<task-id>","parts":[{"text":"escolha=sala-mirante"}]}}}'

curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"GetTask","params":{"id":"<task-id>"}}'
```

## Onde a ponte acontece

Toda a costura está em `agente/ponte.py`.

O `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` na função `_aplicar`: quando o resultado do `tools/call` é um `InputRequiredResult`, ela guarda na Task um objeto `Pausa` (o `requestState`, a chave do `inputRequests`, as alternativas na ordem do `enum` e os argumentos originais) e chama `task.responder(INPUT_REQUIRED, "alternativas: ...")`. O agente só enxerga esse resultado cru porque `agente/cliente_mcp.py` chama a tool pelo nível de sessão do SDK com `allow_input_required=True`; o `Client.call_tool` comum responderia a elicitation sozinho e a Task nunca pausaria.

O `requestState` volta para o servidor na função `continuar`: ela valida `escolha=<valor>` contra as alternativas da `Pausa`, monta a resposta da elicitation (`accept` com a sala, ou `decline` para `escolha=recusar`) e repete o mesmo `tools/call` com `input_responses={pausa.chave: resposta}` e `request_state=pausa.request_state`, sem abrir nem modificar o valor. É um request JSON-RPC novo, com id novo, e leva o mesmo trace-id da Task.

Do outro lado, em `servidor-mcp/servidor.py`, `_perguntar` é onde o servidor termina a resposta pedindo a escolha, e `_retomar` é onde ele reconstrói o pedido a partir do `requestState` e conclui.

O `requestState` não sai do agente: `Task.para_wire` (`agente/tasks.py`) serializa `id`, `contextId`, `status`, `history` e `artifacts`, e não a `Pausa`.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário do SDK, `RequestStateSecurity(keys=[...], ttl=900)`, configurado no `MCPServer`. A tool escreve e lê texto claro; o SDK sela na saída e verifica na volta com AES-256-GCM (AEAD), com a chave derivada de `REQUEST_STATE_SECRET` por HKDF-SHA256. Além da integridade, o selo amarra o token ao método, ao nome da tool, a um digest dos argumentos e ao nome do servidor. Qualquer falha (adulteração, expiração, argumentos diferentes no retry) é rejeitada com `-32602` "Invalid or expired requestState" antes de chegar na tool. O servidor não sobe se a variável faltar ou tiver menos de 32 bytes, e não existe chave no código.

**Validade.** 15 minutos (`VALIDADE_DO_REQUEST_STATE`), dentro da faixa de 5 a 30 pedida.

**O que viaja no `requestState`.** O pedido original (sala, início, fim, responsável) e as alternativas oferecidas. O servidor não guarda nada entre o `input_required` e o retry, então um retry depois de um restart conclui a reserva. No retry valem os valores selados; a escolha precisa estar entre as alternativas seladas.

**Por que a forma manual do MRTR e não `Resolve`/`Elicit`.** A primeira versão usava o resolver do SDK, e passava no validador. Mas o SDK só aceita a resposta se a pergunta, renderizada de novo no retry, for idêntica à que foi selada. Como as alternativas dependem das reservas em memória, um restart do servidor mudava a pergunta e o retry devolvia outro `input_required` em vez de concluir. Com a tool devolvendo o `InputRequiredResult` ela mesma, as alternativas ficam dentro do estado selado e o restart deixa de importar. O custo foi escrever a checagem de capability (`_exigir_elicitation_em_form`, erro `-32021`), que o resolver fazia sozinho.

**Estado das Tasks.** Dicionário em memória no agente (`Tarefas` em `agente/tasks.py`). Reiniciar o agente perde as Tasks, o que o enunciado permite. A pausa é por Task, então duas Tasks pausadas ao mesmo tempo não trocam de `requestState`.

**A2A sem SDK.** O binding JSON-RPC do desafio são dois métodos e um GET, escritos direto em Starlette. Assim a ordem dos estados (`SUBMITTED` → `WORKING` → terminal ou `INPUT_REQUIRED`) e a leitura do header `traceparent` ficam explícitas. Mensagem para Task em estado terminal devolve erro `-32004`; Task inexistente, `-32001`; pedido fora do formato fixo, `-32602`, sem abrir Task.

**Regra de negócio só no servidor MCP.** O agente não sabe o que é conflito, política ou alternativa. Ele lê o `enum` da elicitation para validar a escolha e a versão da política pelo resource `politica://uso`; todo o resto vem das respostas do servidor.

**Limitação do SDK: capability declarada.** O enunciado pede `{"elicitation": {"form": {}}}`. O cliente do SDK Python 2.3.0 anuncia `{"elicitation": {"form": {}, "url": {}}}` sempre que um callback de elicitation é registrado, e não há opção para declarar só o form mode. Trecho de `mcp/client/session.py`, em `_build_capabilities`:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

Tentei sobrepor a chave `io.modelcontextprotocol/clientCapabilities` no `_meta` do request e o SDK carimba o valor dele por cima. A capability de form mode é declarada em todo request, como o critério exige; o `url` vem a mais. Preferi manter o cliente oficial a reescrever o transporte só para remover esse campo. O stderr do servidor MCP mostra as capabilities recebidas em cada request.

## Saída do validador

Última execução, com os dois processos recém-iniciados (código de saída 0):

```
trace-id desta execucao: d77637411ed91d9e1640b728acd21e14
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

No stderr do servidor MCP da mesma execução, o `tools/list` do agente (id 1) aparece antes do primeiro `tools/call`, com o trace-id do validador. O par da reserva que passou pela pausa é o id 4 (pedido inicial) e o id 5 (retry, marcado com `retry=sim`):

```
[mcp] metodo=tools/list id=1 alvo=- traceparent=00-d77637411ed91d9e1640b728acd21e14-20873286c37489e7-01 capabilities={"elicitation":{"form":{},"url":{}}}
[mcp] metodo=tools/call id=2 alvo=reservar_sala traceparent=00-d77637411ed91d9e1640b728acd21e14-63bff7576de27926-01 capabilities={"elicitation":{"form":{},"url":{}}}
[mcp] metodo=resources/read id=3 alvo=politica://uso traceparent=00-d77637411ed91d9e1640b728acd21e14-357a1ff5f2697ded-01 capabilities={"elicitation":{"form":{},"url":{}}}
[mcp] metodo=tools/call id=4 alvo=reservar_sala traceparent=00-d77637411ed91d9e1640b728acd21e14-939c45cadcb63cd1-01 capabilities={"elicitation":{"form":{},"url":{}}}
[mcp] metodo=tools/call id=5 alvo=reservar_sala traceparent=00-d77637411ed91d9e1640b728acd21e14-3b82adf2b73c090b-01 capabilities={"elicitation":{"form":{},"url":{}}} retry=sim
```
