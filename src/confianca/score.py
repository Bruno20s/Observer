"""Score de confianca deterministico do vendedor (`src/confianca/score.py`).

Implementa a formula deterministica **0-100** por site descrita no design
("Formula deterministica do Score_Confianca" -> R6.4), sem LLM e sem I/O. O
resultado e um inteiro em ``[0, 100]`` quando todos os sinais **obrigatorios**
do site estao presentes e validos; quando um sinal obrigatorio esta ausente ou
invalido, o resultado e a string sentinela :data:`INDISPONIVEL` (R6.5).

Propriedades garantidas (ver design, Property 9):

- **Bounds.** Sinais completos e validos -> valor em ``[0, 100]`` (via ``clamp``).
- **Indisponibilidade.** Qualquer sinal obrigatorio ``None``/invalido, ou
  ``ReputacaoResult.sinais_indisponiveis is True``, ou ``sucesso is False`` ->
  :data:`INDISPONIVEL`.
- **Determinismo.** Mesma entrada -> mesmo resultado. Funcao pura: sem
  aleatoriedade, sem I/O, sem estado global mutavel.

Todos os coeficientes, tabelas de peso e mapeamentos vivem em CONSTANTES de
modulo (nada de numeros magicos inline), para facilitar ajuste conforme o
design.
"""

from __future__ import annotations

from typing import Union

from src.adapters.base import ReputacaoResult, Site

# ---------------------------------------------------------------------------
# Sentinela de indisponibilidade (R6.5). O valor casa com o que
# ``HistoricoReputacao.score_confianca`` armazena ("indisponivel").
# ---------------------------------------------------------------------------
INDISPONIVEL: str = "indisponivel"

#: Tipo de retorno do entrypoint: um inteiro em [0,100] ou a sentinela.
ScoreResult = Union[int, str]

# ---------------------------------------------------------------------------
# Limites do score normalizado.
# ---------------------------------------------------------------------------
SCORE_MIN: int = 0
SCORE_MAX: int = 100

# ===========================================================================
# Mercado Livre
#   score_ml = 0.5*pontos_nivel + 0.3*pontos_selo + 0.2*pontos_reclamacoes
# ===========================================================================

#: Pesos da combinacao linear do ML (somam 1.0).
PESO_ML_NIVEL: float = 0.5
PESO_ML_SELO: float = 0.3
PESO_ML_RECLAMACOES: float = 0.2

#: Mapeamento discreto do ``level_id`` -> pontos (0-100).
PONTOS_NIVEL_ML: dict[str, float] = {
    "1_red": 0.0,
    "2_orange": 25.0,
    "3_yellow": 50.0,
    "4_light_green": 75.0,
    "5_green": 100.0,
}

#: Mapeamento do selo MercadoLider -> pontos (0-100).
#: As chaves canonicas casam com os valores de ``ReputacaoResult.ml_selo_mercadolider``
#: ("ausente"|"mercadolider"|"gold"|"platinum"). Aceitamos tambem os nomes
#: longos usados no texto do design como aliases equivalentes.
PONTOS_SELO_ML: dict[str, float] = {
    "ausente": 0.0,
    "mercadolider": 60.0,
    "gold": 80.0,
    "mercadolider_gold": 80.0,
    "platinum": 100.0,
    "mercadolider_platinum": 100.0,
}

#: Base a partir da qual o percentual de reclamacoes e subtraido.
PONTOS_RECLAMACOES_BASE_ML: float = 100.0
#: Limites validos do percentual de reclamacoes (0..100).
PERCENTUAL_RECLAMACOES_MIN: float = 0.0
PERCENTUAL_RECLAMACOES_MAX: float = 100.0

# ===========================================================================
# Amazon
#   score_amz = 0.6*pontos_nota + 0.2*pontos_volume + 0.2*pontos_vendedor
# ===========================================================================

#: Pesos da combinacao linear da Amazon (somam 1.0).
PESO_AMZ_NOTA: float = 0.6
PESO_AMZ_VOLUME: float = 0.2
PESO_AMZ_VENDEDOR: float = 0.2

#: Nota media varia de 0.0 a 5.0; normalizamos para 0-100 dividindo pela maxima.
NOTA_MEDIA_MAX: float = 5.0
NOTA_MEDIA_MIN: float = 0.0
PONTOS_NOTA_ESCALA: float = 100.0

#: Volume de avaliacoes satura em 1000 (1000+ avaliacoes = 100 pontos).
VOLUME_AVALIACOES_SATURACAO: int = 1000
PONTOS_VOLUME_ESCALA: float = 100.0

#: Pontos por responsavel pela venda/entrega.
PONTOS_VENDEDOR_AMAZON: float = 100.0   # vendido e entregue pela Amazon
PONTOS_VENDEDOR_TERCEIRO: float = 50.0  # vendedor terceiro


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clamp(valor: float) -> int:
    """Limita ``valor`` a ``[SCORE_MIN, SCORE_MAX]`` e arredonda para inteiro.

    O arredondamento acontece antes do clamp final para garantir que o
    resultado esteja sempre dentro da faixa mesmo apos arredondar.
    """
    arredondado = round(valor)
    if arredondado < SCORE_MIN:
        return SCORE_MIN
    if arredondado > SCORE_MAX:
        return SCORE_MAX
    return arredondado


def _clamp_faixa(valor: float, minimo: float, maximo: float) -> float:
    """Limita ``valor`` a ``[minimo, maximo]`` (sem arredondar)."""
    if valor < minimo:
        return minimo
    if valor > maximo:
        return maximo
    return valor


# ---------------------------------------------------------------------------
# Score por site
# ---------------------------------------------------------------------------
def calcular_score_mercado_livre(reputacao: ReputacaoResult) -> ScoreResult:
    """Score de confianca do Mercado Livre (0-100) ou :data:`INDISPONIVEL`.

    Sinais obrigatorios: ``ml_level_id``, ``ml_selo_mercadolider`` e
    ``ml_percentual_reclamacoes``. Qualquer um ausente/invalido -> indisponivel.
    """
    level_id = reputacao.ml_level_id
    selo = reputacao.ml_selo_mercadolider
    percentual = reputacao.ml_percentual_reclamacoes

    # Sinais obrigatorios ausentes.
    if level_id is None or selo is None or percentual is None:
        return INDISPONIVEL

    # Valores fora dos dominios mapeados/validos sao tratados como invalidos.
    if level_id not in PONTOS_NIVEL_ML:
        return INDISPONIVEL
    if selo not in PONTOS_SELO_ML:
        return INDISPONIVEL
    if not (PERCENTUAL_RECLAMACOES_MIN <= percentual <= PERCENTUAL_RECLAMACOES_MAX):
        return INDISPONIVEL

    pontos_nivel = PONTOS_NIVEL_ML[level_id]
    pontos_selo = PONTOS_SELO_ML[selo]
    pontos_reclamacoes = _clamp_faixa(
        PONTOS_RECLAMACOES_BASE_ML - percentual,
        SCORE_MIN,
        SCORE_MAX,
    )

    score = (
        PESO_ML_NIVEL * pontos_nivel
        + PESO_ML_SELO * pontos_selo
        + PESO_ML_RECLAMACOES * pontos_reclamacoes
    )
    return _clamp(score)


def calcular_score_amazon(reputacao: ReputacaoResult) -> ScoreResult:
    """Score de confianca da Amazon (0-100) ou :data:`INDISPONIVEL`.

    Sinais obrigatorios: ``amz_nota_media``, ``amz_num_avaliacoes`` e
    ``amz_vendido_por_amazon``. Qualquer um ausente/invalido -> indisponivel.
    """
    nota = reputacao.amz_nota_media
    num_avaliacoes = reputacao.amz_num_avaliacoes
    vendido_por_amazon = reputacao.amz_vendido_por_amazon

    # Sinais obrigatorios ausentes.
    if nota is None or num_avaliacoes is None or vendido_por_amazon is None:
        return INDISPONIVEL

    # Valores fora do dominio valido sao tratados como invalidos.
    if not (NOTA_MEDIA_MIN <= nota <= NOTA_MEDIA_MAX):
        return INDISPONIVEL
    if num_avaliacoes < 0:
        return INDISPONIVEL

    pontos_nota = (nota / NOTA_MEDIA_MAX) * PONTOS_NOTA_ESCALA
    pontos_volume = (
        min(num_avaliacoes, VOLUME_AVALIACOES_SATURACAO)
        / VOLUME_AVALIACOES_SATURACAO
        * PONTOS_VOLUME_ESCALA
    )
    pontos_vendedor = (
        PONTOS_VENDEDOR_AMAZON if vendido_por_amazon else PONTOS_VENDEDOR_TERCEIRO
    )

    score = (
        PESO_AMZ_NOTA * pontos_nota
        + PESO_AMZ_VOLUME * pontos_volume
        + PESO_AMZ_VENDEDOR * pontos_vendedor
    )
    return _clamp(score)


# ---------------------------------------------------------------------------
# Entrypoint com dispatch por site
# ---------------------------------------------------------------------------
def calcular_score(reputacao: ReputacaoResult, site: Site) -> ScoreResult:
    """Calcula o Score_Confianca de um ``ReputacaoResult`` dado o ``site``.

    Retorna um inteiro em ``[0, 100]`` quando todos os sinais obrigatorios do
    site estao presentes e validos; retorna :data:`INDISPONIVEL` quando:

    - a consulta falhou (``reputacao.sucesso is False``); ou
    - ``reputacao.sinais_indisponiveis is True``; ou
    - algum sinal obrigatorio do site esta ausente/invalido; ou
    - ``site`` nao possui formula definida.

    Funcao pura e deterministica: mesma entrada -> mesmo resultado.
    """
    # Consulta sem sucesso ou sinais marcados como indisponiveis (R6.5/R6.6).
    if not reputacao.sucesso or reputacao.sinais_indisponiveis:
        return INDISPONIVEL

    if site == Site.MERCADO_LIVRE:
        return calcular_score_mercado_livre(reputacao)
    if site == Site.AMAZON:
        return calcular_score_amazon(reputacao)

    # Site sem formula definida -> indisponivel (nunca lanca).
    return INDISPONIVEL
