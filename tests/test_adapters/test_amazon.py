"""Testes do adapter da Amazon (task 8.3).

Reune dois tipos de teste:

1. **Property-based (Hypothesis) — Property 12** do atraso aleatorio: o valor
   sorteado por :meth:`Amazon.calcular_atraso` esta SEMPRE em ``[min, max]``,
   para qualquer intervalo configurado (``0 <= min <= max``). O teste cobre
   tanto o gerador real (:func:`random.uniform`) quanto um ``rng`` adversarial
   que devolve valores fora do intervalo — o adapter faz ``clamp`` defensivo.
   O marcador ``# Feature: rastreador-precos-mvp, Property 12`` identifica este
   teste como property-based (Validates: Requirements 3.7; Property 12).

2. **Mockados (exemplo, sem marcador)** de parsing de HTML e de bloqueio/CAPTCHA
   (R6.2, R3.7). O browser real (Playwright) NUNCA sobe: injetamos um
   ``buscar_pagina`` fake que devolve :class:`PaginaAmazon` com HTML/status
   controlados, e um ``sleep`` no-op para nunca dormir de verdade.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from src.adapters.amazon import Amazon, PaginaAmazon
from src.adapters.base import Site

# ===========================================================================
# 1) Property 12 — atraso aleatorio sempre em [min, max]
# ===========================================================================

# Feature: rastreador-precos-mvp, Property 12: o atraso gerado por
# Amazon.calcular_atraso() esta SEMPRE dentro de [delay_min, delay_max] para
# qualquer intervalo configurado (0 <= min <= max), independentemente do rng.


# Gera um par (min, max) valido: 0 <= min <= max. Sorteia dois floats finitos
# nao negativos e os ordena. Restringimos a magnitude para manter o teste rapido
# e os numeros representaveis, sem perder generalidade do intervalo.
_delay_bounds = st.tuples(
    st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False, allow_infinity=False),
).map(lambda par: (min(par), max(par)))


@settings(max_examples=300)
@given(bounds=_delay_bounds)
def test_property_12_atraso_sempre_no_intervalo_com_rng_real(
    bounds: tuple[float, float],
) -> None:
    """Property 12 (gerador real): min <= calcular_atraso() <= max sempre.

    Usa o ``rng`` default (:func:`random.uniform`), que e o gerador de fato
    empregado em producao. A propriedade deve valer para todos os intervalos.

    **Validates: Requirements 3.7; Property 12**
    """
    delay_min, delay_max = bounds
    adapter = Amazon(
        delay_min_segundos=delay_min,
        delay_max_segundos=delay_max,
        sleep=lambda _s: None,  # nunca dorme
        buscar_pagina=lambda _url: PaginaAmazon(html="", status_code=None),
    )

    atraso = adapter.calcular_atraso()

    assert delay_min <= atraso <= delay_max


@settings(max_examples=300)
@given(
    bounds=_delay_bounds,
    # rng adversarial: devolve um valor QUALQUER (inclusive fora de [min,max]).
    fora=st.floats(
        min_value=-10_000.0, max_value=10_000.0, allow_nan=False, allow_infinity=False
    ),
)
def test_property_12_atraso_clampado_com_rng_adversarial(
    bounds: tuple[float, float], fora: float
) -> None:
    """Property 12 (rng fora do contrato): resultado e clampado em [min, max].

    Mesmo que um ``rng`` injetado viole o contrato e retorne valores fora do
    intervalo, o adapter garante ``min <= calcular_atraso() <= max``.

    **Validates: Requirements 3.7; Property 12**
    """
    delay_min, delay_max = bounds
    adapter = Amazon(
        delay_min_segundos=delay_min,
        delay_max_segundos=delay_max,
        rng=lambda _a, _b: fora,  # rng adversarial
        sleep=lambda _s: None,
        buscar_pagina=lambda _url: PaginaAmazon(html="", status_code=None),
    )

    atraso = adapter.calcular_atraso()

    assert delay_min <= atraso <= delay_max


# ===========================================================================
# 2) Testes mockados de parsing e bloqueio/CAPTCHA (R6.2, R3.7)
# ===========================================================================

ASIN = "B0ABCDEFGH"

# HTML representativo de uma pagina de produto da Amazon, construido para casar
# com os seletores centralizados em ``_SELETORES`` (amazon.py). Preco em
# ``priceblock_ourprice``, titulo em ``productTitle``, nota "4,6 de 5 estrelas",
# numero de avaliacoes em ``acrCustomerReviewText`` e "Vendido/Enviado por"
# apontando para a Amazon.
SAMPLE_HTML = """
<html><head><title>Produto</title></head><body>
  <span id="productTitle">  Echo Dot 5a geracao  </span>
  <div class="a-price">
    <span id="priceblock_ourprice">R$ 1.234,56</span>
  </div>
  <div id="averageCustomerReviews">
    <span id="acrPopover" title="4,6 de 5 estrelas"></span>
    <span class="a-icon-alt">4,6 de 5 estrelas</span>
    <span id="acrCustomerReviewText">1.234 avaliacoes</span>
  </div>
  <div id="tabular-buybox">
    <span>Vendido por <a href="#">Amazon.com.br</a></span>
    <span>Enviado por <a href="#">Amazon.com.br</a></span>
  </div>
</body></html>
"""

# HTML de produto vendido por TERCEIRO (nao a Amazon).
SAMPLE_HTML_TERCEIRO = """
<html><body>
  <span id="productTitle">Produto Terceiro</span>
  <span class="a-offscreen">R$ 99,90</span>
  <span class="a-icon-alt">3,2 de 5 estrelas</span>
  <span id="acrCustomerReviewText">57 avaliacoes</span>
  <div id="tabular-buybox">
    <span>Vendido por <a href="#">Loja XPTO LTDA</a></span>
    <span>Enviado por <a href="#">Loja XPTO LTDA</a></span>
  </div>
</body></html>
"""

# Pagina de CAPTCHA / bloqueio ("Robot Check").
SAMPLE_HTML_BLOQUEIO = """
<html><head><title>Robot Check</title></head><body>
  <h4>Enter the characters you see below</h4>
  <p>Sorry, we just need to make sure you're not a robot.</p>
</body></html>
"""

# HTML sem os seletores de preco/nota/avaliacoes/vendedor.
SAMPLE_HTML_SEM_SELETORES = """
<html><body><div id="conteudo">pagina sem os campos esperados</div></body></html>
"""


def _fazer_adapter(
    *,
    html: str = "",
    status_code=None,
    registrar_sleeps: list | None = None,
    fetcher_erro: Exception | None = None,
) -> Amazon:
    """Cria um :class:`Amazon` com fetcher fake e sleep no-op (nunca dorme).

    Se ``fetcher_erro`` for dado, o fetcher levanta essa excecao (para exercitar
    o isolamento de erro). Caso contrario devolve a :class:`PaginaAmazon` com o
    ``html``/``status_code`` informados. ``registrar_sleeps`` (se dado) coleta os
    valores passados ao ``sleep`` para verificar que nunca dormimos de verdade.
    """

    def _sleep(segundos: float) -> None:
        if registrar_sleeps is not None:
            registrar_sleeps.append(segundos)

    def _fetch(_url: str) -> PaginaAmazon:
        if fetcher_erro is not None:
            raise fetcher_erro
        return PaginaAmazon(html=html, status_code=status_code)

    return Amazon(
        delay_min_segundos=2.0,
        delay_max_segundos=8.0,
        sleep=_sleep,
        buscar_pagina=_fetch,
    )


# --- Parsing feliz ---------------------------------------------------------


def test_buscar_preco_parseia_preco_decimal_e_nome() -> None:
    """buscar_preco: preco como Decimal exato + nome do produto (R6.2)."""
    adapter = _fazer_adapter(html=SAMPLE_HTML)

    resultado = adapter.buscar_preco(ASIN)

    assert resultado.sucesso is True
    assert resultado.preco == Decimal("1234.56")
    assert resultado.moeda == "BRL"
    assert resultado.nome_produto == "Echo Dot 5a geracao"
    assert resultado.erro is None


def test_buscar_reputacao_mapeia_nota_num_e_vendido_por_amazon() -> None:
    """buscar_reputacao_vendedor: nota, num_avaliacoes e vendido_por_amazon (R6.2)."""
    adapter = _fazer_adapter(html=SAMPLE_HTML)

    resultado = adapter.buscar_reputacao_vendedor(ASIN)

    assert resultado.sucesso is True
    assert resultado.amz_nota_media == 4.6
    assert resultado.amz_num_avaliacoes == 1234
    assert resultado.amz_vendido_por_amazon is True
    assert resultado.sinais_indisponiveis is False
    assert resultado.erro is None


def test_buscar_reputacao_detecta_vendedor_terceiro() -> None:
    """Vendedor identificado que nao e a Amazon -> amz_vendido_por_amazon False."""
    adapter = _fazer_adapter(html=SAMPLE_HTML_TERCEIRO)

    resultado = adapter.buscar_reputacao_vendedor(ASIN)

    assert resultado.sucesso is True
    assert resultado.amz_nota_media == 3.2
    assert resultado.amz_num_avaliacoes == 57
    assert resultado.amz_vendido_por_amazon is False
    assert resultado.sinais_indisponiveis is False


def test_buscar_preco_usa_a_offscreen_como_fallback() -> None:
    """Layout moderno (a-offscreen) tambem e parseado para preco."""
    adapter = _fazer_adapter(html=SAMPLE_HTML_TERCEIRO)

    resultado = adapter.buscar_preco(ASIN)

    assert resultado.sucesso is True
    assert resultado.preco == Decimal("99.90")


# --- Bloqueio / CAPTCHA ----------------------------------------------------


def test_buscar_preco_bloqueio_por_marcador_robot_check() -> None:
    """HTML com marcador de bloqueio -> sucesso=False, sem lancar (R3.7)."""
    adapter = _fazer_adapter(html=SAMPLE_HTML_BLOQUEIO)

    resultado = adapter.buscar_preco(ASIN)

    assert resultado.sucesso is False
    assert resultado.preco is None
    assert resultado.erro is not None and resultado.erro != ""


def test_buscar_reputacao_bloqueio_por_marcador() -> None:
    """buscar_reputacao tambem desiste em bloqueio (R3.7)."""
    adapter = _fazer_adapter(html=SAMPLE_HTML_BLOQUEIO)

    resultado = adapter.buscar_reputacao_vendedor(ASIN)

    assert resultado.sucesso is False
    assert resultado.erro is not None and resultado.erro != ""


def test_buscar_preco_bloqueio_por_status_503() -> None:
    """status_code 503 -> bloqueio -> sucesso=False mesmo com HTML valido."""
    adapter = _fazer_adapter(html=SAMPLE_HTML, status_code=503)

    resultado = adapter.buscar_preco(ASIN)

    assert resultado.sucesso is False
    assert resultado.erro is not None and resultado.erro != ""


def test_buscar_reputacao_bloqueio_por_status_503() -> None:
    """status_code 503 -> bloqueio na reputacao -> sucesso=False."""
    adapter = _fazer_adapter(html=SAMPLE_HTML, status_code=503)

    resultado = adapter.buscar_reputacao_vendedor(ASIN)

    assert resultado.sucesso is False
    assert resultado.erro is not None and resultado.erro != ""


# --- Seletores ausentes ----------------------------------------------------


def test_buscar_preco_sem_seletor_de_preco_e_indisponivel() -> None:
    """Preco ausente -> sucesso=False (indisponivel), sem crash (R6.2/R6.5)."""
    adapter = _fazer_adapter(html=SAMPLE_HTML_SEM_SELETORES)

    resultado = adapter.buscar_preco(ASIN)

    assert resultado.sucesso is False
    assert resultado.preco is None
    assert resultado.erro is not None and resultado.erro != ""


def test_buscar_reputacao_sem_seletores_sinais_indisponiveis() -> None:
    """Sem seletores -> sucesso=True mas sinais_indisponiveis=True e campos None."""
    adapter = _fazer_adapter(html=SAMPLE_HTML_SEM_SELETORES)

    resultado = adapter.buscar_reputacao_vendedor(ASIN)

    assert resultado.sucesso is True
    assert resultado.sinais_indisponiveis is True
    assert resultado.amz_nota_media is None
    assert resultado.amz_num_avaliacoes is None
    assert resultado.amz_vendido_por_amazon is None
    assert resultado.erro is None


# --- Isolamento de erro do fetcher e ausencia de sleep real ----------------


def test_fetcher_excecao_e_isolada_em_buscar_preco() -> None:
    """Excecao no fetcher -> capturada -> sucesso=False (nunca propaga) (R3.3)."""
    adapter = _fazer_adapter(fetcher_erro=RuntimeError("timeout de rede"))

    resultado = adapter.buscar_preco(ASIN)

    assert resultado.sucesso is False
    assert resultado.erro is not None and "timeout de rede" in resultado.erro


def test_fetcher_excecao_e_isolada_em_buscar_reputacao() -> None:
    """Excecao no fetcher -> capturada -> sucesso=False na reputacao (R6.6)."""
    adapter = _fazer_adapter(fetcher_erro=RuntimeError("boom"))

    resultado = adapter.buscar_reputacao_vendedor(ASIN)

    assert resultado.sucesso is False
    assert resultado.erro is not None and resultado.erro != ""


def test_nunca_dorme_de_verdade_mas_aplica_atraso() -> None:
    """O sleep injetado (no-op) e chamado com um atraso em [min, max] (R3.7).

    Verifica que a consulta aplica o atraso (sleep chamado) sem jamais dormir de
    verdade — o sleep injetado apenas registra o valor.
    """
    sleeps: list[float] = []
    adapter = _fazer_adapter(html=SAMPLE_HTML, registrar_sleeps=sleeps)

    adapter.buscar_preco(ASIN)

    assert len(sleeps) == 1
    assert 2.0 <= sleeps[0] <= 8.0


def test_site_e_amazon() -> None:
    """Sanidade: o adapter declara Site.AMAZON."""
    assert Amazon.site == Site.AMAZON
