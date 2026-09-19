"""Teste property-based da Property 2 ("rejeicao de URLs invalidas").

# Feature: rastreador-precos-mvp, Property 2: Para qualquer string de URL que
# esteja vazia, exceda 2048 caracteres, pertenca a um dominio nao suportado, ou
# pertenca a um site suportado mas nao contenha um identificador no formato
# esperado, o parsing deve rejeitar a entrada (retornar "nao suportado"/
# "identificador nao extraivel") e nenhuma entrada ProdutoSite deve ser
# persistida.

Este teste exercita ``MercadoLivre.parse_url`` e ``Amazon.parse_url``. Para cada
uma das quatro categorias de entrada invalida abaixo, verificamos que AMBOS os
adapters retornam ``None`` (rejeicao):

1. String vazia.
2. Strings com mais de 2048 caracteres (excedem ``_MAX_URL_LEN``).
3. Dominios nao suportados (ex.: google.com, example.com) com caminhos
   aleatorios — nenhum adapter deve reconhecer o site.
4. Dominio suportado, porem SEM identificador valido:
   - Mercado Livre: host ``mercadolivre.*`` sem nenhum ``MLB``+digitos.
   - Amazon: host ``amazon.*`` sem um ASIN valido em ``/dp/`` ou
     ``/gp/product/``.

Sobre a clausula "nenhuma entrada persistida": nesta etapa ainda nao existe a
funcao de cadastro (task 6.1). A rejeicao de persistencia e garantida pelo fluxo
de cadastro, que so persiste um ``ProdutoSite`` quando ``parse_url`` retorna um
``ItemRef`` nao-``None``. Portanto, ao provar que ``parse_url`` retorna ``None``
para toda URL invalida, provamos que nada seria persistido — este teste foca na
camada de parsing (``parse_url is None``), da qual a rejeicao de persistencia
decorre diretamente.

**Validates: Requirements 1.3, 1.4**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.adapters.amazon import Amazon
from src.adapters.mercado_livre import MercadoLivre

# ---------------------------------------------------------------------------
# Geradores por categoria de rejeicao
# ---------------------------------------------------------------------------

# Categoria 1: string vazia.
_vazia = st.just("")

# Categoria 2: URLs que excedem 2048 caracteres. Construimos uma URL de host
# valido (para garantir que a REJEICAO se deva ao comprimento, e nao ao host) e
# padding que a leva a mais de 2048 chars. Inclui um id valido embutido para
# provar que mesmo uma URL "de outra forma valida" e rejeitada so pelo tamanho.
_host_para_longa = st.sampled_from(
    ["www.mercadolivre.com.br", "www.amazon.com.br"]
)
_padding = st.integers(min_value=2049, max_value=4096)


@st.composite
def _url_longa(draw: st.DrawFn) -> str:
    host = draw(_host_para_longa)
    total = draw(_padding)
    base = f"https://{host}/dp/MLB123456ABCD/"
    enchimento = "a" * max(0, total - len(base))
    url = base + enchimento
    # Garante o invariante da categoria: estritamente > 2048.
    assert len(url) > 2048
    return url


# Categoria 3: dominios nao suportados. Caminho aleatorio pode ate conter algo
# parecido com um id, mas o host nao e nem ML nem Amazon, entao deve rejeitar.
_host_nao_suportado = st.sampled_from(
    [
        "www.google.com",
        "example.com",
        "www.ebay.com",
        "shopee.com.br",
        "americanas.com.br",
        "notmercadolivre.evil.com",  # nao casa o regex de host do ML
        "amazonaws.com",  # nao e "amazon." como host de loja
    ]
)
_caminho_aleatorio = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-_", max_size=40
)


@st.composite
def _url_dominio_nao_suportado(draw: st.DrawFn) -> str:
    host = draw(_host_nao_suportado)
    caminho = draw(_caminho_aleatorio)
    return f"https://{host}/{caminho}"


# Categoria 4a: host ML valido, porem SEM "MLB"+digitos em lugar nenhum.
# Restringimos o alfabeto do slug para nunca formar "MLB<digito>": usamos apenas
# letras minusculas e hifen (sem digitos), garantindo ausencia do padrao.
_host_ml = st.sampled_from(
    [
        "www.mercadolivre.com.br",
        "mercadolivre.com.br",
        "produto.mercadolivre.com.br",
        "www.mercadolibre.com.ar",
    ]
)
_slug_sem_id = st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", max_size=30)


@st.composite
def _url_ml_sem_id(draw: st.DrawFn) -> str:
    host = draw(_host_ml)
    slug = draw(_slug_sem_id)
    return f"https://{host}/{slug}"


# Categoria 4b: host Amazon valido, porem SEM ASIN valido em /dp/ ou
# /gp/product/. Geramos caminhos que ou nao usam esses segmentos, ou usam com um
# "asin" de comprimento invalido (!= 10) usando apenas letras minusculas (o
# regex do adapter exige [A-Z0-9]{10} maiusculo em segmento ancorado).
_host_amz = st.sampled_from(
    [
        "www.amazon.com.br",
        "amazon.com.br",
        "www.amazon.com",
        "amazon.com",
    ]
)
# Token curto/invalido para o segmento de produto: minusculas, comprimento 1..9.
_token_invalido = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=9
)
_caminho_amz_sem_asin = st.one_of(
    # Sem segmento de produto reconhecido.
    st.builds(lambda t: f"/gp/help/{t}", _token_invalido),
    st.builds(lambda t: f"/{t}", _token_invalido),
    # Segmento reconhecido, porem token de tamanho invalido (< 10) e minusculo.
    st.builds(lambda t: f"/dp/{t}", _token_invalido),
    st.builds(lambda t: f"/gp/product/{t}", _token_invalido),
)


@st.composite
def _url_amz_sem_id(draw: st.DrawFn) -> str:
    host = draw(_host_amz)
    caminho = draw(_caminho_amz_sem_asin)
    return f"https://{host}{caminho}"


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(url=_vazia)
def test_rejeita_url_vazia(url: str) -> None:
    """Categoria 1: string vazia e rejeitada por ambos os adapters."""
    assert MercadoLivre.parse_url(url) is None
    assert Amazon.parse_url(url) is None


@settings(max_examples=100)
@given(url=_url_longa())
def test_rejeita_url_longa(url: str) -> None:
    """Categoria 2: URL > 2048 chars e rejeitada por ambos os adapters."""
    assert len(url) > 2048
    assert MercadoLivre.parse_url(url) is None
    assert Amazon.parse_url(url) is None


@settings(max_examples=200)
@given(url=_url_dominio_nao_suportado())
def test_rejeita_dominio_nao_suportado(url: str) -> None:
    """Categoria 3: dominio nao suportado e rejeitado por ambos os adapters."""
    assert MercadoLivre.parse_url(url) is None
    assert Amazon.parse_url(url) is None


@settings(max_examples=200)
@given(url=_url_ml_sem_id())
def test_rejeita_ml_sem_id(url: str) -> None:
    """Categoria 4a: host ML valido sem 'MLB'+digitos e rejeitado."""
    # O adapter ML rejeita por ausencia do item_id.
    assert MercadoLivre.parse_url(url) is None
    # E o adapter Amazon rejeita porque o host nao e da Amazon.
    assert Amazon.parse_url(url) is None


@settings(max_examples=200)
@given(url=_url_amz_sem_id())
def test_rejeita_amazon_sem_asin(url: str) -> None:
    """Categoria 4b: host Amazon valido sem ASIN valido e rejeitado."""
    # O adapter Amazon rejeita por ausencia de ASIN valido.
    assert Amazon.parse_url(url) is None
    # E o adapter ML rejeita porque o host nao e do Mercado Livre.
    assert MercadoLivre.parse_url(url) is None
