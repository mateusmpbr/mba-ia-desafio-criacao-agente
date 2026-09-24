"""Camada entre a API e o ADK: sessões, execução do Runner e confirmações."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from google.adk.agents import RunConfig
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.apps import App
from google.adk.events import Event
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types

from . import config, db


# Teto de chamadas ao modelo por requisição: evita laços de transferência.
MAX_CHAMADAS_MODELO = 20


class SessaoNaoEncontrada(Exception):
    pass


class ConfirmacaoNaoPendente(Exception):
    pass


def confirmacoes_pendentes(session: Session) -> list[dict[str, Any]]:
    """Confirmações pedidas pelo ADK e ainda não respondidas nesta sessão.

    Um pedido é uma chamada ``adk_request_confirmation`` gerada pelo próprio
    ADK quando a tool chama ``request_confirmation``. Ele deixa de estar
    pendente assim que existe um evento do usuário com a resposta desse id —
    e a única forma de gerar esse evento é a rota de confirmações.
    """
    pedidos: dict[str, types.FunctionCall] = {}
    respondidos: set[str] = set()
    for evento in session.events:
        for fc in evento.get_function_calls():
            if fc.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME and fc.id:
                pedidos[fc.id] = fc
        if evento.author == "user":
            for fr in evento.get_function_responses():
                if fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME and fr.id:
                    respondidos.add(fr.id)

    pendentes = []
    for id_, fc in pedidos.items():
        if id_ in respondidos:
            continue
        args = fc.args or {}
        original = args.get("originalFunctionCall") or {}
        confirmacao = args.get("toolConfirmation") or {}
        detalhes = confirmacao.get("payload") or original.get("args") or {}
        pendentes.append(
            {
                "id": id_,
                "acao": original.get("name", ""),
                "descricao": confirmacao.get("hint", ""),
                "detalhes": detalhes,
            }
        )
    return pendentes


def _texto_da_resposta(eventos: list[Event]) -> str:
    partes: list[str] = []
    for evento in eventos:
        if evento.author == "user" or evento.partial or not evento.content:
            continue
        for parte in evento.content.parts or []:
            if parte.text and not parte.thought:
                partes.append(parte.text.strip())
    return "\n\n".join(p for p in partes if p)


class Assistente:
    def __init__(self, app: App, session_service: BaseSessionService | None = None):
        config.DIR_VAR.mkdir(parents=True, exist_ok=True)
        self.app = app
        self.session_service = session_service or SqliteSessionService(str(config.DB_SESSOES))
        self.runner = Runner(app=app, session_service=self.session_service)
        # Serializa o processamento de uma mesma sessão (duas mensagens da
        # mesma conversa não correm em paralelo). Sessões diferentes correm
        # em paralelo; a disputa entre elas é resolvida no banco.
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def close(self) -> None:
        await self.runner.close()

    # ------------------------------------------------------------------
    # Sessões
    # ------------------------------------------------------------------

    async def criar_sessao(self, apartamento: str) -> str:
        sessao = await self.session_service.create_session(
            app_name=self.app.name,
            user_id=apartamento,
            # Único ponto em que o apartamento é gravado no state.
            state={config.STATE_APARTAMENTO: apartamento},
        )
        db.registrar_sessao(sessao.id, apartamento)
        return sessao.id

    async def obter_sessao(self, session_id: str) -> Session:
        apartamento = db.apartamento_da_sessao(session_id)
        sessao = (
            await self.session_service.get_session(
                app_name=self.app.name, user_id=apartamento, session_id=session_id
            )
            if apartamento
            else None
        )
        if sessao is None:
            raise SessaoNaoEncontrada(session_id)
        return sessao

    async def eventos(self, session_id: str) -> list[dict[str, Any]]:
        sessao = await self.obter_sessao(session_id)
        return [
            e.model_dump(mode="json", by_alias=True, exclude_none=True) for e in sessao.events
        ]

    # ------------------------------------------------------------------
    # Conversa
    # ------------------------------------------------------------------

    async def enviar_mensagem(self, session_id: str, texto: str) -> dict[str, Any]:
        mensagem = types.Content(role="user", parts=[types.Part(text=texto)])
        async with self._locks[session_id]:
            sessao = await self.obter_sessao(session_id)
            return await self._executar(sessao, mensagem)

    async def responder_confirmacao(
        self, session_id: str, confirmacao_id: str, confirmado: bool
    ) -> dict[str, Any]:
        async with self._locks[session_id]:
            sessao = await self.obter_sessao(session_id)
            # Garantia 1: só aceita id pendente NESTA sessão. Id inexistente,
            # de outra sessão ou já respondido não chega ao Runner.
            if confirmacao_id not in {p["id"] for p in confirmacoes_pendentes(sessao)}:
                raise ConfirmacaoNaoPendente(confirmacao_id)
            resposta = types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=confirmacao_id,
                            name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                            response={"confirmed": confirmado},
                        )
                    )
                ],
            )
            return await self._executar(sessao, resposta)

    async def _executar(self, sessao: Session, mensagem: types.Content) -> dict[str, Any]:
        eventos: list[Event] = []
        resposta: str | None = None
        try:
            async for evento in self.runner.run_async(
                user_id=sessao.user_id,
                session_id=sessao.id,
                new_message=mensagem,
                run_config=RunConfig(max_llm_calls=MAX_CHAMADAS_MODELO),
            ):
                eventos.append(evento)
        except LlmCallsLimitExceededError:
            resposta = "Não consegui concluir seu pedido agora. Pode reformular, por favor?"
        atualizada = await self.obter_sessao(sessao.id)
        return {
            "resposta": resposta or _texto_da_resposta(eventos),
            "confirmacoes_pendentes": confirmacoes_pendentes(atualizada),
        }
