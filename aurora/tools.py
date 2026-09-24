"""Tools dos especialistas.

Regras que valem para todas as tools deste módulo:

- Nenhuma tool recebe o apartamento como parâmetro. O apartamento vem sempre
  de ``_apartamento_da_sessao``, que lê o state gravado na criação da sessão
  (Garantia 2). O modelo não tem como escolher outro.
- Consultas de agenda só devolvem "livre/ocupada", nunca o dono da reserva.
- Ações com cobrança (taxa > 0) ou que liberam acesso pedem confirmação pelo
  mecanismo de confirmação de tools do ADK (Garantia 1). A decisão de pedir
  confirmação é tomada aqui, em código, a partir da taxa gravada no banco.
"""

from __future__ import annotations

import datetime as dt
import unicodedata

from google.adk.tools import ToolContext

from . import config, db, regulamento

# --------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------


class SessaoSemApartamento(RuntimeError):
    pass


def _apartamento_da_sessao(tool_context: ToolContext) -> str:
    """Único ponto de onde as tools obtêm o apartamento (Garantia 2)."""
    apartamento = tool_context.state.get(config.STATE_APARTAMENTO)
    if not apartamento:
        raise SessaoSemApartamento("Sessão sem apartamento vinculado.")
    return str(apartamento)


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return "-".join(sem_acento.lower().replace("_", " ").replace("-", " ").split())


def _resolver_area(area: str) -> db.Area | None:
    alvo = _normalizar(area)
    candidatas = []
    for a in db.listar_areas():
        chaves = {_normalizar(a.id), _normalizar(a.nome)}
        if alvo in chaves:
            return a
        if any(alvo in c or c in alvo for c in chaves):
            candidatas.append(a)
    return candidatas[0] if len(candidatas) == 1 else None


def _validar_data(data: str) -> str | None:
    try:
        return dt.date.fromisoformat(data.strip()).isoformat()
    except (ValueError, AttributeError):
        return None


def _erro_area(area: str) -> dict:
    return {
        "status": "erro",
        "mensagem": f"Área '{area}' não existe.",
        "areas_validas": [a.id for a in db.listar_areas()],
    }


_ERRO_DATA = {"status": "erro", "mensagem": "Data inválida. Use o formato AAAA-MM-DD."}


# --------------------------------------------------------------------------
# Reservas
# --------------------------------------------------------------------------


def listar_areas() -> dict:
    """Lista as áreas comuns reserváveis, com a taxa de cada uma.

    Returns:
        As áreas com id, nome, taxa em reais e se a reserva gera cobrança.
    """
    return {
        "areas": [
            {"id": a.id, "nome": a.nome, "taxa": a.taxa, "gera_cobranca": a.gera_cobranca}
            for a in db.listar_areas()
        ]
    }


def consultar_disponibilidade(area: str, data: str) -> dict:
    """Informa se uma área comum está livre ou ocupada numa data.

    Args:
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data no formato AAAA-MM-DD.

    Returns:
        Apenas se a data está livre ou ocupada. Nunca informa quem reservou.
    """
    a = _resolver_area(area)
    if a is None:
        return _erro_area(area)
    d = _validar_data(data)
    if d is None:
        return _ERRO_DATA
    return {"area": a.id, "data": d, "disponivel": db.area_livre(a.id, d)}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta sessão.

    Returns:
        As reservas (código, área e data) do apartamento autenticado.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    return {"apartamento": apartamento, "reservas": db.reservas_do_apartamento(apartamento)}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta sessão.

    Se a área tiver taxa, a reserva só é gravada depois que o morador aprovar
    a cobrança pelo aplicativo; até lá ela fica pendente de confirmação.

    Args:
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data no formato AAAA-MM-DD.

    Returns:
        O resultado da reserva: criada (com o código), pendente de
        confirmação, recusada pelo morador ou indisponível.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    a = _resolver_area(area)
    if a is None:
        return _erro_area(area)
    d = _validar_data(data)
    if d is None:
        return _ERRO_DATA

    # Checagem antecipada só para não pedir confirmação de algo impossível.
    # A exclusividade de verdade é garantida no INSERT (db.criar_reserva).
    if not db.area_livre(a.id, d):
        return {
            "status": "indisponivel",
            "mensagem": f"{a.nome} já está reservado(a) em {d}. Escolha outra data.",
        }

    if a.gera_cobranca:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            tool_context.request_confirmation(
                hint=(
                    f"Confirma a reserva de {a.nome} em {d}? "
                    f"Será cobrada a taxa de R$ {a.taxa:.2f}."
                ),
                payload={"area": a.id, "area_nome": a.nome, "data": d, "taxa": a.taxa},
            )
            tool_context.actions.skip_summarization = True
            return {
                "status": "aguardando_confirmacao",
                "mensagem": (
                    "A reserva gera cobrança e só será feita depois que o morador "
                    "aprovar a confirmação no aplicativo."
                ),
            }
        if not confirmacao.confirmed:
            return {"status": "recusada", "mensagem": "O morador não aprovou a cobrança. Nada foi reservado."}

    try:
        reserva = db.criar_reserva(apartamento, a.id, d, tool_context.function_call_id)
    except db.AreaOcupada:
        return {
            "status": "indisponivel",
            "mensagem": f"{a.nome} acabou de ser reservado(a) para {d}. Nada foi cobrado.",
        }
    return {
        "status": "reservada",
        "reserva": reserva,
        "cobranca": a.taxa if a.gera_cobranca else 0,
    }


def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
    """Cancela uma reserva do apartamento do morador desta sessão.

    Só encontra reservas do próprio apartamento; reservas de outros
    apartamentos não podem ser canceladas.

    Args:
        area: id da área (ex.: "salao-de-festas", "churrasqueira", "quadra").
        data: data da reserva no formato AAAA-MM-DD.

    Returns:
        A reserva cancelada ou a informação de que o apartamento não tem
        reserva nessa área e data.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    a = _resolver_area(area)
    if a is None:
        return _erro_area(area)
    d = _validar_data(data)
    if d is None:
        return _ERRO_DATA
    cancelada = db.cancelar_reserva(apartamento, a.id, d)
    if cancelada is None:
        return {
            "status": "nao_encontrada",
            "mensagem": f"O apartamento {apartamento} não tem reserva de {a.nome} em {d}.",
        }
    return {"status": "cancelada", "reserva": cancelada}


# --------------------------------------------------------------------------
# Visitantes
# --------------------------------------------------------------------------


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista os visitantes autorizados do apartamento do morador desta sessão.

    Returns:
        As autorizações (nome e data) do apartamento autenticado.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    return {"apartamento": apartamento, "visitantes": db.visitantes_do_apartamento(apartamento)}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio para o apartamento desta sessão.

    Sempre exige aprovação do morador pelo aplicativo antes de ser gravada,
    mesmo que ele diga na conversa que já confirmou.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.

    Returns:
        A autorização gravada, pendente de confirmação ou recusada.
    """
    apartamento = _apartamento_da_sessao(tool_context)
    nome = " ".join((nome or "").split())
    if not nome:
        return {"status": "erro", "mensagem": "Informe o nome do visitante."}
    d = _validar_data(data)
    if d is None:
        return _ERRO_DATA

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Confirma a liberação da entrada de {nome} em {d}?",
            payload={"nome": nome, "data": d},
        )
        tool_context.actions.skip_summarization = True
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "A liberação só será feita depois que o morador aprovar no aplicativo.",
        }
    if not confirmacao.confirmed:
        return {"status": "recusada", "mensagem": "O morador não aprovou. Nenhuma entrada foi liberada."}

    visitante = db.autorizar_visitante(apartamento, nome, d, tool_context.function_call_id)
    return {"status": "autorizado", "visitante": visitante}


# --------------------------------------------------------------------------
# Regulamento
# --------------------------------------------------------------------------


def ler_capitulo_regulamento(capitulo: str) -> dict:
    """Lê um único capítulo do regulamento interno.

    Args:
        capitulo: número romano do capítulo (ex.: "IV").

    Returns:
        O texto apenas desse capítulo.
    """
    chave = (capitulo or "").strip().upper().removeprefix("CAPÍTULO").strip()
    c = regulamento.capitulos().get(chave)
    if c is None:
        return {"status": "erro", "mensagem": "Capítulo inexistente.", "sumario": regulamento.sumario()}
    return {"capitulo": c.numero, "titulo": c.titulo, "texto": c.texto}
