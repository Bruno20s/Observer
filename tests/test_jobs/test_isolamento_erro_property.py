"""Teste property-based da Property 10 ("isolamento de erro entre ProdutoSite").

# Feature: rastreador-precos-mvp, Property 10: Para qualquer conjunto de
# ProdutoSite processados em um ciclo do Job_Monitor no qual um subconjunto
# ARBITRARIO de consultas (preco/reputacao) falha (o adapter levanta excecao,
# retorna sucesso=False, ou a consulta estoura o timeout), todos os ProdutoSite
# cujas consultas de PRECO tiveram sucesso sao processados normalmente (seu
# HistoricoPreco e persistido) e a falha de qualquer item NAO interrompe o
# processamento dos demais.

Este teste exercita `src.jobs.monitor.executar_ciclo` contra um banco SQLite em
memoria (`:memory:`), com adapters, relogio, executor de timeout e remetente de
notificacao TODOS injetados/mockados -- nunca ha rede nem tempo real.

Estrategia:

- Hypothesis gera uma lista (1..12) de "desfechos", cada um marcado como:
  - ``"ok"``    -> o adapter retorna um ``PrecoResult`` valido (deve persistir
                   exatamente um ``HistoricoPreco``);
  - ``"fail"``  -> o adapter retorna ``sucesso=False`` (nao persiste, sem crash);
  - ``"raise"`` -> ``buscar_preco`` LEVANTA uma excecao (isolada pelo try/except
                   do ciclo; nao persiste, sem crash);
  - ``"timeout"`` -> o ``executor_timeout`` injetado levanta ``TimeoutError``
                   para aquele item (abortado, isolado; nao persiste).
- Para cada desfecho inserimos uma linha ``produto_site`` cujo ``item_id``
  codifica o indice/desfecho. Um unico adapter fake por site inspeciona o
  ``item_id`` para decidir o comportamento, permitindo variar o desfecho entre
  muitas linhas mesmo havendo apenas dois sites (Mercado Livre e Amazon).
- Chamamos ``executar_ciclo`` com ``executor_timeout`` inline (que, para itens
  ``"timeout"``, levanta ``TimeoutError``) e um relogio fixo. O banco esta
  vazio de historico, entao TODO item esta "due" (R3.4/R11 nao interferem aqui).

Assercoes (isolamento -- R3.3/R6.6):

- ``executar_ciclo`` NAO propaga excecao (isolamento total).
- O numero de ``HistoricoPreco`` persistidos == numero de itens ``"ok"``.
- Cada item ``"ok"`` tem exatamente um registro de preco, com o valor mockado;
  itens ``"fail"``/``"raise"``/``"timeout"`` NAO tem nenhum registro de preco.

**Validates: Requirements 3.3, 6.6; Property 10**
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Mapping

from hypothesis import given, settings
from hypothesis import strategies as st

from src.adapters.base import BaseAdapter, PrecoResult, ReputacaoResult, Site
from src.config import Config, Credenciais, ParametrosOperacionais
from src.db import database
from src.jobs.monitor import executar_ciclo
from src.notificacao.email import ResultadoEnvio

# Desfechos possiveis por item. "ok" deve persistir; os demais nao.
_DESFECHOS = ("ok", "fail", "raise", "timeout")

# Relogio fixo (UTC-aware) injetado no ciclo -- deterministico, sem tempo real.
_AGORA_FIXO = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

# Preco "bom" retornado pelos itens "ok". Valor exato em Decimal (dominio monetario).
_PRECO_OK = Decimal("199.90")


def _config_ficticia() -> Config:
    """Config com credenciais ficticias (e-mail SMTP) e parametros default.

    As credenciais NAO tocam a rede: o remetente de notificacao e um fake. Os
    parametros default bastam pois o banco comeca sem historico -> todo item
    esta "due" independentemente do Intervalo_Minimo.
    """
    credenciais = Credenciais(
        ml_client_id="cid",
        ml_client_secret="secret",
        ml_redirect_uri="https://exemplo/callback",
        ml_access_token="access",
        ml_refresh_token="refresh",
        email_remetente="remetente@gmail.com",
        email_senha_app="senha-de-app-ficticia",
        email_destinatario="destino@example.com",
    )
    return Config(credenciais=credenciais, parametros=ParametrosOperacionais())


class _AdapterFake(BaseAdapter):
    """Adapter fake cujo comportamento e escolhido POR item_id.

    ``desfechos`` mapeia ``item_id -> desfecho``. ``buscar_preco`` consulta esse
    mapa para decidir entre retornar um preco valido ("ok"), retornar
    ``sucesso=False`` ("fail") ou LEVANTAR uma excecao ("raise"). O desfecho
    "timeout" e tratado FORA do adapter (pelo ``executor_timeout`` injetado),
    entao aqui ele se comporta como "ok" -- mas nunca chega a retornar porque a
    espera e abortada com ``TimeoutError`` antes.
    """

    def __init__(self, site: Site, desfechos: Mapping[str, str]) -> None:
        self.site = site
        self._desfechos = desfechos

    @staticmethod
    def parse_url(url: str):  # nao usado neste teste
        return None

    def buscar_preco(self, produto_site_id: str) -> PrecoResult:
        desfecho = self._desfechos.get(produto_site_id, "ok")
        if desfecho == "raise":
            raise RuntimeError(f"falha simulada de preco em {produto_site_id}")
        if desfecho == "fail":
            return PrecoResult(sucesso=False, erro="indisponivel (simulado)")
        # "ok" (e "timeout", que nunca chega aqui de fato): preco valido.
        return PrecoResult(
            sucesso=True,
            preco=_PRECO_OK,
            moeda="BRL",
            nome_produto=f"Produto {produto_site_id}",
        )

    def buscar_reputacao_vendedor(self, produto_site_id: str) -> ReputacaoResult:
        # Reputacao sempre disponivel e valida; nao e o foco desta propriedade.
        # (Falha de reputacao ja e isolada por R6.6; aqui garantimos que a
        # persistencia do PRECO dos itens "ok" acontece.)
        return ReputacaoResult(
            sucesso=True,
            amz_nota_media=4.5,
            amz_num_avaliacoes=100,
            amz_vendido_por_amazon=True,
            ml_level_id="5_green",
            ml_selo_mercadolider="platinum",
            ml_percentual_reclamacoes=1.0,
        )


def _remetente_fake(mensagem, remetente, senha_app, destinatario, *, produto_site_id=None) -> ResultadoEnvio:
    """Remetente de notificacao fake: nunca toca a rede, sempre "envia" com sucesso."""
    return ResultadoEnvio(sucesso=True, tentativas=1)


def _contar_historico_preco(conn: sqlite3.Connection, produto_site_id: int) -> int:
    """Conta os registros de HistoricoPreco de um ProdutoSite."""
    return conn.execute(
        "SELECT COUNT(*) FROM historico_preco WHERE produto_site_id = ?",
        (produto_site_id,),
    ).fetchone()[0]


@settings(max_examples=100, deadline=None)
@given(
    desfechos=st.lists(
        st.sampled_from(_DESFECHOS),
        min_size=1,
        max_size=12,
    ),
    # Distribui os itens entre os dois sites com adapter registrado, exercitando
    # o roteamento por site sem alterar a semantica da propriedade.
    sites=st.lists(
        st.sampled_from([Site.MERCADO_LIVRE, Site.AMAZON]),
        min_size=1,
        max_size=12,
    ),
)
def test_property_10_isolamento_erro_entre_produto_site(
    desfechos: list[str], sites: list[Site]
) -> None:
    """Falhas arbitrarias nao interrompem os itens bem-sucedidos (R3.3/R6.6).

    **Validates: Requirements 3.3, 6.6; Property 10**
    """
    conn: sqlite3.Connection = database.inicializar_banco(":memory:")
    try:
        # ``item_id`` codifica o indice para servir de chave no mapa de desfechos.
        mapa_desfecho: dict[str, str] = {}
        ids_por_item: list[tuple[int, str, str]] = []  # (ps_id, item_id, desfecho)

        for i, desfecho in enumerate(desfechos):
            site = sites[i % len(sites)]
            item_id = f"ITEM{i:03d}"
            mapa_desfecho[item_id] = desfecho
            cur = conn.execute(
                """
                INSERT INTO produto_site
                    (produto_id, site, item_id, url_original, criado_em)
                VALUES (NULL, ?, ?, ?, ?)
                """,
                (
                    site.value,
                    item_id,
                    f"https://exemplo/{item_id}",
                    _AGORA_FIXO.isoformat(),
                ),
            )
            ids_por_item.append((cur.lastrowid, item_id, desfecho))
        conn.commit()

        # Um adapter fake por site; ambos consultam o mesmo mapa de desfechos.
        adapters: dict[Site, BaseAdapter] = {
            Site.MERCADO_LIVRE: _AdapterFake(Site.MERCADO_LIVRE, mapa_desfecho),
            Site.AMAZON: _AdapterFake(Site.AMAZON, mapa_desfecho),
        }

        # Executor de timeout inline: chama func() diretamente (sem threads),
        # exceto para itens "timeout", cujo item_id aparece na mensagem do
        # closure? Nao temos o item_id aqui, entao decidimos consultando quais
        # item_ids sao "timeout" via um wrapper que roda a func e, se ela
        # retornar um preco de um item "timeout", converte em TimeoutError.
        #
        # Mais simples e robusto: o executor consulta o mapa por meio da
        # PrecoResult.nome_produto ("Produto <item_id>") apenas para itens "ok".
        # Porem itens "fail"/"raise" nao produzem nome. Para manter o executor
        # agnostico, adotamos a abordagem recomendada no design: o executor roda
        # func() e, para os desfechos "timeout", a propria func sinaliza via um
        # marcador. Aqui optamos por deixar o adapter agir normalmente e o
        # executor decidir com base no conjunto de item_ids "timeout".
        item_ids_timeout = {
            iid for iid, d in mapa_desfecho.items() if d == "timeout"
        }

        def executor_inline(
            func: Callable[[], PrecoResult], timeout: float
        ) -> PrecoResult:
            resultado = None
            erro: Exception | None = None
            try:
                resultado = func()
            except Exception as exc:  # propaga apos verificar timeout? nao.
                erro = exc
            # Se a func correspondeu a um item "timeout", aborta com TimeoutError,
            # simulando o estouro do limite de tempo (R3.6) ANTES de qualquer
            # persistencia. Identificamos o item pelo nome_produto retornado.
            if resultado is not None and resultado.nome_produto:
                nome_item = resultado.nome_produto.replace("Produto ", "", 1)
                if nome_item in item_ids_timeout:
                    raise TimeoutError(f"timeout simulado em {nome_item}")
            if erro is not None:
                raise erro
            return resultado  # type: ignore[return-value]

        # --- Execucao: NAO deve levantar (isolamento total) ------------------
        executar_ciclo(
            conn,
            config=_config_ficticia(),
            adapters=adapters,
            agora=lambda: _AGORA_FIXO,
            executor_timeout=executor_inline,
            remetente=_remetente_fake,
        )

        # --- Verificacao do isolamento --------------------------------------
        total_ok = sum(1 for _, _, d in ids_por_item if d == "ok")
        total_persistido = sum(
            _contar_historico_preco(conn, ps_id) for ps_id, _, _ in ids_por_item
        )
        # A contagem total de HistoricoPreco == numero de itens "ok".
        assert total_persistido == total_ok

        for ps_id, item_id, desfecho in ids_por_item:
            n = _contar_historico_preco(conn, ps_id)
            if desfecho == "ok":
                # Item bem-sucedido: exatamente um registro, com o preco mockado.
                assert n == 1, (
                    f"item {item_id} (ok) deveria ter 1 HistoricoPreco, tem {n}"
                )
                ultimo = database.obter_ultimo_preco(conn, ps_id)
                assert ultimo is not None
                assert ultimo.preco == _PRECO_OK
            else:
                # fail/raise/timeout: nenhum registro de preco (nao interrompe os demais).
                assert n == 0, (
                    f"item {item_id} ({desfecho}) nao deveria persistir preco, tem {n}"
                )
    finally:
        conn.close()
