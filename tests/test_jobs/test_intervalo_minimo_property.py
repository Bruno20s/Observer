"""Teste property-based da Property 11 ("Respeito ao Intervalo_Minimo").

# Feature: rastreador-precos-mvp, Property 11: Para qualquer ProdutoSite com um
# registro de preco mais recente em instante t_last e um instante corrente now,
# o Job_Monitor realiza a nova consulta SE E SOMENTE SE now - t_last >=
# Intervalo_Minimo. Itens SEM historico estao sempre "due" (sao consultados).
# Itens dentro do intervalo sao pulados (nenhum novo HistoricoPreco e gravado;
# o adapter nao produz um novo registro de preco).

Este teste exercita a Property 11 em DOIS niveis complementares, ambos sem rede
nem tempo real (banco SQLite em memoria, adapters/relogio/executor/remetente
injetados):

1. Comportamento observavel via ``src.jobs.monitor.executar_ciclo`` -- a
   verdade de ordem superior: um NOVO ``HistoricoPreco`` e gravado SSE o item
   esta "due". Semeamos um ``HistoricoPreco`` em ``coletado_em = agora - delta``
   e, com um adapter fake que retorna um preco valido (portanto, SE consultado,
   um novo registro SERA gravado), contamos os registros antes/depois. O delta e
   posicionado por Hypothesis em torno do limiar (abaixo e em/acima).

2. Biconditional cristalino via ``src.jobs.monitor._due_para_consulta`` -- para
   deltas e intervalos ARBITRARIOS (evitando o piso de 1h validado em
   ``ParametrosOperacionais.__post_init__``): ``_due_para_consulta`` retorna
   ``True`` SSE ``delta >= intervalo`` (ou nao ha historico).

**Validates: Requirements 3.4; Property 11**
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from hypothesis import given, settings
from hypothesis import strategies as st

from src.adapters.base import BaseAdapter, PrecoResult, ReputacaoResult, Site
from src.config import (
    Config,
    Credenciais,
    ParametrosOperacionais,
    MINIMO_INTERVALO_MINIMO_SEGUNDOS,
)
from src.db import database
from src.db.models import HistoricoPreco
from src.jobs.monitor import _due_para_consulta, executar_ciclo
from src.notificacao.email import ResultadoEnvio

# Relogio fixo (UTC-aware) injetado no ciclo -- deterministico, sem tempo real.
_AGORA_FIXO = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

# Preco semeado como "ultimo" (t_last) e preco retornado pelo adapter. Sao
# DIFERENTES para que, quando "due", o adapter produza inequivocamente um novo
# registro (na pratica, a persistencia ocorre para QUALQUER preco valido,
# havendo mudanca ou nao -- o que importa e a contagem de linhas).
_PRECO_SEMEADO = Decimal("100.00")
_PRECO_ADAPTER = Decimal("199.90")

# Intervalo valido usado no teste de nivel-ciclo: exatamente o piso de 1h, para
# manter os deltas gerados pequenos e rapidos de amostrar em torno do limiar.
_INTERVALO_CICLO = MINIMO_INTERVALO_MINIMO_SEGUNDOS  # 3600s


def _config_ficticia(intervalo_minimo_segundos: int) -> Config:
    """Config com credenciais ficticias (e-mail SMTP) e Intervalo_Minimo dado.

    As credenciais NAO tocam a rede: o remetente e um fake.
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
    return Config(
        credenciais=credenciais,
        parametros=ParametrosOperacionais(
            intervalo_minimo_segundos=intervalo_minimo_segundos
        ),
    )


class _AdapterFake(BaseAdapter):
    """Adapter fake que sempre retorna um preco valido (sem rede).

    Se ``buscar_preco`` for chamado (item "due"), um novo ``HistoricoPreco``
    sera gravado pelo ciclo -- e assim detectamos "consultado vs pulado" pela
    variacao na contagem de registros.
    """

    def __init__(self, site: Site) -> None:
        self.site = site

    @staticmethod
    def parse_url(url: str):  # nao usado neste teste
        return None

    def buscar_preco(self, produto_site_id: str) -> PrecoResult:
        return PrecoResult(
            sucesso=True,
            preco=_PRECO_ADAPTER,
            moeda="BRL",
            nome_produto=f"Produto {produto_site_id}",
        )

    def buscar_reputacao_vendedor(self, produto_site_id: str) -> ReputacaoResult:
        return ReputacaoResult(
            sucesso=True,
            amz_nota_media=4.5,
            amz_num_avaliacoes=100,
            amz_vendido_por_amazon=True,
            ml_level_id="5_green",
            ml_selo_mercadolider="platinum",
            ml_percentual_reclamacoes=1.0,
        )


def _remetente_fake(
    mensagem, remetente, senha_app, destinatario, *, produto_site_id=None
) -> ResultadoEnvio:
    """Remetente fake: nunca toca a rede, sempre 'envia' com sucesso."""
    return ResultadoEnvio(sucesso=True, tentativas=1)


def _executor_inline(func: Callable[[], PrecoResult], timeout: float) -> PrecoResult:
    """Executor de timeout inline: chama ``func()`` direto (sem threads)."""
    return func()


def _contar_historico_preco(conn: sqlite3.Connection, produto_site_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM historico_preco WHERE produto_site_id = ?",
        (produto_site_id,),
    ).fetchone()[0]


def _inserir_produto_site(conn: sqlite3.Connection, item_id: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO produto_site (produto_id, site, item_id, url_original, criado_em)
        VALUES (NULL, ?, ?, ?, ?)
        """,
        (
            Site.AMAZON.value,
            item_id,
            f"https://exemplo/{item_id}",
            _AGORA_FIXO.isoformat(),
        ),
    )
    conn.commit()
    return cur.lastrowid


# ===========================================================================
# Nivel 1 -- comportamento observavel via executar_ciclo:
# um NOVO HistoricoPreco e gravado SSE o item esta "due".
# ===========================================================================


@settings(max_examples=100, deadline=None)
@given(
    # delta_segundos varia em torno do limiar de 1h (3600s): abaixo (pula) e
    # em/acima (consulta). A faixa vai de 0 ate 2h para cobrir ambos os lados.
    delta_segundos=st.integers(min_value=0, max_value=2 * 3600),
)
def test_property_11_intervalo_minimo_via_ciclo(delta_segundos: int) -> None:
    """Ciclo grava um novo HistoricoPreco SSE now - t_last >= Intervalo_Minimo.

    **Validates: Requirements 3.4; Property 11**
    """
    conn = database.inicializar_banco(":memory:")
    try:
        ps_id = _inserir_produto_site(conn, "ITEM_SEED")

        # Semeia o "ultimo" HistoricoPreco em t_last = agora - delta.
        t_last = _AGORA_FIXO - timedelta(seconds=delta_segundos)
        database.inserir_historico_preco(
            conn,
            HistoricoPreco(
                produto_site_id=ps_id,
                preco=_PRECO_SEMEADO,
                moeda="BRL",
                mudanca_detectada=False,
                coletado_em=t_last.isoformat(),
            ),
        )
        conn.commit()

        antes = _contar_historico_preco(conn, ps_id)
        assert antes == 1  # somente o registro semeado

        executar_ciclo(
            conn,
            config=_config_ficticia(_INTERVALO_CICLO),
            adapters={Site.AMAZON: _AdapterFake(Site.AMAZON)},
            agora=lambda: _AGORA_FIXO,
            executor_timeout=_executor_inline,
            remetente=_remetente_fake,
        )

        depois = _contar_historico_preco(conn, ps_id)
        due_esperado = delta_segundos >= _INTERVALO_CICLO

        if due_esperado:
            # Consultado: exatamente um novo registro gravado (com o preco do adapter).
            assert depois == antes + 1, (
                f"delta={delta_segundos}s >= {_INTERVALO_CICLO}s deveria consultar "
                f"(gravar 1 novo registro); antes={antes} depois={depois}"
            )
            ultimo = database.obter_ultimo_preco(conn, ps_id)
            assert ultimo is not None and ultimo.preco == _PRECO_ADAPTER
        else:
            # Pulado: nenhum novo registro; historico preservado intacto.
            assert depois == antes, (
                f"delta={delta_segundos}s < {_INTERVALO_CICLO}s deveria PULAR "
                f"(nenhum novo registro); antes={antes} depois={depois}"
            )
            ultimo = database.obter_ultimo_preco(conn, ps_id)
            assert ultimo is not None and ultimo.preco == _PRECO_SEMEADO
    finally:
        conn.close()


def test_property_11_item_sem_historico_sempre_consultado() -> None:
    """Item SEM historico esta sempre 'due' -> o ciclo grava um HistoricoPreco.

    **Validates: Requirements 3.4; Property 11**
    """
    conn = database.inicializar_banco(":memory:")
    try:
        ps_id = _inserir_produto_site(conn, "ITEM_NOVO")
        assert _contar_historico_preco(conn, ps_id) == 0

        executar_ciclo(
            conn,
            config=_config_ficticia(_INTERVALO_CICLO),
            adapters={Site.AMAZON: _AdapterFake(Site.AMAZON)},
            agora=lambda: _AGORA_FIXO,
            executor_timeout=_executor_inline,
            remetente=_remetente_fake,
        )

        # Sem historico anterior => sempre due => exatamente um registro gravado.
        assert _contar_historico_preco(conn, ps_id) == 1
        ultimo = database.obter_ultimo_preco(conn, ps_id)
        assert ultimo is not None and ultimo.preco == _PRECO_ADAPTER
    finally:
        conn.close()


# ===========================================================================
# Nivel 2 -- biconditional cristalino em _due_para_consulta com intervalos e
# deltas ARBITRARIOS (evita o piso de 1h de ParametrosOperacionais).
# ===========================================================================


@settings(max_examples=100, deadline=None)
@given(
    delta_segundos=st.integers(min_value=0, max_value=10 * 24 * 3600),
    intervalo_segundos=st.integers(min_value=1, max_value=10 * 24 * 3600),
)
def test_property_11_due_para_consulta_biconditional(
    delta_segundos: int, intervalo_segundos: int
) -> None:
    """_due_para_consulta == (delta >= intervalo) para qualquer historico existente.

    **Validates: Requirements 3.4; Property 11**
    """
    conn = database.inicializar_banco(":memory:")
    try:
        ps_id = _inserir_produto_site(conn, "ITEM_BICOND")

        # Sem historico: sempre due, independentemente do intervalo.
        assert _due_para_consulta(conn, ps_id, _AGORA_FIXO, intervalo_segundos) is True

        # Com historico em t_last = agora - delta.
        t_last = _AGORA_FIXO - timedelta(seconds=delta_segundos)
        database.inserir_historico_preco(
            conn,
            HistoricoPreco(
                produto_site_id=ps_id,
                preco=_PRECO_SEMEADO,
                moeda="BRL",
                mudanca_detectada=False,
                coletado_em=t_last.isoformat(),
            ),
        )
        conn.commit()

        due = _due_para_consulta(conn, ps_id, _AGORA_FIXO, intervalo_segundos)
        assert due == (delta_segundos >= intervalo_segundos), (
            f"delta={delta_segundos}s intervalo={intervalo_segundos}s: "
            f"esperado due={delta_segundos >= intervalo_segundos}, obtido {due}"
        )
    finally:
        conn.close()
