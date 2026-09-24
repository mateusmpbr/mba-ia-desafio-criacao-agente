"""Armazenamento do condomínio em SQLite.

Todas as regras que dependem do estado gravado ficam aqui, no instante da
escrita, e não na conversa:

- exclusividade de área por data: índice único parcial
  ``ux_reserva_ativa_area_data`` (só reservas ativas entram nele);
- códigos de reserva que nunca se repetem: tabela ``codigos_emitidos``, que
  guarda todo código já gerado e não é apagada nem no cancelamento;
- execução única de ações retomadas pelo ADK: coluna ``chave_idempotencia``
  (o id da chamada de tool), única em reservas e visitantes.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (
    numero  TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS areas (
    id   TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reservas (
    codigo             TEXT PRIMARY KEY,
    apartamento        TEXT NOT NULL,
    area               TEXT NOT NULL REFERENCES areas(id),
    data               TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ativa'
                       CHECK (status IN ('ativa', 'cancelada')),
    chave_idempotencia TEXT UNIQUE,
    criada_em          TEXT NOT NULL DEFAULT (datetime('now')),
    cancelada_em       TEXT
);

-- Garantia 5: no máximo uma reserva ATIVA por área e data, validado pelo
-- próprio SQLite no momento do INSERT.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa_area_data
    ON reservas (area, data) WHERE status = 'ativa';

-- Regra 5: todo código já emitido (inclusive de reservas canceladas ou
-- anteriores a uma restauração) fica registrado aqui para sempre.
CREATE TABLE IF NOT EXISTS codigos_emitidos (
    codigo TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS visitantes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento        TEXT NOT NULL,
    nome               TEXT NOT NULL,
    data               TEXT NOT NULL,
    chave_idempotencia TEXT UNIQUE,
    criado_em          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessoes (
    session_id  TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    criada_em   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class AreaOcupada(Exception):
    """A área já tem reserva ativa na data pedida."""


@dataclass(frozen=True)
class Area:
    id: str
    nome: str
    taxa: float

    @property
    def gera_cobranca(self) -> bool:
        return self.taxa > 0


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    config.DIR_VAR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_CONDOMINIO, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transacao() -> Iterator[sqlite3.Connection]:
    """Transação com lock de escrita desde o início (BEGIN IMMEDIATE)."""
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _ler_json(nome: str) -> list[dict]:
    return json.loads((config.DIR_DADOS / nome).read_text(encoding="utf-8"))


def inicializar() -> None:
    """Cria o schema e, num banco novo, carrega os dados iniciais."""
    with conectar() as conn:
        conn.executescript(SCHEMA)
        vazio = conn.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 0
    if vazio:
        restaurar()


def restaurar() -> None:
    """Volta reservas e visitantes ao estado de ``dados/*.json``."""
    with conectar() as conn:
        conn.executescript(SCHEMA)
    with transacao() as conn:
        conn.execute("DELETE FROM reservas")
        conn.execute("DELETE FROM visitantes")
        for apto in _ler_json("apartamentos.json"):
            conn.execute(
                "INSERT INTO apartamentos (numero, morador) VALUES (?, ?)"
                " ON CONFLICT(numero) DO UPDATE SET morador = excluded.morador",
                (apto["numero"], apto["morador"]),
            )
        for area in _ler_json("areas.json"):
            conn.execute(
                "INSERT INTO areas (id, nome, taxa) VALUES (?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET nome = excluded.nome, taxa = excluded.taxa",
                (area["id"], area["nome"], float(area["taxa"])),
            )
        for r in _ler_json("reservas.json"):
            conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                (r["codigo"], r["apartamento"], r["area"], r["data"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)",
                (r["codigo"],),
            )
        for v in _ler_json("visitantes.json"):
            conn.execute(
                "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
                (v["apartamento"], v["nome"], v["data"]),
            )


# --------------------------------------------------------------------------
# Áreas
# --------------------------------------------------------------------------


def listar_areas() -> list[Area]:
    with conectar() as conn:
        rows = conn.execute("SELECT id, nome, taxa FROM areas ORDER BY nome").fetchall()
    return [Area(r["id"], r["nome"], r["taxa"]) for r in rows]


def obter_area(area_id: str) -> Area | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT id, nome, taxa FROM areas WHERE id = ?", (area_id,)
        ).fetchone()
    return Area(r["id"], r["nome"], r["taxa"]) if r else None


# --------------------------------------------------------------------------
# Reservas
# --------------------------------------------------------------------------


def area_livre(area_id: str, data: str) -> bool:
    """Só diz se a data está livre; nunca expõe de quem é a reserva."""
    with conectar() as conn:
        r = conn.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area_id, data),
        ).fetchone()
    return r is None


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND status = 'ativa' ORDER BY data, area",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def _novo_codigo(conn: sqlite3.Connection) -> str:
    while True:
        codigo = f"RSV-{secrets.token_hex(3).upper()}"
        try:
            conn.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            return codigo
        except sqlite3.IntegrityError:
            continue  # já emitido alguma vez: sorteia outro


def criar_reserva(
    apartamento: str, area_id: str, data: str, chave_idempotencia: str | None
) -> dict:
    """Grava a reserva. A exclusividade é decidida aqui, no INSERT.

    Levanta ``AreaOcupada`` se outra reserva ativa já ocupa a área na data,
    mesmo que ela tenha sido gravada um instante antes por outra requisição.
    Se a mesma chamada de tool já gravou a reserva (retomada repetida),
    devolve a reserva existente em vez de criar outra.
    """
    try:
        with transacao() as conn:
            if chave_idempotencia:
                existente = conn.execute(
                    "SELECT codigo, area, data FROM reservas"
                    " WHERE chave_idempotencia = ? AND apartamento = ?",
                    (chave_idempotencia, apartamento),
                ).fetchone()
                if existente:
                    return dict(existente)
            codigo = _novo_codigo(conn)
            conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, chave_idempotencia)"
                " VALUES (?, ?, ?, ?, ?)",
                (codigo, apartamento, area_id, data, chave_idempotencia),
            )
    except sqlite3.IntegrityError as e:
        if "reservas.area" in str(e) or "ux_reserva_ativa" in str(e):
            raise AreaOcupada(area_id, data) from e
        raise
    return {"codigo": codigo, "area": area_id, "data": data}


def cancelar_reserva(apartamento: str, area_id: str, data: str) -> dict | None:
    """Cancela a reserva ativa do PRÓPRIO apartamento na área e data.

    O filtro por apartamento está no UPDATE: reserva de outro apartamento
    simplesmente não é encontrada.
    """
    with transacao() as conn:
        r = conn.execute(
            "SELECT codigo, area, data FROM reservas"
            " WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'",
            (apartamento, area_id, data),
        ).fetchone()
        if r is None:
            return None
        conn.execute(
            "UPDATE reservas SET status = 'cancelada', cancelada_em = datetime('now')"
            " WHERE codigo = ? AND apartamento = ?",
            (r["codigo"], apartamento),
        )
    return dict(r)


# --------------------------------------------------------------------------
# Visitantes
# --------------------------------------------------------------------------


def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def autorizar_visitante(
    apartamento: str, nome: str, data: str, chave_idempotencia: str | None
) -> dict:
    with transacao() as conn:
        if chave_idempotencia:
            existente = conn.execute(
                "SELECT nome, data FROM visitantes"
                " WHERE chave_idempotencia = ? AND apartamento = ?",
                (chave_idempotencia, apartamento),
            ).fetchone()
            if existente:
                return dict(existente)
        conn.execute(
            "INSERT INTO visitantes (apartamento, nome, data, chave_idempotencia)"
            " VALUES (?, ?, ?, ?)",
            (apartamento, nome, data, chave_idempotencia),
        )
    return {"nome": nome, "data": data}


# --------------------------------------------------------------------------
# Sessões (vínculo imutável sessão → apartamento)
# --------------------------------------------------------------------------


def apartamento_existe(numero: str) -> bool:
    with conectar() as conn:
        return (
            conn.execute("SELECT 1 FROM apartamentos WHERE numero = ?", (numero,)).fetchone()
            is not None
        )


def registrar_sessao(session_id: str, apartamento: str) -> None:
    with transacao() as conn:
        conn.execute(
            "INSERT INTO sessoes (session_id, apartamento) VALUES (?, ?)",
            (session_id, apartamento),
        )


def apartamento_da_sessao(session_id: str) -> str | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)
        ).fetchone()
    return r["apartamento"] if r else None
