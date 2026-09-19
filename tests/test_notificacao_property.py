"""Testes property-based da notificacao por e-mail (task 10.3).

Cobre duas propriedades do design:

- **Property 7** — calculo correto do percentual de variacao
  (`email.calcular_percentual`, R5.2).
- **Property 8** — conteudo completo da mensagem de notificacao
  (`email.montar_mensagem`, R5.1, R5.3, R5.4, R5.5).

As assertivas de conteudo textual sao feitas contra as CONSTANTES exportadas do
modulo (``CABECALHO_COMPARACAO``, ``REPUTACAO_INDISPONIVEL``, etc.) e nao contra
literais, de modo que os testes permanecam robustos a ajustes de redacao — mas o
design ("Formato da mensagem") define a redacao acentuada dessas constantes.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from src.adapters.base import ReputacaoResult, Site
from src.notificacao.email import (
    CABECALHO_COMPARACAO,
    NOME_SITE,
    REPUTACAO_INDISPONIVEL,
    ROTULO_IGUAL,
    ROTULO_QUEDA,
    ROTULO_SUBIDA,
    PrecoSiteComparacao,
    calcular_percentual,
    montar_mensagem,
)

_DUAS_CASAS = Decimal("0.01")


# ---------------------------------------------------------------------------
# Geradores de precos Decimal.
# ---------------------------------------------------------------------------
# Precos monetarios finitos representados como Decimal a 2 casas. Restringimos a
# magnitude apenas para manter o teste rapido, sem perder generalidade.
_preco_decimal = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

# Preco anterior estritamente positivo (anterior != 0), para o ramo principal.
_preco_positivo = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("1000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


# ===========================================================================
# Property 7 — Percentual de variacao
# ===========================================================================
# Feature: rastreador-precos-mvp, Property 7: Para qualquer par (preco anterior
# > 0, preco novo) Decimal, calcular_percentual retorna exatamente
# ((novo - anterior) / anterior) * 100 arredondado a 2 casas decimais, com
# rotulo "subida" sse novo > anterior, "queda" sse novo < anterior e "igual"
# quando iguais. Para anterior == 0, disponivel e False (sem divisao por zero).


@settings(max_examples=300)
@given(anterior=_preco_positivo, novo=_preco_decimal)
def test_property_7_percentual_e_rotulo_corretos(
    anterior: Decimal, novo: Decimal
) -> None:
    """anterior > 0: percentual exato a 2 casas e rotulo coerente com a direcao.

    **Validates: Requirements 5.2; Property 7**
    """
    resultado = calcular_percentual(anterior, novo)

    esperado = (((novo - anterior) / anterior) * Decimal(100)).quantize(
        _DUAS_CASAS, rounding=ROUND_HALF_UP
    )

    assert resultado.disponivel is True
    assert resultado.percentual == esperado

    # O rotulo indica "subida" sse o percentual (arredondado, o valor exibido) e
    # positivo e "queda" sse negativo (design: "Formato da mensagem" / Property
    # 7). Uma variacao de precos que arredonda a 0.00% e reportada como "igual".
    if esperado > 0:
        assert resultado.rotulo == ROTULO_SUBIDA
    elif esperado < 0:
        assert resultado.rotulo == ROTULO_QUEDA
    else:
        assert resultado.rotulo == ROTULO_IGUAL


@settings(max_examples=200)
@given(novo=_preco_decimal)
def test_property_7_anterior_zero_indisponivel(novo: Decimal) -> None:
    """anterior == 0: disponivel=False, percentual=None (sem divisao por zero).

    **Validates: Requirements 5.2; Property 7**
    """
    resultado = calcular_percentual(Decimal("0"), novo)

    assert resultado.disponivel is False
    assert resultado.percentual is None


# ===========================================================================
# Property 8 — Conteudo completo da mensagem
# ===========================================================================
# Feature: rastreador-precos-mvp, Property 8: Para quaisquer entradas validas, a
# saida de montar_mensagem SEMPRE contem o nome do produto, ambos os precos, o
# percentual e o link; quando ha >= 2 entradas ProdutoSite de comparacao, contem
# o cabecalho de comparacao e cada site; e contem um resumo de reputacao ou o
# texto de "reputacao indisponivel" quando os sinais estao indisponiveis.

# Nome de produto nao vazio (sem quebras de linha, que confundiriam o layout).
_nome_produto = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Cs"), blacklist_characters="\n\r"),
    min_size=1,
    max_size=60,
).filter(lambda s: s.strip() != "")

# URL simples nao vazia (sem espacos/quebras).
_url = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="/:.-_?=&",
    ),
    min_size=5,
    max_size=80,
).map(lambda s: "https://exemplo.com/" + s)


# ReputacaoResult valido de Mercado Livre (sinais presentes).
_reputacao_ml_valida = st.builds(
    ReputacaoResult,
    sucesso=st.just(True),
    ml_level_id=st.sampled_from(["3_yellow", "4_light_green", "5_green"]),
    ml_selo_mercadolider=st.sampled_from(["ausente", "mercadolider", "gold", "platinum"]),
    ml_percentual_reclamacoes=st.floats(
        min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False
    ),
    sinais_indisponiveis=st.just(False),
)

# ReputacaoResult com sinais indisponiveis (via flag OU sucesso=False OU None).
_reputacao_indisponivel = st.one_of(
    st.none(),
    st.builds(
        ReputacaoResult,
        sucesso=st.just(True),
        sinais_indisponiveis=st.just(True),
    ),
    st.builds(
        ReputacaoResult,
        sucesso=st.just(False),
        sinais_indisponiveis=st.just(False),
    ),
)


@settings(max_examples=200)
@given(
    nome=_nome_produto,
    anterior=_preco_positivo,
    novo=_preco_decimal,
    url=_url,
)
def test_property_8_mensagem_contem_campos_obrigatorios(
    nome: str, anterior: Decimal, novo: Decimal, url: str
) -> None:
    """Mensagem sempre contem nome, ambos os precos, o percentual e o link.

    **Validates: Requirements 5.1; Property 8**
    """
    variacao = calcular_percentual(anterior, novo)

    mensagem = montar_mensagem(
        nome_produto=nome,
        preco_anterior=anterior,
        preco_novo=novo,
        url_original=url,
        site=Site.MERCADO_LIVRE,
    )

    # Nome do produto e link presentes literalmente.
    assert nome in mensagem
    assert url in mensagem

    # Ambos os precos (a 2 casas) presentes.
    anterior_fmt = anterior.quantize(_DUAS_CASAS, rounding=ROUND_HALF_UP)
    novo_fmt = novo.quantize(_DUAS_CASAS, rounding=ROUND_HALF_UP)
    assert str(anterior_fmt) in mensagem
    assert str(novo_fmt) in mensagem

    # Percentual presente (anterior > 0 garante disponibilidade).
    assert variacao.percentual is not None
    assert str(variacao.percentual) in mensagem


@settings(max_examples=200)
@given(
    nome=_nome_produto,
    anterior=_preco_positivo,
    novo=_preco_decimal,
    url=_url,
    preco_ml=st.one_of(st.none(), _preco_decimal),
    preco_amz=st.one_of(st.none(), _preco_decimal),
)
def test_property_8_comparacao_entre_sites(
    nome: str,
    anterior: Decimal,
    novo: Decimal,
    url: str,
    preco_ml,
    preco_amz,
) -> None:
    """Com >= 2 entradas ProdutoSite: cabecalho de comparacao e cada site.

    **Validates: Requirements 5.3; Property 8**
    """
    precos_por_site = [
        PrecoSiteComparacao(site=Site.MERCADO_LIVRE, preco=preco_ml),
        PrecoSiteComparacao(site=Site.AMAZON, preco=preco_amz),
    ]

    mensagem = montar_mensagem(
        nome_produto=nome,
        preco_anterior=anterior,
        preco_novo=novo,
        url_original=url,
        site=Site.MERCADO_LIVRE,
        precos_por_site=precos_por_site,
    )

    # Cabecalho de comparacao presente (assertiva contra a constante exportada).
    assert CABECALHO_COMPARACAO in mensagem
    # Cada site identificado pelo seu nome legivel.
    assert NOME_SITE[Site.MERCADO_LIVRE] in mensagem
    assert NOME_SITE[Site.AMAZON] in mensagem


@settings(max_examples=100)
@given(
    nome=_nome_produto,
    anterior=_preco_positivo,
    novo=_preco_decimal,
    url=_url,
)
def test_property_8_reputacao_indisponivel(
    nome: str, anterior: Decimal, novo: Decimal, url: str
) -> None:
    """Uma unica entrada de site: sem cabecalho de comparacao.

    **Validates: Requirements 5.3; Property 8**
    """
    precos_por_site = [PrecoSiteComparacao(site=Site.MERCADO_LIVRE, preco=novo)]

    mensagem = montar_mensagem(
        nome_produto=nome,
        preco_anterior=anterior,
        preco_novo=novo,
        url_original=url,
        site=Site.MERCADO_LIVRE,
        precos_por_site=precos_por_site,
    )

    assert CABECALHO_COMPARACAO not in mensagem


@settings(max_examples=200)
@given(
    nome=_nome_produto,
    anterior=_preco_positivo,
    novo=_preco_decimal,
    url=_url,
    reputacao=_reputacao_indisponivel,
)
def test_property_8_reputacao_indisponivel_texto(
    nome: str, anterior: Decimal, novo: Decimal, url: str, reputacao
) -> None:
    """Sinais de reputacao indisponiveis: mensagem contem o texto indisponivel.

    **Validates: Requirements 5.4, 5.5; Property 8**
    """
    mensagem = montar_mensagem(
        nome_produto=nome,
        preco_anterior=anterior,
        preco_novo=novo,
        url_original=url,
        site=Site.MERCADO_LIVRE,
        reputacao=reputacao,
    )

    assert REPUTACAO_INDISPONIVEL in mensagem


@settings(max_examples=200)
@given(
    nome=_nome_produto,
    anterior=_preco_positivo,
    novo=_preco_decimal,
    url=_url,
    reputacao=_reputacao_ml_valida,
)
def test_property_8_reputacao_disponivel_resumo(
    nome: str, anterior: Decimal, novo: Decimal, url: str, reputacao: ReputacaoResult
) -> None:
    """Sinais validos: mensagem contem um resumo de reputacao (nao o indisponivel).

    Com sinais ML validos o resumo inclui pelo menos o nivel do vendedor.

    **Validates: Requirements 5.4; Property 8**
    """
    mensagem = montar_mensagem(
        nome_produto=nome,
        preco_anterior=anterior,
        preco_novo=novo,
        url_original=url,
        site=Site.MERCADO_LIVRE,
        reputacao=reputacao,
    )

    # O resumo (nivel do ML) aparece; o texto de indisponivel nao.
    assert reputacao.ml_level_id in mensagem
    assert REPUTACAO_INDISPONIVEL not in mensagem
