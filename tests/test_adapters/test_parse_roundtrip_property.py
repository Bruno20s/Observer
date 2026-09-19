"""Teste property-based da Property 1 ("round-trip de parsing de URL").

# Feature: rastreador-precos-mvp, Property 1: Para qualquer site suportado e
# identificador valido (item_id "MLB"+digitos no Mercado Livre; ASIN de
# exatamente 10 caracteres alfanumericos na Amazon), ao construir uma URL de
# anuncio contendo esse identificador e fazer o parsing dela, o resultado deve
# identificar o site correto e recuperar exatamente o mesmo identificador.

Este teste exercita ``MercadoLivre.parse_url`` e ``Amazon.parse_url``. Para cada
site, gera um identificador valido, constroi uma URL de anuncio que o adapter
efetivamente aceita e verifica o round-trip: o parsing recupera o MESMO site e o
MESMO identificador.

Notas de normalizacao (importantes para o round-trip):
- O Mercado Livre normaliza o item_id para MAIUSCULAS. Portanto, se o gerador
  produzir "mlb123", esperamos recuperar "MLB123" (comparamos com ``.upper()``).
- O ASIN da Amazon ja e gerado a partir de ``[A-Z0-9]`` (maiusculas), entao a
  recuperacao e exatamente igual.

**Validates: Requirements 1.1, 1.2, 1.6**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.adapters.amazon import Amazon
from src.adapters.mercado_livre import MercadoLivre
from src.adapters.base import Site

# ---------------------------------------------------------------------------
# Geradores
# ---------------------------------------------------------------------------

# item_id do Mercado Livre: "MLB" (em maiuscula ou minuscula, para exercitar a
# normalizacao) seguido de 1..12 digitos. O regex do adapter e case-insensitive
# e normaliza o resultado para maiusculas.
_prefixo_mlb = st.sampled_from(["MLB", "mlb", "Mlb", "mLB"])
_digitos_ml = st.integers(min_value=0, max_value=10**12 - 1).map(str)
_item_id_ml = st.builds(lambda p, d: p + d, _prefixo_mlb, _digitos_ml)

# Hosts validos do Mercado Livre aceitos pelo adapter (mercadolivre.*/mercadolibre.*).
_host_ml = st.sampled_from(
    [
        "www.mercadolivre.com.br",
        "mercadolivre.com.br",
        "produto.mercadolivre.com.br",
        "www.mercadolibre.com.ar",
        "mercadolibre.com",
    ]
)

# ASIN da Amazon: exatamente 10 caracteres de [A-Z0-9] (maiusculas).
_char_asin = st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_asin = st.lists(_char_asin, min_size=10, max_size=10).map("".join)

# Hosts validos da Amazon aceitos pelo adapter (amazon.*).
_host_amz = st.sampled_from(
    [
        "www.amazon.com.br",
        "amazon.com.br",
        "www.amazon.com",
        "amazon.com",
    ]
)

# Padrao de segmento do ASIN aceito pelo adapter: /dp/<ASIN> ou /gp/product/<ASIN>.
_padrao_amz = st.sampled_from(["dp", "gp/product"])


@settings(max_examples=200)
@given(host=_host_ml, item_id=_item_id_ml, slug=st.text(alphabet="abc-", max_size=8))
def test_roundtrip_mercado_livre(host: str, item_id: str, slug: str) -> None:
    """ML: uma URL construida com o item_id gerado recupera o mesmo (site, item_id)."""
    # URLs que o adapter aceita: qualquer URL do host ML contendo "MLB"+digitos.
    # Exercitamos alguns formatos reais de anuncio.
    url = f"https://{host}/{slug}/p/{item_id}"

    resultado = MercadoLivre.parse_url(url)

    assert resultado is not None, url
    assert resultado.site == Site.MERCADO_LIVRE
    # ML normaliza para maiusculas: round-trip modulo case.
    assert resultado.item_id == item_id.upper()
    assert resultado.url_original == url


@settings(max_examples=200)
@given(host=_host_amz, asin=_asin, padrao=_padrao_amz)
def test_roundtrip_amazon(host: str, asin: str, padrao: str) -> None:
    """Amazon: uma URL /dp/<ASIN> ou /gp/product/<ASIN> recupera o mesmo (site, ASIN)."""
    url = f"https://{host}/{padrao}/{asin}"

    resultado = Amazon.parse_url(url)

    assert resultado is not None, url
    assert resultado.site == Site.AMAZON
    assert resultado.item_id == asin
    assert resultado.url_original == url
