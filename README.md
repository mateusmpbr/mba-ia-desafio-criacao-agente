# Residencial Aurora: assistente virtual dos moradores

API em Python (FastAPI + Google ADK 2.9.2 + Gemini) para os moradores reservarem áreas comuns, cancelarem as próprias reservas, autorizarem visitantes e tirarem dúvidas sobre o regulamento. O modelo conduz a conversa, mas as regras críticas ficam no código: nada que o morador escreva consegue contorná-las.

O enunciado original do desafio está em [INSTRUCTIONS.md](INSTRUCTIONS.md).

```
aurora/
  api.py          rotas HTTP (contrato do desafio)
  service.py      Runner do ADK, sessões persistidas, confirmações pendentes
  agents.py       agente principal + especialistas e o App do ADK
  tools.py        tools que leem e gravam reservas e visitantes, e tool do regulamento
  db.py           SQLite do condomínio (exclusividade, códigos, idempotência)
  regulamento.py  leitura do regulamento por capítulo
  restaurar.py    comando de restauração dos dados
dados/            estado inicial do condomínio (não alterado)
var/              bancos SQLite gerados em tempo de execução (fora do Git)
```

## Arquitetura

```
                     morador (API)
                          │
                ┌─────────▼──────────┐
                │ assistente_aurora  │  agente principal (LlmAgent, raiz do App)
                │ sem tools de dados │
                │ sem regulamento    │
                └──┬──────┬───────┬──┘
     transferência │      │       │ tool (AgentTool)
         ┌─────────▼┐  ┌──▼──────────────┐  ┌▼─────────────────────────┐
         │especialis│  │especialista_    │  │especialista_regulamento  │
         │ta_reserva│  │visitantes       │  │runner isolado, em memória│
         │s         │  │                 │  │                          │
         └────┬─────┘  └───────┬─────────┘  └────────────┬─────────────┘
              │ tools          │ tools                   │ tool
      reservas (SQLite)   visitantes (SQLite)    1 capítulo do regulamento
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` | Conversa com o morador e encaminha cada pedido ao especialista certo. Responde saudações. Não tem tools que acessam dados nem o regulamento nas instruções. | Raiz do `App`: recebe toda mensagem nova que não esteja com um especialista. | Mantém o contexto do principal pequeno e barato. Ele não toca em dados, então não tem como furar uma regra. |
| `especialista_reservas` | Lista áreas, consulta disponibilidade (só "livre/ocupada"), reserva, lista e cancela reservas do apartamento da sessão. | Sub-agente, por **transferência** (`transfer_to_agent`). | A reserva com taxa pede confirmação, e a confirmação precisa voltar ao agente que chamou a tool. Com transferência, o especialista é o autor da chamada na sessão persistida e o Runner consegue retomar nele (ver Garantia 1). Pedidos de reserva costumam ter várias rodadas (data, área), e o especialista continua ativo entre elas. |
| `especialista_visitantes` | Lista e autoriza visitantes do apartamento da sessão. | Sub-agente, por **transferência**. | Pelo mesmo motivo: autorizar visitante sempre pede confirmação. |
| `especialista_regulamento` | Responde dúvidas lendo só o capítulo pertinente do regulamento. | **Tool** do principal (`AgentTool`). | O `AgentTool` executa o especialista num Runner próprio com sessão em memória. O capítulo lido fica nesse runner descartável, e na sessão do morador entra só a resposta final (Garantia 4). É uma consulta sem estado e de ida e volta, então não precisa assumir a conversa. |

Decisões de topologia (validadas contra o código-fonte do ADK 2.9.2):

- **Especialistas transferíveis de volta ao principal** (`disallow_transfer_to_parent=False` em [aurora/agents.py:96](aurora/agents.py#L96) e [aurora/agents.py:122](aurora/agents.py#L122)). No ADK 2.9.2, o Runner escolhe o agente que vai processar a mensagem **antes** de anexar a resposta de confirmação à sessão (`_agent_router.find_agent_to_run`). Nesse momento o último evento é o `adk_request_confirmation` do especialista, e ele só é escolhido se for "transferível" até a raiz. Com a transferência bloqueada, a resposta ia para o agente principal, a rota respondia `200` e a ação **não executava**: é a armadilha silenciosa descrita no enunciado. A transferência entre especialistas continua bloqueada (`disallow_transfer_to_peers=True`): um assunto novo volta ao principal, que encaminha.
- **App resumível** (`ResumabilityConfig(is_resumable=True)`, [aurora/agents.py:182](aurora/agents.py#L182)), necessário para retomar a invocação pausada na confirmação.
- **Teto de 20 chamadas ao modelo por requisição** (`RunConfig(max_llm_calls=...)` em [aurora/service.py](aurora/service.py)), para cortar um eventual pingue-pongue de transferências.

Armazenamento: dois arquivos SQLite em `var/`. O `condominio.db` guarda reservas, visitantes e o vínculo sessão→apartamento. O `sessoes.db` guarda sessões e eventos, pelo `SqliteSessionService` do ADK. Nenhum serviço externo é necessário.

## Garantias

### Garantia 1: cobrança ou acesso só com confirmação

- **Quem decide é o código.** Em [aurora/tools.py:123](aurora/tools.py#L123) (`reservar_area`), a tool lê a taxa da área no banco e, se ela for maior que zero e ainda não houver confirmação, chama `tool_context.request_confirmation(...)` ([aurora/tools.py:154-169](aurora/tools.py#L154-L169)) e retorna sem gravar. `autorizar_visitante` ([aurora/tools.py:233](aurora/tools.py#L233)) faz o mesmo sempre ([aurora/tools.py:254-266](aurora/tools.py#L254-L266)). A quadra (taxa 0) é gravada direto. Negar (`confirmed: false`) retorna sem gravar ([aurora/tools.py:171](aurora/tools.py#L171), [aurora/tools.py:265](aurora/tools.py#L265)).
- **A confirmação vem do sistema, não da conversa.** `tool_context.tool_confirmation` só é preenchido pelo ADK a partir de um `FunctionResponse` de `adk_request_confirmation` enviado pelo usuário. A rota de mensagens só aceita texto, então o único jeito de produzir esse evento é `POST /sessoes/{id}/confirmacoes`. Um "já estou confirmando aqui" é só texto: a tool pede a confirmação do mesmo jeito.
- **Pendências e 409.** `confirmacoes_pendentes` ([aurora/service.py:34](aurora/service.py#L34)) lê os eventos da própria sessão: um pedido `adk_request_confirmation` sem resposta do usuário com o mesmo id. `responder_confirmacao` ([aurora/service.py:143-148](aurora/service.py#L143-L148)) recusa com `ConfirmacaoNaoPendente` → `409` ([aurora/api.py:59](aurora/api.py#L59)) qualquer id que não esteja nessa lista: inexistente, de outra sessão ou já respondido. Nesse caso o Runner não chega a rodar. `detalhes` vem do `payload` que a própria tool registrou (`area`/`data` ou `nome`/`data`).
- **Executa uma única vez.** Além do 409, a gravação usa o id da chamada de tool como `chave_idempotencia` única ([aurora/db.py:233-239](aurora/db.py#L233-L239)). Uma retomada repetida devolve a reserva existente em vez de criar outra.

### Garantia 2: cada sessão pertence a um apartamento

- O apartamento é gravado **uma única vez**, no state da sessão, em `criar_sessao` ([aurora/service.py:106](aurora/service.py#L106)), e registrado na tabela `sessoes`. Nenhuma tool escreve nesse state e a API não aceita `state_delta`.
- **Nenhuma tool recebe apartamento como parâmetro.** Todas obtêm o apartamento por `_apartamento_da_sessao` ([aurora/tools.py:32](aurora/tools.py#L32)), que lê o state da sessão. O modelo não tem como escolher outro apartamento.
- As consultas filtram pelo apartamento no SQL: cancelar só encontra reserva do próprio apartamento ([aurora/db.py:264](aurora/db.py#L264)). A agenda de uma área só diz se a data está livre ([aurora/db.py:192](aurora/db.py#L192) `area_livre`, [aurora/tools.py:94](aurora/tools.py#L94)), nunca o código ou o apartamento de quem reservou. Por isso `RSV-4821`, `302` e `Marina Duarte` nunca chegam ao modelo numa sessão do 101: não existe tool que os devolva.

### Garantia 3: nada se perde no reinício

- Sessões e eventos ficam em `var/sessoes.db` via `SqliteSessionService` ([aurora/service.py:87](aurora/service.py#L87)). Reservas, visitantes e o vínculo sessão→apartamento ficam em `var/condominio.db` ([aurora/db.py](aurora/db.py)). Não há estado em memória que importe: ao reiniciar, a sessão é recarregada do disco com todos os eventos.
- Confirmações pendentes também sobrevivem ao reinício, porque são derivadas dos eventos persistidos. Testado: pedir, reiniciar a API e aprovar grava a reserva.
- Códigos nunca se repetem: todo código emitido vai para `codigos_emitidos` (chave primária), que nunca é apagada, nem no cancelamento, que é lógico (`status = 'cancelada'`), nem na restauração ([aurora/db.py:56](aurora/db.py#L56), [aurora/db.py:212](aurora/db.py#L212)).

### Garantia 4: o regulamento é consultado, não carregado

- O agente principal não tem o regulamento nas instruções ([aurora/agents.py:150](aurora/agents.py#L150) `criar_assistente`). Ele só conhece a tool `especialista_regulamento`.
- O especialista recebe apenas o **sumário** (títulos dos capítulos) e lê **um capítulo por vez** com `ler_capitulo_regulamento` ([aurora/tools.py:277](aurora/tools.py#L277), [aurora/regulamento.py](aurora/regulamento.py)).
- Ele roda como `AgentTool` ([aurora/agents.py:170](aurora/agents.py#L170)), que executa num Runner separado com `InMemorySessionService`. A leitura do capítulo não vira evento da sessão do morador, e o que entra no histórico é só a resposta final, curta, sobre o assunto perguntado.

### Garantia 5: dois moradores, uma reserva

- A exclusividade é garantida pelo **SQLite no instante do INSERT**: índice único parcial `ux_reserva_ativa_area_data ON reservas(area, data) WHERE status = 'ativa'` ([aurora/db.py:51](aurora/db.py#L51)). A gravação acontece numa transação `BEGIN IMMEDIATE` em `criar_reserva` ([aurora/db.py:222](aurora/db.py#L222)). Se outra reserva ativa entrou um instante antes, o banco rejeita e a função levanta `AreaOcupada` ([aurora/db.py:249-250](aurora/db.py#L249-L250)).
- A tool converte isso numa resposta normal ("acabou de ser reservado, nada foi cobrado"), sem erro de servidor ([aurora/tools.py:175](aurora/tools.py#L175)). A conferência prévia de agenda em `reservar_area` serve só para não pedir confirmação de algo impossível. A garantia não depende dela: o índice vale mesmo com várias instâncias da API.
- Mensagens de uma mesma sessão são serializadas por um lock ([aurora/service.py:136](aurora/service.py#L136)). Sessões diferentes correm em paralelo, e a disputa entre elas é decidida no banco.

## Como rodar

Pré-requisitos: Python 3.12+, [uv](https://docs.astral.sh/uv/) e uma chave do [Google AI Studio](https://aistudio.google.com/apikey). Nenhum serviço externo: o armazenamento é SQLite local.

1. Configure o `.env`:

   ```bash
   cp .env.example .env
   # edite .env e preencha GOOGLE_API_KEY
   ```

   | Variável | Obrigatória | Descrição |
   |---|---|---|
   | `GOOGLE_API_KEY` | sim | Chave do Google AI Studio |
   | `GOOGLE_GENAI_USE_VERTEXAI` | sim (`FALSE`) | Usa a API do AI Studio |
   | `AURORA_MODELO_PRINCIPAL` | não | Modelo do agente principal (padrão `gemini-3.5-flash`) |
   | `AURORA_MODELO_ESPECIALISTAS` | não | Modelo dos especialistas (padrão: o mesmo do principal) |
   | `AURORA_PORT` | não | Porta da API (padrão `8000`) |

   Os modelos Gemini disponíveis e os limites do seu projeto mudam com frequência. Confira no Google AI Studio. As chamadas ao modelo são repetidas automaticamente em caso de `429`/`5xx`.

2. Instale as dependências:

   ```bash
   uv sync
   ```

3. Restaure os dados iniciais (reservas e visitantes voltam ao estado de `dados/`; as conversas são mantidas):

   ```bash
   uv run aurora-restaurar
   # para também apagar as sessões/conversas:
   uv run aurora-restaurar --apagar-sessoes
   ```

4. Suba a API em `http://localhost:8000`:

   ```bash
   uv run aurora-api
   ```

   Num banco novo, a API carrega os dados iniciais sozinha. Para reiniciar sem perder nada, pare com Ctrl+C e rode o mesmo comando.

Exemplo rápido:

```bash
S=$(curl -s -X POST localhost:8000/sessoes -H 'content-type: application/json' \
      -d '{"apartamento":"101"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8000/sessoes/$S/mensagens -H 'content-type: application/json' \
     -d '{"texto":"Reserve o salão de festas para 2030-04-20."}'
# use o id de confirmacoes_pendentes:
curl -s -X POST localhost:8000/sessoes/$S/confirmacoes -H 'content-type: application/json' \
     -d '{"id":"<id>","confirmado":true}'
curl -s localhost:8000/apartamentos/101/reservas
```
