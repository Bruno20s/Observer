"""Teste property-based da Property 6 ("deteccao de mudanca de preco").

# Feature: rastreador-precos-mvp, Property 6: Uma mudanca e detectada (e, portanto,
# uma notificacao seria disparada) SE E SOMENTE SE existir um preco anterior E o
# novo preco diferir dele sob comparacao Decimal EXATA. Sem preco anterior -> sem
# mudanca. Igual -> sem mudanca. Diferente -> mudanca (subida se novo>anterior,
# queda se novo<anterior).

Este teste exercita a funcao **pura** `src.jobs.monitor.detectar_mudanca`, que
concentra a decisao de "houve mudanca?" do Job_Monitor. A comparacao e feita como
valor monetario exato em :class:`~decimal.Decimal`, sem tolerancia de
arredondamento (R4.2).

Regras validadas (bicondicional da Property 6):

- ``anterior is None`` -> ``houve_mudanca=False`` e ``direcao=None`` (R4.5).
- ``anterior == novo`` (igualdade Decimal por valor) -> ``houve_mudanca=False`` e
  ``direcao=None`` (R4.4).
- ``anterior != novo`` -> ``houve_mudanca=True`` e ``direcao`` = SUBIDA sse
  ``novo > anterior``, caso contrario QUEDA (R4.3).

O gerador restringe os precos a Decimals finitos e limitados (sem NaN/inf, faixa
e casas controladas) para manter o teste rapido. Incluimos ainda um caso
direcionado de Decimals com escala diferente porem mesmo valor
(``Decimal("10.00")`` vs ``Decimal("10.000")``) para confirmar que a comparacao e
por VALOR (nao por representacao textual): esse par NAO caracteriza mudanca.

**Validates: Requirements 4.2, 4.3, 4.4, 4.5; Property 6**
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from src.jobs.monitor import (
    DirecaoMudanca,
    ResultadoMudanca,
    SEM_MUDANCA,
    detectar_mudanca,
)

# ---------------------------------------------------------------------------
# Geradores.
#
# Precos monetarios: Decimals finitos, nao-negativos, com ate 2 casas e faixa
# limitada para manter o espaco de busca pequeno/rapido. `places=2` alinha com o
# dominio monetario, mas a comparacao da funcao e por valor, entao a escala nao
# altera a semantica (ver caso direcionado de escala mais abaixo).
# ---------------------------------------------------------------------------
_preco = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)

# `anterior` pode ser None (primeira observacao) ou um Decimal.
_anterior = st.one_of(st.none(), _preco)


# ===========================================================================
# Property 6 — bicondicional completa sobre detectar_mudanca (funcao pura).
# ===========================================================================
@settings(max_examples=300)
@given(anterior=_anterior, novo=_preco)
def test_property_6_deteccao_mudanca_bicondicional(
    anterior: Optional[Decimal], novo: Decimal
) -> None:
    """Mudanca detectada SSE ha anterior E ``novo`` difere dele (Decimal exato).

    **Validates: Requirements 4.2, 4.3, 4.4, 4.5; Property 6**
    """
    resultado = detectar_mudanca(anterior, novo)

    # O resultado e sempre um ResultadoMudanca coerente.
    assert isinstance(resultado, ResultadoMudanca)

    # A condicao de mudanca esperada, derivada diretamente da Property 6.
    esperado_mudanca = anterior is not None and novo != anterior
    assert resultado.houve_mudanca is esperado_mudanca

    if not esperado_mudanca:
        # Sem preco anterior (R4.5) OU precos iguais (R4.4): sem mudanca, sem direcao.
        assert resultado.direcao is None
        return

    # Houve mudanca (R4.3): a direcao classifica subida vs queda por valor exato.
    assert resultado.direcao is not None
    if novo > anterior:
        assert resultado.direcao is DirecaoMudanca.SUBIDA
    else:
        assert novo < anterior
        assert resultado.direcao is DirecaoMudanca.QUEDA


@settings(max_examples=200)
@given(preco=_preco)
def test_property_6_sem_anterior_nunca_muda(preco: Decimal) -> None:
    """Sem preco anterior (``None``) -> nunca ha mudanca, qualquer que seja o novo (R4.5).

    **Validates: Requirements 4.5; Property 6**
    """
    resultado = detectar_mudanca(None, preco)

    assert resultado.houve_mudanca is False
    assert resultado.direcao is None
    assert resultado == SEM_MUDANCA


@settings(max_examples=200)
@given(preco=_preco)
def test_property_6_igual_nunca_muda(preco: Decimal) -> None:
    """Preco igual ao anterior -> sem mudanca (R4.4).

    **Validates: Requirements 4.4; Property 6**
    """
    resultado = detectar_mudanca(preco, preco)

    assert resultado.houve_mudanca is False
    assert resultado.direcao is None


def test_property_6_escala_diferente_mesmo_valor_nao_e_mudanca() -> None:
    """Decimals com escalas distintas mas mesmo valor NAO caracterizam mudanca (R4.2).

    A comparacao e por VALOR monetario, nao por representacao textual: portanto
    ``Decimal("10.00")`` e ``Decimal("10.000")`` sao iguais e nao disparam
    notificacao. Ja ``Decimal("10.01")`` difere de ``Decimal("10.00")``.

    **Validates: Requirements 4.2, 4.3, 4.4; Property 6**
    """
    # Mesmo valor, escalas diferentes -> sem mudanca.
    igual = detectar_mudanca(Decimal("10.00"), Decimal("10.000"))
    assert igual.houve_mudanca is False
    assert igual.direcao is None

    # Diferenca minima de 1 centavo -> mudanca (subida).
    subida = detectar_mudanca(Decimal("10.00"), Decimal("10.01"))
    assert subida.houve_mudanca is True
    assert subida.direcao is DirecaoMudanca.SUBIDA

    # Diferenca minima de 1 centavo -> mudanca (queda).
    queda = detectar_mudanca(Decimal("10.00"), Decimal("9.99"))
    assert queda.houve_mudanca is True
    assert queda.direcao is DirecaoMudanca.QUEDA
