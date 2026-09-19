"""Teste property-based da Property 5 ("invariante append-only do historico").

# Feature: rastreador-precos-mvp, Property 5: Para qualquer sequencia de
# operacoes do sistema (insercoes de HistoricoPreco/HistoricoReputacao,
# associacao e desassociacao de ProdutoSite), a quantidade de registros em
# HistoricoPreco e HistoricoReputacao de cada ProdutoSite nunca diminui e
# nenhum registro previamente gravado e alterado ou sobrescrito.

Este teste exercita a superficie append-only de `src.db.database`
(``inserir_historico_preco``, ``inserir_historico_reputacao``,
``associar_produto``, ``desassociar_produto``) contra um banco SQLite em
memoria (`:memory:`).

Estrategia: geramos uma sequencia arbitraria de operacoes. Apos aplicar cada
operacao, verificamos dois invariantes:

1. **Monotonicidade da contagem** — a contagem de linhas em ``historico_preco``
   e em ``historico_reputacao`` (por ProdutoSite e no total) nunca e menor que a
   contagem observada apos a operacao anterior.
2. **Imutabilidade dos registros** — cada linha de historico ja inserida
   (identificada por ``id``) permanece byte-a-byte identica ao seu snapshot
   original em todas as observacoes subsequentes.

As operacoes ``associar``/``desassociar`` alteram apenas ``produto_site.produto_id``
e, portanto, nao devem tocar em nenhuma linha de historico — algo que os dois
invariantes acima capturam diretamente.

**Validates: Requirements 2.5, 3.2, 4.6, 6.3**
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from src.db.database import (
    associar_produto,
    desassociar_produto,
    inicializar_banco,
    inserir_historico_preco,
    inserir_historico_reputacao,
)
from src.db.models import HistoricoPreco, HistoricoReputacao

# Timestamp fixo de largura fixa: o conteudo exato nao importa para o invariante
# append-only, apenas que os registros sejam gravaveis e comparaveis.
_TS = "2024-01-01T00:00:00Z"

# --- Operacoes geradas -----------------------------------------------------
# Cada operacao e uma tupla (tipo, payload). Usamos indices para escolher o
# ProdutoSite/Produto alvo de forma segura (modulo a quantidade existente).

_OP_INSERIR_PRECO = "inserir_preco"
_OP_INSERIR_REPUTACAO = "inserir_reputacao"
_OP_ASSOCIAR = "associar"
_OP_DESASSOCIAR = "desassociar"

_preco_opcional = st.one_of(
    st.none(),
    st.integers(min_value=0, max_value=1_000_000).map(
        lambda centavos: Decimal(centavos) / Decimal(100)
    ),
)

_op_inserir_preco = st.tuples(
    st.just(_OP_INSERIR_PRECO),
    st.integers(min_value=0, max_value=999),  # indice do ProdutoSite (mod N)
    _preco_opcional,
    st.booleans(),  # mudanca_detectada
)

_op_inserir_reputacao = st.tuples(
    st.just(_OP_INSERIR_REPUTACAO),
    st.integers(min_value=0, max_value=999),  # indice do ProdutoSite (mod N)
    st.one_of(st.none(), st.floats(min_value=0.0, max_value=100.0)),  # % reclamacoes
    st.booleans(),  # sinais_indisponiveis
)

_op_associar = st.tuples(
    st.just(_OP_ASSOCIAR),
    st.integers(min_value=0, max_value=999),  # indice do ProdutoSite (mod N)
    st.integers(min_value=0, max_value=999),  # indice do Produto (mod M)
)

_op_desassociar = st.tuples(
    st.just(_OP_DESASSOCIAR),
    st.integers(min_value=0, max_value=999),  # indice do ProdutoSite (mod N)
)

_operacao = st.one_of(
    _op_inserir_preco,
    _op_inserir_reputacao,
    _op_associar,
    _op_desassociar,
)


def _snapshot_historico(
    conn: sqlite3.Connection, tabela: str
) -> dict[int, tuple]:
    """Retorna {id: linha_completa_como_tupla} de uma tabela de historico.

    A tupla captura *todas* as colunas, permitindo detectar qualquer alteracao
    byte-a-byte em um registro previamente gravado.
    """
    rows = conn.execute(f"SELECT * FROM {tabela} ORDER BY id").fetchall()
    return {row["id"]: tuple(row) for row in rows}


@settings(max_examples=200)
@given(
    operacoes=st.lists(_operacao, min_size=0, max_size=40),
    num_produto_sites=st.integers(min_value=1, max_value=4),
    num_produtos=st.integers(min_value=1, max_value=3),
)
def test_historico_e_append_only(
    operacoes: list[tuple],
    num_produto_sites: int,
    num_produtos: int,
) -> None:
    conn: sqlite3.Connection = inicializar_banco(":memory:")
    try:
        # --- Fixture base: alguns Produtos e ProdutoSites ------------------
        produto_ids: list[int] = []
        for i in range(num_produtos):
            cur = conn.execute(
                "INSERT INTO produto (nome, criado_em) VALUES (?, ?)",
                (f"Produto {i}", _TS),
            )
            produto_ids.append(cur.lastrowid)

        produto_site_ids: list[int] = []
        for i in range(num_produto_sites):
            cur = conn.execute(
                """
                INSERT INTO produto_site
                    (produto_id, site, item_id, url_original, criado_em)
                VALUES (NULL, ?, ?, ?, ?)
                """,
                ("mercado_livre", f"MLB{i}", f"https://exemplo/MLB{i}", _TS),
            )
            produto_site_ids.append(cur.lastrowid)
        conn.commit()

        # Snapshots acumulados dos registros de historico ja observados, e as
        # contagens monotonicas de referencia.
        snap_preco: dict[int, tuple] = {}
        snap_reput: dict[int, tuple] = {}
        contagem_preco = 0
        contagem_reput = 0

        def verificar_invariantes() -> None:
            nonlocal snap_preco, snap_reput, contagem_preco, contagem_reput

            atual_preco = _snapshot_historico(conn, "historico_preco")
            atual_reput = _snapshot_historico(conn, "historico_reputacao")

            # (1) Monotonicidade: a contagem nunca diminui.
            assert len(atual_preco) >= contagem_preco, (
                "contagem de historico_preco diminuiu: "
                f"{len(atual_preco)} < {contagem_preco}"
            )
            assert len(atual_reput) >= contagem_reput, (
                "contagem de historico_reputacao diminuiu: "
                f"{len(atual_reput)} < {contagem_reput}"
            )

            # (2) Imutabilidade: todo id ja visto continua identico, e nenhum
            # id previamente gravado desaparece.
            for id_ant, linha_ant in snap_preco.items():
                assert id_ant in atual_preco, (
                    f"registro historico_preco id={id_ant} desapareceu"
                )
                assert atual_preco[id_ant] == linha_ant, (
                    f"registro historico_preco id={id_ant} foi alterado"
                )
            for id_ant, linha_ant in snap_reput.items():
                assert id_ant in atual_reput, (
                    f"registro historico_reputacao id={id_ant} desapareceu"
                )
                assert atual_reput[id_ant] == linha_ant, (
                    f"registro historico_reputacao id={id_ant} foi alterado"
                )

            # Avanca as referencias monotonicas.
            snap_preco = atual_preco
            snap_reput = atual_reput
            contagem_preco = len(atual_preco)
            contagem_reput = len(atual_reput)

        # Estado inicial (sem historico) tambem deve satisfazer os invariantes.
        verificar_invariantes()

        for op in operacoes:
            tipo = op[0]
            if tipo == _OP_INSERIR_PRECO:
                _, idx_ps, preco, mudanca = op
                ps_id = produto_site_ids[idx_ps % len(produto_site_ids)]
                inserir_historico_preco(
                    conn,
                    HistoricoPreco(
                        produto_site_id=ps_id,
                        preco=preco,
                        coletado_em=_TS,
                        mudanca_detectada=mudanca,
                    ),
                )
            elif tipo == _OP_INSERIR_REPUTACAO:
                _, idx_ps, perc, indisponivel = op
                ps_id = produto_site_ids[idx_ps % len(produto_site_ids)]
                inserir_historico_reputacao(
                    conn,
                    HistoricoReputacao(
                        produto_site_id=ps_id,
                        coletado_em=_TS,
                        ml_percentual_reclamacoes=perc,
                        sinais_indisponiveis=indisponivel,
                    ),
                )
            elif tipo == _OP_ASSOCIAR:
                _, idx_ps, idx_prod = op
                ps_id = produto_site_ids[idx_ps % len(produto_site_ids)]
                prod_id = produto_ids[idx_prod % len(produto_ids)]
                associar_produto(conn, ps_id, prod_id)
            elif tipo == _OP_DESASSOCIAR:
                _, idx_ps = op
                ps_id = produto_site_ids[idx_ps % len(produto_site_ids)]
                desassociar_produto(conn, ps_id)
            else:  # pragma: no cover - guarda defensiva
                raise AssertionError(f"operacao desconhecida: {tipo!r}")

            # Apos CADA operacao, os invariantes devem valer.
            verificar_invariantes()

        # Ao final, a contagem total deve igualar exatamente o numero de
        # insercoes de historico executadas (nenhuma insercao "sumiu").
        num_inserts_preco = sum(1 for o in operacoes if o[0] == _OP_INSERIR_PRECO)
        num_inserts_reput = sum(
            1 for o in operacoes if o[0] == _OP_INSERIR_REPUTACAO
        )
        assert contagem_preco == num_inserts_preco
        assert contagem_reput == num_inserts_reput
    finally:
        conn.close()
