"""Teste property-based da Property 3 ("idempotencia/dedup do cadastro").

# Feature: rastreador-precos-mvp, Property 3: Para qualquer par (site, item_id),
# cadastrar o mesmo par duas ou mais vezes deve resultar em exatamente uma
# entrada ProdutoSite persistida; tentativas subsequentes sao rejeitadas como
# duplicata.

Este teste exercita ``src.cadastro.cadastrar_produto`` contra um banco SQLite em
memoria (``:memory:``). Para cada exemplo, gera um par valido (site, item_id) e
N (1..10) URLs de anuncio que fazem parsing para EXATAMENTE esse mesmo par, e
entao cadastra as N URLs em sequencia.

Verificacoes (invariantes da Property 3):

- Exatamente UMA tentativa retorna ``StatusCadastro.SUCESSO`` (a primeira).
- Todas as tentativas subsequentes retornam ``StatusCadastro.DUPLICADO``.
- ``listar_produto_site`` mostra exatamente UMA entrada para aquele
  (site, item_id) — independentemente de as URLs serem literalmente diferentes,
  pois a dedup e por (site, item_id) e nao pela string exata da URL.

O gerador cobre tanto Mercado Livre (``MLB`` + digitos) quanto Amazon (ASIN de
10 caracteres ``[A-Z0-9]``), e mistura variacoes de URL (host, slug, query) que
mapeiam para o mesmo identificador, fortalecendo a propriedade.

**Validates: Requirements 1.5**
"""

from __future__ import annotations

import sqlite3

from hypothesis import given, settings
from hypothesis import strategies as st

from src.adapters.base import Site
from src.cadastro import StatusCadastro, cadastrar_produto
from src.db.database import inicializar_banco, listar_produto_site

# ---------------------------------------------------------------------------
# Geradores
# ---------------------------------------------------------------------------

# item_id do Mercado Livre: "MLB" + 1..12 digitos. O adapter normaliza para
# maiusculas; geramos ja em maiusculas para um par canonico previsivel.
_item_id_ml = st.integers(min_value=0, max_value=10**12 - 1).map(
    lambda n: f"MLB{n}"
)

# ASIN da Amazon: exatamente 10 caracteres de [A-Z0-9].
_char_asin = st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_asin = st.lists(_char_asin, min_size=10, max_size=10).map("".join)

# Hosts aceitos por cada adapter (mesmo (site, item_id), URLs diferentes).
_host_ml = st.sampled_from(
    [
        "www.mercadolivre.com.br",
        "mercadolivre.com.br",
        "produto.mercadolivre.com.br",
        "www.mercadolibre.com.ar",
    ]
)
_host_amz = st.sampled_from(
    [
        "www.amazon.com.br",
        "amazon.com.br",
        "www.amazon.com",
    ]
)
_slug = st.text(alphabet="abc-", max_size=8)
_padrao_amz = st.sampled_from(["dp", "gp/product"])
_query = st.sampled_from(["", "?ref=abc", "?a=1&b=2", "#anchor"])


def _fabricas_ml(item_id: str) -> st.SearchStrategy[str]:
    """Estrategia de URLs distintas do ML que fazem parse para o MESMO item_id."""

    def montar(host: str, slug: str, query: str) -> str:
        return f"https://{host}/{slug}/p/{item_id}{query}"

    return st.builds(montar, _host_ml, _slug, _query)


def _fabricas_amz(asin: str) -> st.SearchStrategy[str]:
    """Estrategia de URLs distintas da Amazon que fazem parse para o MESMO ASIN."""

    def montar(host: str, padrao: str, query: str) -> str:
        return f"https://{host}/{padrao}/{asin}{query}"

    return st.builds(montar, _host_amz, _padrao_amz, _query)


# Um "caso" = (site esperado, item_id esperado, lista de N URLs que mapeiam para
# esse par). Usamos `st.builds`/`flatmap` para acoplar o item_id gerado as URLs
# construidas a partir dele.
def _caso_ml() -> st.SearchStrategy[tuple[Site, str, list[str]]]:
    return _item_id_ml.flatmap(
        lambda item_id: st.lists(
            _fabricas_ml(item_id), min_size=1, max_size=10
        ).map(lambda urls: (Site.MERCADO_LIVRE, item_id, urls))
    )


def _caso_amz() -> st.SearchStrategy[tuple[Site, str, list[str]]]:
    return _asin.flatmap(
        lambda asin: st.lists(
            _fabricas_amz(asin), min_size=1, max_size=10
        ).map(lambda urls: (Site.AMAZON, asin, urls))
    )


_caso = st.one_of(_caso_ml(), _caso_amz())


@settings(max_examples=200)
@given(caso=_caso)
def test_cadastro_dedup_uma_unica_entrada(
    caso: tuple[Site, str, list[str]],
) -> None:
    """Cadastrar N URLs do mesmo (site, item_id) => 1 SUCESSO, resto DUPLICADO, 1 linha."""
    site_esperado, item_id_esperado, urls = caso

    conn: sqlite3.Connection = inicializar_banco(":memory:")
    try:
        resultados = [cadastrar_produto(conn, url) for url in urls]

        status = [r.status for r in resultados]

        # Exatamente uma tentativa bem-sucedida, e ela e a PRIMEIRA.
        assert status[0] == StatusCadastro.SUCESSO
        assert status.count(StatusCadastro.SUCESSO) == 1

        # Todas as demais sao rejeitadas como duplicata.
        assert all(s == StatusCadastro.DUPLICADO for s in status[1:])

        # A entrada persistida corresponde ao (site, item_id) esperado.
        primeiro = resultados[0]
        assert primeiro.sucesso is True
        assert primeiro.produto_site is not None
        assert primeiro.produto_site.site == site_esperado.value
        assert primeiro.produto_site.item_id == item_id_esperado

        # As tentativas duplicadas nao trazem uma nova ProdutoSite.
        assert all(r.produto_site is None for r in resultados[1:])

        # Estado final do banco: exatamente UMA entrada para esse (site, item_id).
        entradas = listar_produto_site(conn)
        do_par = [
            ps
            for ps in entradas
            if ps.site == site_esperado.value and ps.item_id == item_id_esperado
        ]
        assert len(do_par) == 1
        # E, no total, so existe essa unica entrada (nada mais foi persistido).
        assert len(entradas) == 1
    finally:
        conn.close()


@settings(max_examples=100)
@given(
    item_id=_item_id_ml,
    n=st.integers(min_value=2, max_value=10),
    hosts=st.lists(_host_ml, min_size=2, max_size=10),
)
def test_dedup_por_par_e_nao_por_url_exata(
    item_id: str, n: int, hosts: list[str]
) -> None:
    """Mesmo (site, item_id) via URLs literalmente distintas ainda dedup para 1 linha.

    Foca no ponto de que a deduplicacao e por (site, item_id), NAO pela string
    exata da URL: variamos o host/slug em cada tentativa, mas o item_id e o mesmo.
    """
    conn: sqlite3.Connection = inicializar_banco(":memory:")
    try:
        urls = [
            f"https://{hosts[i % len(hosts)]}/anuncio-{i}/p/{item_id}?v={i}"
            for i in range(n)
        ]
        # Garante que as URLs sao de fato distintas entre si.
        assert len(set(urls)) == len(urls)

        resultados = [cadastrar_produto(conn, url) for url in urls]

        assert resultados[0].status == StatusCadastro.SUCESSO
        assert all(
            r.status == StatusCadastro.DUPLICADO for r in resultados[1:]
        )

        entradas = listar_produto_site(conn)
        assert len(entradas) == 1
        assert entradas[0].site == Site.MERCADO_LIVRE.value
        assert entradas[0].item_id == item_id
    finally:
        conn.close()
