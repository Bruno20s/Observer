"""Teste property-based da Property 4 ("ultimo preco por timestamp").

# Feature: rastreador-precos-mvp, Property 4: Para qualquer sequencia nao vazia
# de registros HistoricoPreco de um ProdutoSite, o "preco atual" retornado pelo
# sistema (obter_ultimo_preco) deve ser o do registro de maior `coletado_em`
# (desempate pelo maior `id`, i.e. a ordem de insercao).

Este teste exercita `src.db.database.obter_ultimo_preco` contra um banco SQLite
em memoria (`:memory:`). Insere uma sequencia arbitraria, porem nao vazia, de
`HistoricoPreco` para um mesmo `ProdutoSite` e verifica que o registro retornado
e exatamente aquele com o maior `coletado_em`; havendo empate de timestamp, o de
maior `id` (o ultimo inserido, pois o `id` e AUTOINCREMENT).

O gerador produz timestamps ISO-8601 UTC com largura fixa, de modo que a
comparacao textual (lexicografica) usada pelo SQLite coincida com a ordem
cronologica. Empates de timestamp sao propositalmente frequentes para exercitar
o criterio de desempate por `id`.

**Validates: Requirements 2.3, 4.1**
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from src.db.database import (
    inicializar_banco,
    inserir_historico_preco,
    obter_ultimo_preco,
)
from src.db.models import HistoricoPreco

# Base temporal fixa; cada registro escolhe um deslocamento inteiro de minutos a
# partir daqui. Formatar sempre com o mesmo padrao (largura fixa, sufixo "Z")
# garante que a ordem lexicografica das strings == ordem cronologica, que e o
# que `obter_ultimo_preco` assume ao ordenar por `coletado_em` como TEXT.
_BASE = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _timestamp_iso(offset_minutos: int) -> str:
    """Timestamp ISO-8601 UTC de largura fixa para `offset_minutos` a partir da base."""
    momento = _BASE + timedelta(minutes=offset_minutos)
    return momento.strftime("%Y-%m-%dT%H:%M:%SZ")


# Um registro e um par (offset_de_minutos, preco_opcional). Restringimos o
# espaco de offsets a uma faixa pequena para forcar empates frequentes de
# timestamp entre registros distintos, exercitando o desempate por `id`.
_registro = st.tuples(
    st.integers(min_value=0, max_value=8),
    st.one_of(
        st.none(),
        st.integers(min_value=0, max_value=1_000_000).map(
            lambda centavos: Decimal(centavos) / Decimal(100)
        ),
    ),
)


def _esperado_por_ordenacao(
    registros: list[tuple[int, Optional[Decimal]]],
) -> tuple[int, int]:
    """Indice do registro esperado como "ultimo preco", segundo (coletado_em, id).

    Os registros sao inseridos na ordem da lista; o `id` AUTOINCREMENT segue essa
    ordem (1-based). O vencedor e o de maior timestamp e, em empate, o de maior
    `id` (o inserido mais tarde). Retorna (indice_na_lista, id_esperado).
    """
    # id atribuido = posicao (1-based) na ordem de insercao.
    melhor_idx = max(
        range(len(registros)),
        key=lambda i: (registros[i][0], i),  # (offset ~ timestamp, ordem de insercao)
    )
    return melhor_idx, melhor_idx + 1


@settings(max_examples=200)
@given(registros=st.lists(_registro, min_size=1, max_size=30))
def test_obter_ultimo_preco_retorna_maior_timestamp_desempate_por_id(
    registros: list[tuple[int, Optional[Decimal]]],
) -> None:
    conn: sqlite3.Connection = inicializar_banco(":memory:")
    try:
        # Um ProdutoSite base para satisfazer a FK de historico_preco. Nao ha
        # helper de insert dedicado, entao inserimos diretamente contra o schema.
        cur = conn.execute(
            """
            INSERT INTO produto_site (produto_id, site, item_id, url_original, criado_em)
            VALUES (NULL, ?, ?, ?, ?)
            """,
            ("mercado_livre", "MLB1", "https://exemplo/MLB1", _timestamp_iso(0)),
        )
        conn.commit()
        produto_site_id = cur.lastrowid

        # Insere a sequencia na ordem gerada; o id AUTOINCREMENT segue a ordem.
        ids_inseridos: list[int] = []
        for offset, preco in registros:
            inserido = inserir_historico_preco(
                conn,
                HistoricoPreco(
                    produto_site_id=produto_site_id,
                    preco=preco,
                    coletado_em=_timestamp_iso(offset),
                ),
            )
            ids_inseridos.append(inserido.id)

        # Os ids devem ter sido atribuidos 1..N na ordem de insercao.
        assert ids_inseridos == list(range(1, len(registros) + 1))

        idx_esperado, id_esperado = _esperado_por_ordenacao(registros)
        offset_esperado, preco_esperado = registros[idx_esperado]

        resultado = obter_ultimo_preco(conn, produto_site_id)

        assert resultado is not None  # sequencia nao vazia -> sempre ha ultimo
        assert resultado.id == id_esperado
        assert resultado.coletado_em == _timestamp_iso(offset_esperado)
        assert resultado.preco == preco_esperado
        # O timestamp retornado e o maximo de fato presente.
        assert resultado.coletado_em == max(_timestamp_iso(o) for o, _ in registros)
    finally:
        conn.close()


@settings(max_examples=50)
@given(offsets=st.lists(st.integers(min_value=0, max_value=8), min_size=2, max_size=15))
def test_desempate_estrito_escolhe_maior_id(offsets: list[int]) -> None:
    """Foco no desempate: varios registros com o MESMO timestamp -> maior id vence.

    Insere todos os registros com um timestamp identico; o "ultimo preco" deve
    ser sempre o ultimo inserido (maior id), independentemente dos precos.
    """
    conn: sqlite3.Connection = inicializar_banco(":memory:")
    try:
        cur = conn.execute(
            """
            INSERT INTO produto_site (produto_id, site, item_id, url_original, criado_em)
            VALUES (NULL, ?, ?, ?, ?)
            """,
            ("amazon", "ABCDEFGHIJ", "https://exemplo/dp/ABCDEFGHIJ", _timestamp_iso(0)),
        )
        conn.commit()
        produto_site_id = cur.lastrowid

        mesmo_ts = _timestamp_iso(5)
        ultimo_id = None
        for i, _ in enumerate(offsets):
            inserido = inserir_historico_preco(
                conn,
                HistoricoPreco(
                    produto_site_id=produto_site_id,
                    preco=Decimal(i),
                    coletado_em=mesmo_ts,
                ),
            )
            ultimo_id = inserido.id

        resultado = obter_ultimo_preco(conn, produto_site_id)

        assert resultado is not None
        assert resultado.id == ultimo_id  # maior id == ultimo inserido
        assert resultado.coletado_em == mesmo_ts
        assert resultado.preco == Decimal(len(offsets) - 1)
    finally:
        conn.close()
