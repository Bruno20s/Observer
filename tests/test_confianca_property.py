"""Teste property-based da Property 9 do score de confianca.

# Feature: rastreador-precos-mvp, Property 9: Para qualquer conjunto de sinais
# de reputacao de um site: se todos os sinais obrigatorios estiverem presentes e
# validos, o Score_Confianca e um valor em [0, 100]; se algum sinal obrigatorio
# estiver ausente ou invalido, o resultado e "indisponivel". Alem disso, para
# uma mesma entrada, o calculo produz sempre o mesmo resultado (determinismo,
# sem LLM).

Este teste exercita `src.confianca.score.calcular_score` (dispatch por site) e
cobre as tres partes da Property 9:

1. **Bounds.** Quando todos os sinais obrigatorios do site estao presentes e
   dentro do dominio valido, `calcular_score` retorna um `int` em ``[0, 100]``.
2. **Indisponibilidade.** Quando um sinal obrigatorio esta ausente (``None``),
   ou ``sinais_indisponiveis is True``, ou ``sucesso is False``, o resultado e
   a sentinela :data:`INDISPONIVEL`.
3. **Determinismo.** A mesma entrada produz sempre o mesmo resultado (chamar
   duas vezes -> resultados iguais).

Os geradores restringem os sinais ao espaco de entrada valido de cada site
(niveis/selos do conjunto mapeado no ML; nota 0..5, avaliacoes >= 0, booleano na
Amazon), conforme o design ("Formula deterministica do Score_Confianca").

**Validates: Requirements 6.4, 6.5; Property 9**
"""

from __future__ import annotations

from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from src.adapters.base import ReputacaoResult, Site
from src.confianca.score import (
    INDISPONIVEL,
    PONTOS_NIVEL_ML,
    PONTOS_SELO_ML,
    calcular_score,
)

# ---------------------------------------------------------------------------
# Geradores de sinais validos por site.
# ---------------------------------------------------------------------------

# Level ids validos: exatamente as chaves mapeadas em PONTOS_NIVEL_ML.
_ml_level_id = st.sampled_from(sorted(PONTOS_NIVEL_ML.keys()))

# Selos validos: as chaves mapeadas em PONTOS_SELO_ML (inclui aliases longos).
_ml_selo = st.sampled_from(sorted(PONTOS_SELO_ML.keys()))

# Percentual de reclamacoes: float finito em [0, 100].
_ml_percentual = st.floats(
    min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False
)

# Nota media Amazon: float finito em [0.0, 5.0].
_amz_nota = st.floats(
    min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False
)

# Numero de avaliacoes: inteiro >= 0 (limitado apenas para manter o teste rapido).
_amz_num_avaliacoes = st.integers(min_value=0, max_value=1_000_000)


@st.composite
def _reputacao_ml_valida(draw) -> ReputacaoResult:
    """ReputacaoResult valido de Mercado Livre (todos os sinais obrigatorios)."""
    return ReputacaoResult(
        sucesso=True,
        ml_level_id=draw(_ml_level_id),
        ml_selo_mercadolider=draw(_ml_selo),
        ml_percentual_reclamacoes=draw(_ml_percentual),
        sinais_indisponiveis=False,
    )


@st.composite
def _reputacao_amz_valida(draw) -> ReputacaoResult:
    """ReputacaoResult valido de Amazon (todos os sinais obrigatorios)."""
    return ReputacaoResult(
        sucesso=True,
        amz_nota_media=draw(_amz_nota),
        amz_num_avaliacoes=draw(_amz_num_avaliacoes),
        amz_vendido_por_amazon=draw(st.booleans()),
        sinais_indisponiveis=False,
    )


# Um par (site, reputacao valida) para os dois sites suportados.
_site_reputacao_valida = st.one_of(
    _reputacao_ml_valida().map(lambda r: (Site.MERCADO_LIVRE, r)),
    _reputacao_amz_valida().map(lambda r: (Site.AMAZON, r)),
)


# Nomes dos campos obrigatorios por site, para "apagar" (None) exatamente um.
_CAMPOS_OBRIGATORIOS = {
    Site.MERCADO_LIVRE: (
        "ml_level_id",
        "ml_selo_mercadolider",
        "ml_percentual_reclamacoes",
    ),
    Site.AMAZON: (
        "amz_nota_media",
        "amz_num_avaliacoes",
        "amz_vendido_por_amazon",
    ),
}


# ===========================================================================
# Parte 1 — Bounds: sinais validos completos -> int em [0, 100].
# ===========================================================================
@settings(max_examples=200)
@given(par=_site_reputacao_valida)
def test_property_9_bounds_score_em_0_100(par: tuple[Site, ReputacaoResult]) -> None:
    """Sinais obrigatorios presentes e validos -> int normalizado em [0, 100].

    **Validates: Requirements 6.4; Property 9**
    """
    site, reputacao = par

    resultado = calcular_score(reputacao, site)

    assert isinstance(resultado, int)
    assert 0 <= resultado <= 100


# ===========================================================================
# Parte 2 — Indisponibilidade: sinal ausente / flag / sucesso=False -> INDISPONIVEL.
# ===========================================================================
@settings(max_examples=200)
@given(par=_site_reputacao_valida, data=st.data())
def test_property_9_sinal_obrigatorio_ausente_indisponivel(
    par: tuple[Site, ReputacaoResult], data: st.DataObject
) -> None:
    """Um sinal obrigatorio ``None`` -> INDISPONIVEL (partindo de entrada valida).

    **Validates: Requirements 6.5; Property 9**
    """
    site, reputacao = par
    campo = data.draw(st.sampled_from(_CAMPOS_OBRIGATORIOS[site]))

    reputacao_faltante = replace(reputacao, **{campo: None})

    assert calcular_score(reputacao_faltante, site) == INDISPONIVEL


@settings(max_examples=100)
@given(par=_site_reputacao_valida)
def test_property_9_sinais_indisponiveis_flag_indisponivel(
    par: tuple[Site, ReputacaoResult],
) -> None:
    """``sinais_indisponiveis=True`` -> INDISPONIVEL mesmo com sinais presentes.

    **Validates: Requirements 6.5; Property 9**
    """
    site, reputacao = par

    reputacao_marcada = replace(reputacao, sinais_indisponiveis=True)

    assert calcular_score(reputacao_marcada, site) == INDISPONIVEL


@settings(max_examples=100)
@given(par=_site_reputacao_valida)
def test_property_9_sucesso_falso_indisponivel(
    par: tuple[Site, ReputacaoResult],
) -> None:
    """``sucesso=False`` -> INDISPONIVEL mesmo com sinais presentes.

    **Validates: Requirements 6.5; Property 9**
    """
    site, reputacao = par

    reputacao_falha = replace(reputacao, sucesso=False)

    assert calcular_score(reputacao_falha, site) == INDISPONIVEL


# ===========================================================================
# Parte 3 — Determinismo: mesma entrada -> mesmo resultado.
# ===========================================================================

# Gera qualquer ReputacaoResult (valido OU nao) para exercitar o determinismo
# em toda a superficie da funcao, incluindo caminhos que retornam INDISPONIVEL.
_reputacao_qualquer = st.builds(
    ReputacaoResult,
    sucesso=st.booleans(),
    ml_level_id=st.one_of(st.none(), st.text(max_size=12)),
    ml_selo_mercadolider=st.one_of(st.none(), st.text(max_size=12)),
    ml_percentual_reclamacoes=st.one_of(
        st.none(),
        st.floats(min_value=-50.0, max_value=150.0, allow_nan=False, allow_infinity=False),
    ),
    amz_nota_media=st.one_of(
        st.none(),
        st.floats(min_value=-1.0, max_value=6.0, allow_nan=False, allow_infinity=False),
    ),
    amz_num_avaliacoes=st.one_of(st.none(), st.integers(min_value=-10, max_value=1_000_000)),
    amz_vendido_por_amazon=st.one_of(st.none(), st.booleans()),
    sinais_indisponiveis=st.booleans(),
)


@settings(max_examples=200)
@given(
    reputacao=_reputacao_qualquer,
    site=st.sampled_from([Site.MERCADO_LIVRE, Site.AMAZON]),
)
def test_property_9_determinismo(reputacao: ReputacaoResult, site: Site) -> None:
    """Mesma entrada -> mesmo resultado (chamar duas vezes produz igualdade).

    **Validates: Requirements 6.4; Property 9**
    """
    primeiro = calcular_score(reputacao, site)
    segundo = calcular_score(reputacao, site)

    assert primeiro == segundo
