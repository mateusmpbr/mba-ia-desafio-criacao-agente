"""Leitura do regulamento interno por capítulo.

O texto completo nunca é entregue a nenhum agente: o especialista de
regulamento recebe só o sumário (títulos) nas instruções e lê, via tool, o
capítulo que interessa à pergunta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from . import config

_CABECALHO = re.compile(r"^## Capítulo ([IVXLC]+): (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Capitulo:
    numero: str  # romano, ex.: "IV"
    titulo: str
    texto: str


@lru_cache(maxsize=1)
def capitulos() -> dict[str, Capitulo]:
    texto = (config.DIR_DADOS / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_CABECALHO.finditer(texto))
    resultado: dict[str, Capitulo] = {}
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        resultado[m.group(1)] = Capitulo(
            numero=m.group(1), titulo=m.group(2).strip(), texto=texto[m.start() : fim].strip()
        )
    return resultado


def sumario() -> str:
    return "\n".join(f"- Capítulo {c.numero}: {c.titulo}" for c in capitulos().values())
