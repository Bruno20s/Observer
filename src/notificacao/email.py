"""Notificador por e-mail via SMTP/Gmail (`src/notificacao/email.py`).

Este modulo cobre a montagem da mensagem de notificacao de mudanca de preco
(R5.1-R5.3) e o envio efetivo por **e-mail (SMTP)** (R5.4). O canal anterior
(WhatsApp via CallMeBot) foi descartado no MVP por instabilidade pratica do
servico nao-oficial; a decisao esta registrada em ``tech.md`` (secao
"Notificacao - E-mail").

Funcoes puras (sem I/O), reaproveitadas do notificador anterior sem alteracao
de contrato ou de conteudo:

- :func:`calcular_percentual` -- calcula ``((novo - anterior) / anterior) x 100``
  arredondado a 2 casas, com rotulo textual "subida"/"queda"/"igual" (R5.1,
  Property 7).
- :func:`montar_mensagem` -- monta o texto da notificacao contendo nome,
  precos, percentual, link, comparacao entre sites (quando >= 2 ProdutoSite) e
  resumo de reputacao ou "reputacao indisponivel" (R5.1, R5.2, R5.3, Property 8).

Funcao de envio:

- :func:`enviar_notificacao` -- envia a mensagem por SMTP (Gmail, STARTTLS em
  ``smtp.gmail.com:587``) usando ``smtplib`` (biblioteca nativa). Mantem o mesmo
  contrato de conteudo do canal anterior: recebe a mensagem ja montada e o
  ``produto_site_id`` (para log de falha); apenas as credenciais mudaram de
  telefone/apikey para remetente/senha-de-app/destinatario. Em falha, loga
  localmente e NUNCA lanca nem escreve no banco (fallback local -- R5.4).

Todas as chaves de configuracao textual (rotulos, cabecalhos) vivem em
CONSTANTES de modulo para facilitar ajuste sem tocar na logica.
"""

from __future__ import annotations

import logging
import smtplib
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage
from typing import Optional, Sequence

from src.adapters.base import ReputacaoResult, Site
from src.confianca.score import INDISPONIVEL, ScoreResult, calcular_score

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rotulos de direcao da variacao (R5.1).
# ---------------------------------------------------------------------------
ROTULO_SUBIDA: str = "subida"
ROTULO_QUEDA: str = "queda"
ROTULO_IGUAL: str = "igual"

#: Quantizacao a 2 casas decimais para o percentual (R5.1).
_DUAS_CASAS = Decimal("0.01")

# ---------------------------------------------------------------------------
# Textos fixos da mensagem. Em portugues.
# ---------------------------------------------------------------------------
CABECALHO_MUDANCA: str = "Preço mudou"
LABEL_ANTERIOR: str = "Anterior"
LABEL_NOVO: str = "Novo"
LABEL_VARIACAO: str = "Variação"
LABEL_LINK: str = "Link"
CABECALHO_COMPARACAO: str = "Comparação entre sites"
CABECALHO_REPUTACAO: str = "Reputação do vendedor"
#: Indicacao explicita quando os sinais de reputacao faltam (R5.3).
REPUTACAO_INDISPONIVEL: str = "reputação indisponível"

#: Assunto do e-mail de notificacao de mudanca de preco.
ASSUNTO_EMAIL: str = "Rastreador de Preços: mudança de preço detectada"

#: Rotulos legiveis por site para a comparacao entre sites e o resumo.
NOME_SITE: dict[Site, str] = {
    Site.MERCADO_LIVRE: "Mercado Livre",
    Site.AMAZON: "Amazon",
}


@dataclass(frozen=True)
class VariacaoPercentual:
    """Resultado do calculo de variacao percentual.

    ``percentual`` e um :class:`~decimal.Decimal` arredondado a 2 casas
    decimais (positivo em subida, negativo em queda, zero em igualdade).
    ``rotulo`` e "subida"/"queda"/"igual". ``disponivel`` e ``False`` quando o
    preco anterior e ``0`` (divisao por zero evitada -- R5.1), caso em que
    ``percentual`` fica ``None``.
    """

    disponivel: bool
    percentual: Optional[Decimal]
    rotulo: str


@dataclass(frozen=True)
class PrecoSiteComparacao:
    """Preco atual de um :class:`ProdutoSite` para a comparacao entre sites.

    Usado por :func:`montar_mensagem` quando o ``Produto`` agrupa >= 2 sites
    (R5.2). ``preco`` e o ultimo ``HistoricoPreco`` daquele site; ``None`` quando
    indisponivel.
    """

    site: Site
    preco: Optional[Decimal]
    moeda: str = "BRL"


def calcular_percentual(
    anterior: Decimal,
    novo: Decimal,
) -> VariacaoPercentual:
    """Calcula a variacao percentual entre ``anterior`` e ``novo`` (R5.1).

    Formula: ``((novo - anterior) / anterior) x 100``, arredondada a 2 casas
    decimais (``ROUND_HALF_UP``). O rotulo e:

    - ``"subida"`` sse o percentual for positivo (``novo > anterior``);
    - ``"queda"`` sse o percentual for negativo (``novo < anterior``);
    - ``"igual"`` quando ``novo == anterior``.

    Guarda contra ``anterior == 0`` (divisao por zero): retorna
    :class:`VariacaoPercentual` com ``disponivel=False`` e ``percentual=None``,
    para que o chamador possa omitir o percentual em vez de falhar.

    Funcao pura e deterministica. ``anterior`` e ``novo`` sao :class:`Decimal`
    para preservar o valor monetario exato (mesma convencao de ``HistoricoPreco``).
    """
    anterior = Decimal(anterior)
    novo = Decimal(novo)

    # Guarda contra divisao por zero (R5.1): sem percentual definido.
    if anterior == 0:
        rotulo = ROTULO_IGUAL if novo == 0 else ROTULO_SUBIDA
        return VariacaoPercentual(disponivel=False, percentual=None, rotulo=rotulo)

    variacao = ((novo - anterior) / anterior) * Decimal(100)
    percentual = variacao.quantize(_DUAS_CASAS, rounding=ROUND_HALF_UP)

    if percentual > 0:
        rotulo = ROTULO_SUBIDA
    elif percentual < 0:
        rotulo = ROTULO_QUEDA
    else:
        rotulo = ROTULO_IGUAL

    return VariacaoPercentual(disponivel=True, percentual=percentual, rotulo=rotulo)


def _formatar_preco(preco: Optional[Decimal], moeda: str = "BRL") -> str:
    """Formata um preco monetario para exibicao (ex.: ``"R$ 150.00"``).

    ``None`` -> ``"indisponivel"``. Usa ``Decimal`` a 2 casas para preservar o
    valor exato sem introduzir erro de ponto flutuante.
    """
    if preco is None:
        return "indisponivel"
    valor = Decimal(preco).quantize(_DUAS_CASAS, rounding=ROUND_HALF_UP)
    simbolo = "R$" if moeda == "BRL" else moeda
    return f"{simbolo} {valor}"


def _formatar_variacao(variacao: VariacaoPercentual) -> str:
    """Formata a linha de variacao percentual com sinal e rotulo textual (R5.1)."""
    if not variacao.disponivel or variacao.percentual is None:
        # anterior == 0: percentual indefinido; ainda indicamos a direcao.
        return f"indisponivel ({variacao.rotulo})"

    percentual = variacao.percentual
    # Sinal explicito: "+" em subida, "-" ja vem no proprio Decimal negativo.
    sinal = "+" if percentual > 0 else ""
    return f"{sinal}{percentual}%   ({variacao.rotulo})"


def _resumo_reputacao(
    reputacao: Optional[ReputacaoResult],
    site: Site,
    score: Optional[ScoreResult] = None,
) -> str:
    """Monta o resumo textual da reputacao do vendedor (R5.3) ou a indicacao
    de indisponibilidade.

    Retorna :data:`REPUTACAO_INDISPONIVEL` quando:

    - ``reputacao`` e ``None``; ou
    - a consulta de reputacao falhou (``sucesso is False``); ou
    - os sinais estao marcados como indisponiveis; ou
    - o score e :data:`INDISPONIVEL`.

    Caso contrario, resume os sinais do site (nivel/selo/reclamacoes no ML;
    nota/avaliacoes/vendedor na Amazon) e inclui o score de confianca.
    """
    if reputacao is None or not reputacao.sucesso or reputacao.sinais_indisponiveis:
        return REPUTACAO_INDISPONIVEL

    # Calcula o score quando nao fornecido explicitamente.
    if score is None:
        score = calcular_score(reputacao, site)

    if score == INDISPONIVEL:
        return REPUTACAO_INDISPONIVEL

    linhas: list[str] = []
    if site == Site.MERCADO_LIVRE:
        if reputacao.ml_level_id is not None:
            linhas.append(f"Nivel: {reputacao.ml_level_id}")
        if reputacao.ml_selo_mercadolider is not None:
            linhas.append(f"Selo: {reputacao.ml_selo_mercadolider}")
        if reputacao.ml_percentual_reclamacoes is not None:
            linhas.append(
                f"Reclamacoes: {reputacao.ml_percentual_reclamacoes:.2f}%"
            )
    elif site == Site.AMAZON:
        if reputacao.amz_nota_media is not None:
            linhas.append(f"Nota: {reputacao.amz_nota_media:.1f}/5.0")
        if reputacao.amz_num_avaliacoes is not None:
            linhas.append(f"Avaliacoes: {reputacao.amz_num_avaliacoes}")
        if reputacao.amz_vendido_por_amazon is not None:
            vendedor = (
                "vendido e entregue pela Amazon"
                if reputacao.amz_vendido_por_amazon
                else "vendedor terceiro"
            )
            linhas.append(f"Vendedor: {vendedor}")

    if not linhas:
        return REPUTACAO_INDISPONIVEL

    linhas.append(f"Score de confianca: {score}/100")
    return "\n".join(linhas)


def montar_mensagem(
    nome_produto: str,
    preco_anterior: Decimal,
    preco_novo: Decimal,
    url_original: str,
    site: Site,
    reputacao: Optional[ReputacaoResult] = None,
    score: Optional[ScoreResult] = None,
    precos_por_site: Optional[Sequence[PrecoSiteComparacao]] = None,
    moeda: str = "BRL",
) -> str:
    """Monta o texto da notificacao de mudanca de preco (R5.1, R5.2, R5.3).

    A mensagem contem sempre (R5.1):

    - o nome do produto;
    - o preco anterior e o preco novo;
    - o percentual de variacao com rotulo subida/queda;
    - o link do anuncio.

    Quando ``precos_por_site`` contem 2 ou mais entradas (o ``Produto`` agrupa
    >= 2 ``ProdutoSite``), inclui uma secao de comparacao entre sites com o
    preco atual de cada site, identificando o site (R5.2).

    Sempre inclui uma secao de reputacao (R5.3): o resumo dos sinais do vendedor
    ou a indicacao :data:`REPUTACAO_INDISPONIVEL` quando os sinais faltam.

    Funcao pura: nao faz I/O e nao envia nada. O envio e responsabilidade de
    :func:`enviar_notificacao`.
    """
    variacao = calcular_percentual(preco_anterior, preco_novo)

    linhas: list[str] = [
        f"{CABECALHO_MUDANCA}: {nome_produto}",
        f"{LABEL_ANTERIOR}: {_formatar_preco(preco_anterior, moeda)}",
        f"{LABEL_NOVO}: {_formatar_preco(preco_novo, moeda)}",
        f"{LABEL_VARIACAO}: {_formatar_variacao(variacao)}",
        f"{LABEL_LINK}: {url_original}",
    ]

    # Comparacao entre sites: somente quando ha >= 2 ProdutoSite (R5.2).
    if precos_por_site is not None and len(precos_por_site) >= 2:
        linhas.append("")
        linhas.append(f"{CABECALHO_COMPARACAO}:")
        for entrada in precos_por_site:
            nome_site = NOME_SITE.get(entrada.site, str(entrada.site))
            linhas.append(
                f"- {nome_site}: {_formatar_preco(entrada.preco, entrada.moeda)}"
            )

    # Reputacao do vendedor (R5.3): sempre presente.
    linhas.append("")
    linhas.append(f"{CABECALHO_REPUTACAO}:")
    linhas.append(_resumo_reputacao(reputacao, site, score))

    return "\n".join(linhas)


# ===========================================================================
# Envio por e-mail via SMTP/Gmail (R5.4).
#
# Esta parte do modulo faz I/O de rede (SMTP) e e mantida SEPARADA das funcoes
# puras de montagem de mensagem acima. ``enviar_notificacao`` nunca lanca por
# falha de envio e NUNCA escreve no banco: apenas retorna um indicador de
# sucesso/falha e, em falha, loga com o identificador do ProdutoSite (fallback
# local). O Job_Monitor usa esse indicador para logar e continuar, sem reverter
# HistoricoPreco.
# ===========================================================================

#: Servidor SMTP do Gmail (STARTTLS na porta 587). Fixo no MVP.
SMTP_HOST: str = "smtp.gmail.com"
SMTP_PORT: int = 587

#: Timeout por tentativa de conexao/envio, em segundos.
TIMEOUT_ENVIO_SEGUNDOS: float = 15.0

#: Numero maximo de tentativas de envio.
MAX_TENTATIVAS_ENVIO: int = 3

#: Backoff base entre tentativas, em segundos. O atraso da n-esima falha e
#: ``BACKOFF_BASE_SEGUNDOS * 2 ** (tentativa - 1)`` (backoff exponencial).
#: Nao ha sleep apos a ultima tentativa.
BACKOFF_BASE_SEGUNDOS: float = 1.0


@dataclass(frozen=True)
class ResultadoEnvio:
    """Indicador claro de sucesso/falha do envio por e-mail (R5.4).

    ``sucesso`` e ``True`` sse alguma tentativa concluiu o envio SMTP sem erro.
    ``tentativas`` e o numero de tentativas realizadas (1..``MAX_TENTATIVAS_ENVIO``).
    ``erro`` descreve o motivo da falha quando ``sucesso`` e ``False``.

    Este resultado nunca reflete escrita em banco: ``enviar_notificacao`` nao
    faz nenhum acesso a dados; apenas o chamador decide o que logar/persistir.
    """

    sucesso: bool
    tentativas: int
    erro: Optional[str] = None


def _construir_email(
    mensagem: str, remetente: str, destinatario: str
) -> EmailMessage:
    """Monta o :class:`email.message.EmailMessage` de texto simples a enviar."""
    email_msg = EmailMessage()
    email_msg["Subject"] = ASSUNTO_EMAIL
    email_msg["From"] = remetente
    email_msg["To"] = destinatario
    email_msg.set_content(mensagem)
    return email_msg


def _enviar_via_smtp(
    email_msg: EmailMessage,
    remetente: str,
    senha_app: str,
    timeout: float,
) -> None:
    """Envia ``email_msg`` por SMTP/Gmail (STARTTLS). Pode lancar em falha.

    Isolado da logica de retry para ser facilmente mockavel nos testes
    (``smtplib.SMTP`` e mockado; nenhum e-mail real e enviado).
    """
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=timeout) as smtp:
        smtp.starttls()
        smtp.login(remetente, senha_app)
        smtp.send_message(email_msg)


def enviar_notificacao(
    mensagem: str,
    remetente: str,
    senha_app: str,
    destinatario: str,
    produto_site_id: Optional[object] = None,
    *,
    sleep=time.sleep,
    max_tentativas: int = MAX_TENTATIVAS_ENVIO,
    timeout: float = TIMEOUT_ENVIO_SEGUNDOS,
    backoff_base: float = BACKOFF_BASE_SEGUNDOS,
) -> ResultadoEnvio:
    """Envia ``mensagem`` por e-mail via SMTP/Gmail (R5.4).

    Monta um e-mail de texto simples (assunto :data:`ASSUNTO_EMAIL`) e o envia
    por ``smtp.gmail.com:587`` com STARTTLS, autenticando com uma Senha de App
    do Gmail. Em caso de erro/timeout, tenta novamente ate ``max_tentativas``
    (default 3), com backoff exponencial entre tentativas (sem sleep apos a
    ultima).

    Degradacao graciosa (R5.4): esta funcao **nunca** propaga excecao por falha
    de envio e **nunca** escreve no banco. Esgotadas as tentativas, registra o
    erro em log local incluindo o ``produto_site_id`` afetado (fallback local) e
    retorna um :class:`ResultadoEnvio` com ``sucesso=False``. O Job_Monitor usa
    o indicador para logar e seguir com os demais itens, sem reverter os
    ``HistoricoPreco`` ja persistidos.

    Contrato de conteudo identico ao notificador anterior: recebe a mensagem ja
    montada por :func:`montar_mensagem` e o ``produto_site_id`` para o log; o
    conteudo e o ponto de chamada no Job_Monitor nao mudaram. Apenas as
    credenciais mudaram de telefone/apikey (WhatsApp) para
    remetente/senha-de-app/destinatario (e-mail).

    Args:
        mensagem: Texto ja montado por :func:`montar_mensagem`.
        remetente: E-mail remetente (``EMAIL_REMETENTE``); tambem o login SMTP.
        senha_app: Senha de App do Gmail (``EMAIL_SENHA_APP``).
        destinatario: E-mail de destino (``EMAIL_DESTINATARIO``).
        produto_site_id: Identificador do ``ProdutoSite`` afetado, usado apenas
            para o log de falha (R5.4). Nao aparece no corpo do e-mail.
        sleep: Seam de espera injetavel, ``(segundos) -> None``. Default:
            ``time.sleep``. Injetar um no-op evita dormir de verdade nos testes.
        max_tentativas: Numero maximo de tentativas (default 3).
        timeout: Timeout por tentativa em segundos (default 15s).
        backoff_base: Base do backoff exponencial entre tentativas.

    Returns:
        :class:`ResultadoEnvio` indicando sucesso/falha e numero de tentativas.
    """
    email_msg = _construir_email(mensagem, remetente, destinatario)

    tentativas = max(1, int(max_tentativas))
    ultimo_erro: Optional[str] = None

    for tentativa in range(1, tentativas + 1):
        try:
            _enviar_via_smtp(email_msg, remetente, senha_app, timeout)
            return ResultadoEnvio(sucesso=True, tentativas=tentativa)
        except Exception as exc:  # noqa: BLE001 -- isolamento (R5.4): nunca propaga.
            ultimo_erro = f"{type(exc).__name__}: {exc}"

        # Backoff antes da proxima tentativa (nao apos a ultima).
        if tentativa < tentativas:
            sleep(backoff_base * (2 ** (tentativa - 1)))

    # Todas as tentativas falharam: fallback local (R5.4). Loga com o
    # identificador do ProdutoSite e retorna indicador de falha sem lancar.
    logger.error(
        "Falha ao enviar notificacao por e-mail apos %d tentativa(s) "
        "(produto_site_id=%s): %s. Fallback local: mensagem nao enviada; "
        "historicos preservados.",
        tentativas,
        produto_site_id,
        ultimo_erro,
    )
    return ResultadoEnvio(
        sucesso=False,
        tentativas=tentativas,
        erro=ultimo_erro,
    )
