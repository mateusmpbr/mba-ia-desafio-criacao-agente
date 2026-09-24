"""Configuração central: caminhos, modelos e nomes compartilhados."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

DIR_DADOS = RAIZ / "dados"
DIR_VAR = Path(os.getenv("AURORA_DIR_VAR") or RAIZ / "var")

# Estado do condomínio (reservas, visitantes, vínculo sessão→apartamento).
DB_CONDOMINIO = DIR_VAR / "condominio.db"
# Sessões e eventos do ADK (SqliteSessionService).
DB_SESSOES = DIR_VAR / "sessoes.db"

APP_NAME = "residencial_aurora"

# Chave do state da sessão com o apartamento autenticado. É gravada apenas na
# criação da sessão (rota POST /sessoes) e nenhuma tool a altera.
STATE_APARTAMENTO = "apartamento"

MODELO_PRINCIPAL = os.getenv("AURORA_MODELO_PRINCIPAL") or "gemini-3.5-flash"
MODELO_ESPECIALISTAS = os.getenv("AURORA_MODELO_ESPECIALISTAS") or MODELO_PRINCIPAL
