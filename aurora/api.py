"""API HTTP do assistente (FastAPI)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.genai import errors as genai_errors
from pydantic import BaseModel

from . import db
from .agents import criar_app
from .service import Assistente, ConfirmacaoNaoPendente, SessaoNaoEncontrada

logger = logging.getLogger("aurora.api")


class CriarSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.assistente = Assistente(criar_app())
    try:
        yield
    finally:
        await app.state.assistente.close()


app = FastAPI(title="Residencial Aurora", lifespan=lifespan)


def _assistente(request: Request) -> Assistente:
    return request.app.state.assistente


@app.exception_handler(SessaoNaoEncontrada)
async def _sessao_nao_encontrada(_: Request, exc: SessaoNaoEncontrada) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Sessão não encontrada."})


@app.exception_handler(ConfirmacaoNaoPendente)
async def _confirmacao_nao_pendente(_: Request, exc: ConfirmacaoNaoPendente) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": "Não existe confirmação pendente com esse id nesta sessão."},
    )


@app.exception_handler(genai_errors.APIError)
async def _erro_modelo(_: Request, exc: genai_errors.APIError) -> JSONResponse:
    logger.exception("Falha ao chamar o modelo")
    return JSONResponse(
        status_code=503,
        content={"detail": f"O modelo não respondeu ({exc.code}). Tente novamente."},
    )


# --------------------------------------------------------------------------
# Conversa
# --------------------------------------------------------------------------


@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: CriarSessao, request: Request) -> dict[str, str]:
    apartamento = corpo.apartamento.strip()
    if not db.apartamento_existe(apartamento):
        raise HTTPException(status_code=422, detail="Apartamento inexistente.")
    return {"session_id": await _assistente(request).criar_sessao(apartamento)}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: Mensagem, request: Request) -> dict[str, Any]:
    return await _assistente(request).enviar_mensagem(session_id, corpo.texto)


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(
    session_id: str, corpo: RespostaConfirmacao, request: Request
) -> dict[str, Any]:
    return await _assistente(request).responder_confirmacao(
        session_id, corpo.id, corpo.confirmado
    )


@app.get("/sessoes/{session_id}/eventos")
async def eventos(session_id: str, request: Request) -> list[dict[str, Any]]:
    return await _assistente(request).eventos(session_id)


# --------------------------------------------------------------------------
# Verificação (leitura direta do banco, sem modelo)
# --------------------------------------------------------------------------


@app.get("/apartamentos/{numero}/reservas")
async def reservas(numero: str) -> list[dict[str, str]]:
    return db.reservas_do_apartamento(numero)


@app.get("/apartamentos/{numero}/visitantes")
async def visitantes(numero: str) -> list[dict[str, str]]:
    return db.visitantes_do_apartamento(numero)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "aurora.api:app",
        host=os.getenv("AURORA_HOST", "0.0.0.0"),
        port=int(os.getenv("AURORA_PORT") or 8000),
    )
