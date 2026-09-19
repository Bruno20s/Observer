"""Orquestracao do Job_Monitor (`src/jobs/monitor.py`).

Este modulo concentra a logica de monitoramento periodico descrita no design
("Fluxo do Job_Monitor"). A **decisao de notificar** vive exclusivamente aqui
(regra arquitetural de separacao, R3/R4): os adapters retornam apenas dados
brutos e nao conhecem historico, comparacao ou notificacao.

Por ora este modulo expoe a deteccao de mudanca de preco:

- :func:`detectar_mudanca` -- compara um novo preco contra o ultimo preco
  registrado usando **comparacao Decimal exata, sem tolerancia** (R4.2), e
  classifica o resultado (sem mudanca / subida / queda). Funcao **pura**:
  matematica exata em :class:`~decimal.Decimal`, sem I/O, deterministica.

A orquestracao completa do ciclo (``executar_ciclo``) e implementada
separadamente (task 11.2) e consome :func:`detectar_mudanca`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# Rotulos de direcao da mudanca de preco.
#
# Os valores textuais ("subida"/"queda") sao intencionalmente alinhados aos
# rotulos usados pelo notificador (``src/notificacao/email.py``:
# ``ROTULO_SUBIDA`` / ``ROTULO_QUEDA``), garantindo consistencia entre a
# deteccao e a mensagem enviada. Definimos um enum proprio no monitor (em vez
# de importar de ``email``) para manter ``detectar_mudanca`` puro e
# desacoplado do modulo de notificacao.
# ---------------------------------------------------------------------------


class DirecaoMudanca(str, Enum):
    """Direcao de uma mudanca de preco detectada.

    - :attr:`SUBIDA` -- o novo preco e maior que o anterior (``novo > anterior``).
    - :attr:`QUEDA` -- o novo preco e menor que o anterior (``novo < anterior``).

    Os valores textuais coincidem com ``ROTULO_SUBIDA``/``ROTULO_QUEDA`` do
    notificador. Quando nao ha mudanca, o resultado usa ``direcao = None``.
    """

    SUBIDA = "subida"
    QUEDA = "queda"


@dataclass(frozen=True)
class ResultadoMudanca:
    """Resultado da deteccao de mudanca de preco (R4).

    ``houve_mudanca`` e ``True`` sse existia um preco anterior registrado e o
    novo preco difere dele na comparacao Decimal exata (R4.2, R4.3). Quando
    ``houve_mudanca`` e ``True``, ``direcao`` indica :attr:`DirecaoMudanca.SUBIDA`
    ou :attr:`DirecaoMudanca.QUEDA`; caso contrario, ``direcao`` e ``None``
    (sem preco anterior, R4.5, ou preco igual, R4.4).
    """

    houve_mudanca: bool
    direcao: Optional[DirecaoMudanca] = None


#: Resultado reutilizavel para "nenhuma mudanca" (imutavel/singleton logico).
SEM_MUDANCA: ResultadoMudanca = ResultadoMudanca(houve_mudanca=False, direcao=None)


def detectar_mudanca(
    anterior: Optional[Decimal],
    novo: Decimal,
) -> ResultadoMudanca:
    """Detecta se ``novo`` representa uma mudanca em relacao a ``anterior`` (R4).

    Regras (comparacao monetaria exata, sem tolerancia de arredondamento -- R4.2):

    - ``anterior is None`` (nao ha preco anterior registrado): **sem mudanca**
      (primeira observacao; R4.5) -> ``houve_mudanca=False``, ``direcao=None``.
    - ``novo == anterior`` (Decimal exato): **sem mudanca** (R4.4) ->
      ``houve_mudanca=False``, ``direcao=None``.
    - ``novo > anterior``: **mudanca** classificada como
      :attr:`DirecaoMudanca.SUBIDA` (R4.3).
    - ``novo < anterior``: **mudanca** classificada como
      :attr:`DirecaoMudanca.QUEDA` (R4.3).

    A comparacao usa a igualdade/ordenacao nativa de :class:`~decimal.Decimal`,
    que compara **valores** numericos: ``Decimal("10.00") == Decimal("10.000")``
    e, portanto, nao ha mudanca entre eles; ja ``Decimal("10.01")`` difere de
    ``Decimal("10.00")`` e caracteriza mudanca.

    Funcao **pura**: nao faz I/O, nao usa ``float`` e e deterministica. O
    chamador (``executar_ciclo``, task 11.2) extrai ``.preco`` do ultimo
    ``HistoricoPreco`` (ou ``None``) e passa o preco recem-obtido como ``novo``.

    Args:
        anterior: Ultimo preco registrado como :class:`~decimal.Decimal`, ou
            ``None`` quando nao ha registro anterior (primeira observacao).
        novo: Novo preco obtido, como :class:`~decimal.Decimal` (assume-se
            valido/nao-nulo; a validacao de preco invalido e feita antes, R4.6).

    Returns:
        :class:`ResultadoMudanca` indicando se houve mudanca e, em caso
        afirmativo, a :class:`DirecaoMudanca` (subida ou queda).
    """
    # Sem preco anterior: primeira observacao, nao ha o que comparar (R4.5).
    if anterior is None:
        return SEM_MUDANCA

    # Comparacao Decimal exata, sem tolerancia (R4.2).
    if novo == anterior:
        return SEM_MUDANCA  # Preco igual: sem mudanca, sem notificacao (R4.4).

    # Preco diferente: mudanca detectada; classifica a direcao (R4.3).
    if novo > anterior:
        return ResultadoMudanca(houve_mudanca=True, direcao=DirecaoMudanca.SUBIDA)
    return ResultadoMudanca(houve_mudanca=True, direcao=DirecaoMudanca.QUEDA)


# ===========================================================================
# Orquestracao do ciclo (`executar_ciclo`) -- task 11.2
#
# Implementa o "Fluxo do Job_Monitor" do design: itera os ProdutoSite,
# respeita o Intervalo_Minimo (R3.4), consulta o preco com timeout (R3.6),
# trata preco invalido preservando o historico (R4.6), persiste HistoricoPreco
# (R3.2), detecta mudanca (R4), busca/persiste reputacao + score (R6.3/R6.5),
# notifica apenas na mudanca (R5) e isola erros por item (R3.3/R5.6/R6.6).
#
# TODA dependencia externa e INJETAVEL para permitir testes sem rede/tempo
# real (tasks 11.3-11.6): mapa de adapters, relogio ``agora``, o remetente de
# notificacao e a configuracao/parametros. Ha defaults sensatos.
# ===========================================================================

import logging as _logging
import concurrent.futures as _futures
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from src.adapters.base import BaseAdapter, PrecoResult, ReputacaoResult, Site
from src.config import Config, ParametrosOperacionais
from src.confianca.score import INDISPONIVEL, calcular_score
from src.db import database
from src.db.models import HistoricoPreco, HistoricoReputacao, ProdutoSite
from src.notificacao.email import (
    PrecoSiteComparacao,
    ResultadoEnvio,
    enviar_notificacao,
    montar_mensagem,
)

logger = _logging.getLogger(__name__)

#: Assinatura do relogio injetavel: retorna o instante corrente (UTC-aware).
Relogio = Callable[[], datetime]

#: Assinatura do remetente de notificacao injetavel. Casa com a interface
#: publica de ``enviar_notificacao`` no que o Job usa (mensagem, telefone,
#: apikey, produto_site_id) e retorna um ``ResultadoEnvio``.
Remetente = Callable[..., ResultadoEnvio]

#: Assinatura do executor de timeout injetavel: executa ``func`` com um limite
#: de ``timeout`` segundos; deve levantar ``TimeoutError`` no estouro (R3.6).
ExecutorTimeout = Callable[[Callable[[], PrecoResult], float], PrecoResult]


def _agora_utc() -> datetime:
    """Relogio padrao: instante corrente em UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


def _parse_iso_utc(texto: str) -> datetime:
    """Converte um timestamp ISO-8601 (como gravado em ``coletado_em``) em
    ``datetime`` timezone-aware em UTC.

    Aceita valores com ou sem offset. Quando o texto e "naive" (sem tz),
    assume-se UTC (convencao do sistema: timestamps sao sempre gravados em UTC).
    """
    dt = datetime.fromisoformat(texto)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _chamar_com_timeout_thread(
    func: Callable[[], PrecoResult], timeout: float
) -> PrecoResult:
    """Executor de timeout padrao (R3.6): roda ``func`` numa thread separada e
    aborta a espera apos ``timeout`` segundos, levantando ``TimeoutError``.

    A thread pode continuar em background (nao ha como interromper I/O bloqueante
    de forma segura em Python), mas o ciclo nao fica preso: o resultado tardio e
    descartado e o Job segue para o proximo item. Injetavel para os testes
    substituirem por uma chamada inline/deterministica.
    """
    executor = _futures.ThreadPoolExecutor(max_workers=1)
    try:
        futuro = executor.submit(func)
        return futuro.result(timeout=timeout)
    finally:
        # Nao bloqueia o ciclo aguardando threads penduradas.
        executor.shutdown(wait=False)


def _due_para_consulta(
    conn,
    produto_site_id: int,
    agora: datetime,
    intervalo_minimo_segundos: float,
) -> bool:
    """Decide se um ProdutoSite deve ser consultado agora (Intervalo_Minimo -- R3.4).

    Retorna ``True`` (deve consultar) quando nao ha historico de preco (item
    novo) ou quando ``agora - t_last >= Intervalo_Minimo``. Caso contrario,
    ``False`` (pula neste ciclo). ``t_last`` e o ``coletado_em`` do
    ``HistoricoPreco`` mais recente (R4.1).
    """
    ultimo = database.obter_ultimo_preco(conn, produto_site_id)
    if ultimo is None:
        return True
    try:
        t_last = _parse_iso_utc(ultimo.coletado_em)
    except (ValueError, TypeError):
        # Timestamp ilegivel: por seguranca, trata como devido (nao trava o item).
        return True
    decorrido = (agora - t_last).total_seconds()
    return decorrido >= intervalo_minimo_segundos


def _precos_por_site_do_grupo(
    conn, produto_site: ProdutoSite
) -> list[PrecoSiteComparacao]:
    """Coleta o ultimo preco de cada ProdutoSite do mesmo Produto agrupado (R5.3).

    Retorna a comparacao entre sites somente quando o ``Produto`` agrupa >= 2
    entradas; caso contrario retorna lista vazia (single-site -> sem comparacao,
    R2.2). Usa o ultimo ``HistoricoPreco`` de cada entrada (R2.3).
    """
    if produto_site.produto_id is None:
        return []

    irmaos = [
        ps
        for ps in database.listar_produto_site(conn)
        if ps.produto_id == produto_site.produto_id
    ]
    if len(irmaos) < 2:
        return []

    comparacao: list[PrecoSiteComparacao] = []
    for ps in irmaos:
        ultimo = database.obter_ultimo_preco(conn, ps.id)
        # ``site`` e uma string aberta no banco; para sites futuros sem membro
        # de enum, ``montar_mensagem`` cai no ``str(site)`` (NOME_SITE.get).
        try:
            site_valor = Site(ps.site)
        except ValueError:
            site_valor = ps.site  # type: ignore[assignment]
        comparacao.append(
            PrecoSiteComparacao(
                site=site_valor,
                preco=None if ultimo is None else ultimo.preco,
                moeda="BRL" if ultimo is None else ultimo.moeda,
            )
        )
    return comparacao


def _processar_item(
    conn,
    produto_site: ProdutoSite,
    *,
    adapters: Mapping[Site, BaseAdapter],
    parametros: ParametrosOperacionais,
    credenciais,
    agora: datetime,
    executor_timeout: ExecutorTimeout,
    remetente: Remetente,
) -> None:
    """Processa um unico ProdutoSite conforme o fluxo do design.

    Encapsula os passos por item; o isolamento de erro (try/except) fica no
    chamador ``executar_ciclo`` para garantir que uma falha aqui nunca
    interrompa os demais itens (R3.3/R6.6).
    """
    ps_id = produto_site.id

    # --- 1. Intervalo_Minimo (R3.4) --------------------------------------
    if not _due_para_consulta(
        conn, ps_id, agora, parametros.intervalo_minimo_segundos
    ):
        logger.info(
            "ProdutoSite id=%s pulado: dentro do Intervalo_Minimo.", ps_id
        )
        return

    # --- Roteamento por site (R3.1) --------------------------------------
    try:
        site_enum = Site(produto_site.site)
    except ValueError:
        logger.warning(
            "ProdutoSite id=%s: site '%s' sem adapter registrado.",
            ps_id,
            produto_site.site,
        )
        return
    adapter = adapters.get(site_enum)
    if adapter is None:
        logger.warning(
            "ProdutoSite id=%s: nenhum adapter para o site %s.", ps_id, site_enum
        )
        return

    # --- 2. Consulta de preco com timeout (R3.6) -------------------------
    try:
        preco_result: PrecoResult = executor_timeout(
            lambda: adapter.buscar_preco(produto_site.item_id),
            parametros.timeout_consulta_segundos,
        )
    except TimeoutError:
        logger.warning(
            "ProdutoSite id=%s: consulta de preco excedeu o timeout de %ss.",
            ps_id,
            parametros.timeout_consulta_segundos,
        )
        return

    # --- 3. Preco invalido/indisponivel (R4.6) ---------------------------
    if not preco_result.sucesso or preco_result.preco is None:
        logger.info(
            "ProdutoSite id=%s: preco indisponivel/invalido (%s); historico "
            "preservado, sem comparacao/notificacao.",
            ps_id,
            preco_result.erro,
        )
        return

    novo_preco = preco_result.preco

    # --- 5. Deteccao de mudanca (R4) -------------------------------------
    # IMPORTANTE: obter o ultimo preco ANTES de inserir o novo, para comparar
    # contra o preco anterior de fato (nao contra o que acabamos de inserir).
    anterior_reg = database.obter_ultimo_preco(conn, ps_id)
    anterior = None if anterior_reg is None else anterior_reg.preco
    resultado = detectar_mudanca(anterior, novo_preco)

    # --- 4. Persistencia do preco (R3.2), append-only --------------------
    coletado_em = agora.isoformat()
    database.inserir_historico_preco(
        conn,
        HistoricoPreco(
            produto_site_id=ps_id,
            preco=novo_preco,
            moeda=preco_result.moeda,
            mudanca_detectada=resultado.houve_mudanca,
            coletado_em=coletado_em,
        ),
    )

    # --- 6/7. Reputacao + score (R6.3/R6.5) ------------------------------
    reputacao: ReputacaoResult | None = None
    score = None
    try:
        reputacao = adapter.buscar_reputacao_vendedor(produto_site.item_id)
        score = calcular_score(reputacao, site_enum)
        database.inserir_historico_reputacao(
            conn,
            HistoricoReputacao(
                produto_site_id=ps_id,
                coletado_em=coletado_em,
                ml_level_id=reputacao.ml_level_id,
                ml_selo=reputacao.ml_selo_mercadolider,
                ml_percentual_reclamacoes=reputacao.ml_percentual_reclamacoes,
                amz_nota_media=reputacao.amz_nota_media,
                amz_num_avaliacoes=reputacao.amz_num_avaliacoes,
                amz_vendido_por_amazon=reputacao.amz_vendido_por_amazon,
                score_confianca=str(score),
                sinais_indisponiveis=(
                    reputacao.sinais_indisponiveis or score == INDISPONIVEL
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 -- reputacao nao interrompe o ciclo (R6.6)
        logger.warning(
            "ProdutoSite id=%s: falha ao obter/persistir reputacao: %s",
            ps_id,
            exc,
        )

    # --- 8. Notificacao APENAS na mudanca (R5) ---------------------------
    if not resultado.houve_mudanca:
        return

    precos_por_site = _precos_por_site_do_grupo(conn, produto_site)
    mensagem = montar_mensagem(
        nome_produto=preco_result.nome_produto or produto_site.item_id,
        preco_anterior=anterior,  # nao-None: houve_mudanca => anterior existe
        preco_novo=novo_preco,
        url_original=produto_site.url_original,
        site=site_enum,
        reputacao=reputacao,
        score=score,
        precos_por_site=precos_por_site if precos_por_site else None,
        moeda=preco_result.moeda,
    )

    remetente_email = getattr(credenciais, "email_remetente", "")
    senha_app = getattr(credenciais, "email_senha_app", "")
    destinatario = getattr(credenciais, "email_destinatario", "")
    resultado_envio = remetente(
        mensagem,
        remetente_email,
        senha_app,
        destinatario,
        produto_site_id=ps_id,
    )
    if not resultado_envio.sucesso:
        # Falha de envio nao reverte historicos ja gravados (R5.6).
        logger.error(
            "ProdutoSite id=%s: notificacao nao enviada (%s); historicos "
            "preservados.",
            ps_id,
            resultado_envio.erro,
        )


def executar_ciclo(
    conn,
    *,
    config: Config | None = None,
    adapters: Mapping[Site, BaseAdapter] | None = None,
    agora: Relogio = _agora_utc,
    executor_timeout: ExecutorTimeout = _chamar_com_timeout_thread,
    remetente: Remetente = enviar_notificacao,
) -> None:
    """Executa um ciclo completo do Job_Monitor (R3, R4, R5, R6).

    Itera todos os ``ProdutoSite`` cadastrados e, para cada um (isolando erros
    por item -- R3.3/R6.6): respeita o Intervalo_Minimo (R3.4), consulta o preco
    com timeout (R3.6), trata preco invalido preservando o historico (R4.6),
    persiste ``HistoricoPreco`` (R3.2), detecta mudanca (R4), busca/persiste a
    reputacao com o score (R6.3/R6.5) e notifica **apenas** quando ha mudanca
    (R5), incluindo a comparacao entre sites quando o ``Produto`` agrupa >= 2
    entradas (R5.3).

    Todas as dependencias externas sao INJETAVEIS (defaults sensatos), o que
    permite testar sem rede/tempo real:

    Args:
        conn: Conexao SQLite aberta (ver ``database.conectar``/``inicializar_banco``).
        config: :class:`~src.config.Config` com credenciais (e-mail SMTP) e
            :class:`~src.config.ParametrosOperacionais` (Intervalo_Minimo,
            timeout). Se ``None``, usa parametros default e credenciais vazias
            (util em smoke/tests com remetente fake).
        adapters: Mapa ``{Site: BaseAdapter}`` para roteamento por site (R3.1).
            Se ``None``, nenhum site tem adapter (todos sao pulados). Nos testes,
            injete adapters fake que retornam ``PrecoResult``/``ReputacaoResult``
            conhecidos, sem tocar a rede.
        agora: Relogio injetavel ``() -> datetime`` (UTC-aware) usado para o
            Intervalo_Minimo e para ``coletado_em``. Default: ``datetime.now(UTC)``.
            Nos testes, injete um relogio fixo/controlado.
        executor_timeout: Seam de timeout ``(func, timeout) -> PrecoResult`` que
            levanta ``TimeoutError`` no estouro (R3.6). Default: wrapper baseado
            em thread. Nos testes, injete um que chame ``func()`` inline (ou que
            force ``TimeoutError``) para determinismo.
        remetente: Remetente de notificacao (default
            :func:`~src.notificacao.email.enviar_notificacao`). Nos testes,
            injete um fake que registre as chamadas e retorne ``ResultadoEnvio``.
    """
    if adapters is None:
        adapters = {}
    if config is not None:
        parametros = config.parametros
        credenciais = config.credenciais
    else:
        parametros = ParametrosOperacionais()
        credenciais = None

    instante = agora()

    for produto_site in database.listar_produto_site(conn):
        try:
            _processar_item(
                conn,
                produto_site,
                adapters=adapters,
                parametros=parametros,
                credenciais=credenciais,
                agora=instante,
                executor_timeout=executor_timeout,
                remetente=remetente,
            )
        except Exception as exc:  # noqa: BLE001 -- isolamento por item (R3.3/R6.6)
            logger.error(
                "ProdutoSite id=%s: erro isolado no ciclo, seguindo demais: %s",
                getattr(produto_site, "id", "?"),
                exc,
            )
