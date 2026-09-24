"""Restaura reservas e visitantes ao estado de ``dados/``.

Uso:
    uv run aurora-restaurar                  # reservas e visitantes
    uv run aurora-restaurar --apagar-sessoes # também apaga as conversas
"""

from __future__ import annotations

import argparse

from . import config, db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apagar-sessoes",
        action="store_true",
        help="apaga também as sessões e eventos das conversas",
    )
    args = parser.parse_args()

    db.restaurar()
    print("Reservas e visitantes restaurados a partir de dados/.")

    if args.apagar_sessoes:
        for sufixo in ("", "-wal", "-shm"):
            config.DB_SESSOES.with_name(config.DB_SESSOES.name + sufixo).unlink(missing_ok=True)
        with db.transacao() as conn:
            conn.execute("DELETE FROM sessoes")
        print("Sessões apagadas.")


if __name__ == "__main__":
    main()
