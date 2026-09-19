"""Testes (exemplo, mockados) do envio por e-mail SMTP (task 10.3 -> migracao e-mail).

Exercitam :func:`src.notificacao.email.enviar_notificacao` sem rede: o
``smtplib.SMTP`` e mockado (via ``unittest.mock``), portanto NENHUM e-mail real
e enviado. O ``sleep`` do backoff e substituido por um no-op para nunca dormir
de verdade. Verifica o caminho de sucesso (envio na primeira tentativa), o
caminho de falha (esgota ``MAX_TENTATIVAS_ENVIO``, ``sucesso=False``, nunca
propaga excecao) e a recuperacao apos uma falha transitoria.
"""

from __future__ import annotations

from unittest import mock

import smtplib

from src.notificacao.email import (
    MAX_TENTATIVAS_ENVIO,
    SMTP_HOST,
    SMTP_PORT,
    ResultadoEnvio,
    enviar_notificacao,
)

_REMETENTE = "remetente@gmail.com"
_SENHA = "senha-de-app-ficticia"
_DESTINATARIO = "destino@example.com"


def _sleep_noop(_segundos: float) -> None:
    """Substitui ``time.sleep`` para nunca dormir de verdade nos testes."""
    return None


def _fazer_smtp_fake():
    """Cria um mock de instancia SMTP que suporta o protocolo de context manager.

    Retorna (fabrica, instancia): ``fabrica`` e o objeto a ser usado como
    ``smtplib.SMTP`` (chamado com host/porta/timeout) e ``instancia`` e o mock
    da conexao, no qual asseguramos que ``starttls``/``login``/``send_message``
    sao chamados. ``__enter__`` retorna a propria instancia (with-statement).
    """
    instancia = mock.MagicMock(name="smtp_conn")
    instancia.__enter__.return_value = instancia
    instancia.__exit__.return_value = False
    fabrica = mock.MagicMock(name="SMTP_factory", return_value=instancia)
    return fabrica, instancia


# ===========================================================================
# Caminho de sucesso
# ===========================================================================
def test_enviar_notificacao_sucesso_primeira_tentativa() -> None:
    """Envio SMTP bem-sucedido na 1a tentativa -> sucesso=True, tentativas=1."""
    fabrica, conn = _fazer_smtp_fake()

    with mock.patch("src.notificacao.email.smtplib.SMTP", fabrica):
        resultado = enviar_notificacao(
            mensagem="Preco mudou: Produto X",
            remetente=_REMETENTE,
            senha_app=_SENHA,
            destinatario=_DESTINATARIO,
            produto_site_id=7,
            sleep=_sleep_noop,
        )

    assert isinstance(resultado, ResultadoEnvio)
    assert resultado.sucesso is True
    assert resultado.tentativas == 1
    assert resultado.erro is None

    # Conectou no Gmail (host/porta corretos) e seguiu o handshake STARTTLS+login.
    fabrica.assert_called_once()
    args, kwargs = fabrica.call_args
    assert args[0] == SMTP_HOST
    assert args[1] == SMTP_PORT
    conn.starttls.assert_called_once()
    conn.login.assert_called_once_with(_REMETENTE, _SENHA)
    conn.send_message.assert_called_once()


def test_enviar_notificacao_sucesso_apos_falha_transitoria() -> None:
    """Falha na 1a tentativa e sucesso na 2a -> sucesso=True, tentativas=2."""
    fabrica, conn = _fazer_smtp_fake()
    # 1a chamada de send_message levanta; 2a retorna normalmente.
    conn.send_message.side_effect = [smtplib.SMTPException("falha transitoria"), None]

    with mock.patch("src.notificacao.email.smtplib.SMTP", fabrica):
        resultado = enviar_notificacao(
            mensagem="Preco mudou",
            remetente=_REMETENTE,
            senha_app=_SENHA,
            destinatario=_DESTINATARIO,
            sleep=_sleep_noop,
        )

    assert resultado.sucesso is True
    assert resultado.tentativas == 2


# ===========================================================================
# Caminho de falha
# ===========================================================================
def test_enviar_notificacao_falha_esgota_tentativas() -> None:
    """SMTP sempre falha -> sucesso=False, esgota MAX_TENTATIVAS, nunca propaga."""
    fabrica, conn = _fazer_smtp_fake()
    conn.send_message.side_effect = smtplib.SMTPException("indisponivel")

    with mock.patch("src.notificacao.email.smtplib.SMTP", fabrica):
        resultado = enviar_notificacao(
            mensagem="Preco mudou",
            remetente=_REMETENTE,
            senha_app=_SENHA,
            destinatario=_DESTINATARIO,
            produto_site_id=99,
            sleep=_sleep_noop,
        )

    assert resultado.sucesso is False
    assert resultado.tentativas == MAX_TENTATIVAS_ENVIO
    assert resultado.erro is not None
    # Uma conexao por tentativa.
    assert fabrica.call_count == MAX_TENTATIVAS_ENVIO


def test_enviar_notificacao_falha_de_conexao_e_isolada() -> None:
    """Erro ao abrir a conexao SMTP tambem e isolado -> sucesso=False, sem propagar."""
    fabrica = mock.MagicMock(
        name="SMTP_factory", side_effect=OSError("conexao recusada")
    )

    with mock.patch("src.notificacao.email.smtplib.SMTP", fabrica):
        resultado = enviar_notificacao(
            mensagem="Preco mudou",
            remetente=_REMETENTE,
            senha_app=_SENHA,
            destinatario=_DESTINATARIO,
            sleep=_sleep_noop,
        )

    assert resultado.sucesso is False
    assert resultado.tentativas == MAX_TENTATIVAS_ENVIO


def test_enviar_notificacao_falha_de_login_e_isolada() -> None:
    """Falha de autenticacao (senha de app invalida) e isolada, nunca propaga."""
    fabrica, conn = _fazer_smtp_fake()
    conn.login.side_effect = smtplib.SMTPAuthenticationError(535, b"invalid")

    with mock.patch("src.notificacao.email.smtplib.SMTP", fabrica):
        resultado = enviar_notificacao(
            mensagem="Preco mudou",
            remetente=_REMETENTE,
            senha_app=_SENHA,
            destinatario=_DESTINATARIO,
            sleep=_sleep_noop,
        )

    assert resultado.sucesso is False
    assert resultado.erro is not None
