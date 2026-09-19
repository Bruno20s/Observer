"""Adapter da Amazon (MVP).

Este modulo implementa o adapter isolado da Amazon, conforme a secao
"Adapter Amazon" e "Rate limiting e bloqueio da Amazon" do design. Nenhum
adapter importa outro adapter.

Nesta etapa estao implementados:

- :meth:`Amazon.parse_url` (task 5.1) — extracao do ASIN.
- O **gerador de atraso aleatorio** configuravel (task 8.1) e a **estrategia de
  rate limiting / CAPTCHA** (task 8.1): antes de cada requisicao Amazon aplica-se
  um atraso aleatorio no intervalo configurado (default 2-8s, R3.7); ao detectar
  CAPTCHA/bloqueio, o adapter **loga e desiste do item no ciclo atual**,
  retornando ``sucesso=False`` (nunca insiste imediatamente).
- A **extracao real via Playwright** (task 8.2) de :meth:`buscar_preco` e
  :meth:`buscar_reputacao_vendedor`. O conteudo da Amazon e renderizado via JS,
  entao a busca da pagina usa Playwright (Chromium headless). Para manter o
  parsing puro/testavel e permitir mockar o browser (task 8.3), a busca da
  pagina fica atras de um **seam injetavel**: o construtor aceita um
  ``buscar_pagina`` (callable ``(url: str) -> PaginaAmazon``) cujo default e um
  fetcher baseado em Playwright (importado de forma **lazy**, para que o modulo
  importe sem o Playwright/os browsers instalados). O parsing opera sobre a
  string de HTML retornada — funcoes puras, sem I/O.

Convencoes (base.py): :meth:`buscar_preco` e :meth:`buscar_reputacao_vendedor`
NUNCA lancam por falha externa (rede/parsing/timeout/bloqueio): retornam um
resultado com ``sucesso=False`` e ``erro`` preenchido (R3.3, R6.6). Seletor
ausente NAO e erro: o sinal correspondente e marcado como indisponivel
(``sinais_indisponiveis=True`` e/ou campo ``None``), sem crash (R6.2/R6.5).

Todos os seletores CSS/XPath e padroes de extracao ficam centralizados em
:data:`_SELETORES` para facilitar a manutencao (os seletores da Amazon sao
frageis por natureza — design -> "Adapter Amazon").
"""

from __future__ import annotations

import html as _html
import logging
import random
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional
from urllib.parse import urlparse

from .base import BaseAdapter, ItemRef, PrecoResult, ReputacaoResult, Site

logger = logging.getLogger(__name__)

#: Comprimento maximo aceito para uma URL (R1.3 / Property 2).
_MAX_URL_LEN = 2048

#: ASIN em segmentos ``/dp/<ASIN>`` ou ``/gp/product/<ASIN>`` (R1.2).
#: O ASIN tem exatamente 10 caracteres alfanumericos maiusculos.
_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)")

#: Dominios aceitos: amazon.* (amazon.com.br, amazon.com, ...).
_HOST_RE = re.compile(r"(?:^|\.)amazon\.", re.IGNORECASE)

#: Base da URL de produto usada pelo fetcher default (amazon.com.br, R6.2).
_URL_PRODUTO_BASE = "https://www.amazon.com.br/dp/{asin}"

#: Atraso aleatorio padrao antes de cada requisicao Amazon (R3.7). Os defaults
#: espelham ``ParametrosOperacionais.amazon_delay_{min,max}_segundos``.
_DEFAULT_DELAY_MIN_SEGUNDOS = 2.0
_DEFAULT_DELAY_MAX_SEGUNDOS = 8.0

#: Timeout (ms) para o carregamento da pagina no fetcher Playwright default.
_DEFAULT_PLAYWRIGHT_TIMEOUT_MS = 30_000

#: User-agent realista para reduzir o risco de bloqueio (design -> Playwright
#: headless com user-agent realista).
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

#: Status HTTP que indicam rate limiting / bloqueio pela Amazon.
#: 429 (Too Many Requests) e 503 (Service Unavailable, usado pela Amazon em
#: paginas "Robot Check"/bloqueio) sao os sinais mais comuns.
_STATUS_BLOQUEIO: frozenset[int] = frozenset({429, 503})

#: Marcadores textuais de CAPTCHA / pagina de bloqueio da Amazon. Comparados de
#: forma case-insensitive contra o conteudo (HTML/texto) da pagina. Sao
#: propositalmente conservadores para evitar falsos positivos em paginas de
#: produto legitimas.
_MARCADORES_BLOQUEIO: tuple[str, ...] = (
    "enter the characters you see below",
    "type the characters you see in this image",
    "to discuss automated access to amazon data",
    "sorry, we just need to make sure you're not a robot",
    "api-services-support@amazon.com",
    "/errors/validatecaptcha",
    "captchacharacters",
    "robot check",
)

# ---------------------------------------------------------------------------
# Seletores centralizados (R6.2 / design -> "Adapter Amazon").
#
# Os seletores da Amazon sao frageis e mudam com frequencia; mante-los todos
# num unico lugar torna a manutencao trivial. Cada sinal tem uma LISTA de
# padroes candidatos, tentados em ordem — o primeiro que casar vence. Isso
# absorve as variacoes de layout (preco em ``#priceblock_ourprice``,
# ``.a-price .a-offscreen``, etc.) sem espalhar logica pelo parsing.
#
# As entradas de "preco" e "nota"/"avaliacoes" sao expressoes regulares
# aplicadas ao HTML (compiladas em ``_SELETORES_COMPILADOS``). A extracao e
# feita sobre a string de HTML (parsing puro), portanto usamos regex/heuristica
# em vez de um motor CSS de browser — o resultado e testavel sem Playwright.
# ---------------------------------------------------------------------------

#: Padroes de extracao por sinal. Cada valor e uma tupla de regexes (fonte); o
#: grupo 1 de cada regex captura o texto bruto do sinal.
_SELETORES: dict[str, tuple[str, ...]] = {
    # Preco: capturamos o texto monetario BR (ex.: "R$ 1.234,56"). Cobrimos o
    # layout moderno (a-offscreen) e os ids legados de priceblock.
    "preco": (
        r'id="priceblock_ourprice"[^>]*>\s*([^<]+)',
        r'id="priceblock_dealprice"[^>]*>\s*([^<]+)',
        r'id="priceblock_saleprice"[^>]*>\s*([^<]+)',
        r'class="a-price[^"]*"[^>]*>\s*<span class="a-offscreen">\s*([^<]+)',
        r'class="a-offscreen">\s*(R\$\s*[\d.,]+)',
    ),
    # Nome do produto (titulo).
    "nome_produto": (
        r'id="productTitle"[^>]*>\s*([^<]+)',
        r'id="title"[^>]*>\s*([^<]+)',
    ),
    # Nota media (estrelas). Ex.: "4,6 de 5 estrelas" / "4.6 out of 5 stars".
    "nota_media": (
        r'id="acrPopover"[^>]*title="\s*([\d.,]+)',
        r'([\d.,]+)\s*de\s*5\s*estrelas',
        r'([\d.,]+)\s*out of\s*5\s*stars',
        r'class="a-icon-alt">\s*([\d.,]+)\s*(?:de|out of)',
    ),
    # Numero de avaliacoes. Ex.: "1.234 avaliacoes" / "1,234 ratings".
    "num_avaliacoes": (
        r'id="acrCustomerReviewText"[^>]*>\s*([\d.,]+)',
        r'([\d.,]+)\s*avalia',
        r'([\d.,]+)\s*ratings?',
        r'([\d.,]+)\s*global ratings?',
    ),
    # Responsavel pela venda ("Vendido por") e pela entrega ("Enviado por").
    "vendido_por": (
        r'id="sellerProfileTriggerId"[^>]*>\s*([^<]+)',
        r'(?:Vendido por|Sold by)[:\s]*<[^>]*>\s*([^<]+)',
        r'id="merchant-info"[^>]*>\s*([^<]*)',
    ),
    "enviado_por": (
        r'(?:Enviado por|Ships from|Shipped by)[:\s]*<[^>]*>\s*([^<]+)',
        r'id="tabular-buybox"[^>]*>[\s\S]{0,400}?(?:Enviado por|Ships from)[\s\S]{0,80}?>\s*([^<]+)',
    ),
}

#: Marcadores textuais que, quando aparecem em "vendido_por"/"enviado_por",
#: indicam que a Amazon e a responsavel (mapeia para amz_vendido_por_amazon).
_MARCADORES_AMAZON_VENDEDOR: tuple[str, ...] = (
    "amazon.com.br",
    "amazon.com",
    "vendido por amazon",
    "sold by amazon",
    "amazon.br",
    "amazon servicos",
    "amazon services",
)

#: Regexes compiladas a partir de :data:`_SELETORES` (case-insensitive).
_SELETORES_COMPILADOS: dict[str, tuple[re.Pattern[str], ...]] = {
    sinal: tuple(re.compile(padrao, re.IGNORECASE) for padrao in padroes)
    for sinal, padroes in _SELETORES.items()
}


@dataclass(frozen=True)
class PaginaAmazon:
    """Resultado da busca de uma pagina de produto da Amazon.

    Contrato do seam de fetch injetavel (:class:`Amazon` recebe um
    ``buscar_pagina: (url: str) -> PaginaAmazon``). Carrega o HTML renderizado e,
    opcionalmente, o status HTTP da resposta — ambos consumidos por
    :meth:`Amazon.detectar_bloqueio` e pelo parsing puro.

    Args:
        html: HTML da pagina (renderizado). Pode ser vazio em caso de falha.
        status_code: status HTTP da resposta, quando disponivel (Playwright nem
            sempre expoe; ``None`` quando desconhecido).
    """

    html: str
    status_code: Optional[int] = None


#: Assinatura do seam de fetch injetavel.
PageFetcher = Callable[[str], PaginaAmazon]


def _buscar_pagina_playwright(url: str) -> PaginaAmazon:
    """Fetcher default: carrega ``url`` com Playwright (Chromium headless).

    O import do Playwright e **lazy** (feito aqui dentro) de proposito: o modulo
    ``amazon`` precisa importar mesmo em ambientes sem o Playwright ou sem os
    browsers instalados (design/tarefa 8.2). Assim, apenas quem *usa* o fetcher
    real paga o custo/dependencia; testes injetam um fetcher fake e nunca sobem
    um browser.
    """
    from playwright.sync_api import sync_playwright  # import lazy proposital

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        try:
            pagina = navegador.new_page(user_agent=_USER_AGENT)
            resposta = pagina.goto(
                url, timeout=_DEFAULT_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded"
            )
            status = resposta.status if resposta is not None else None
            conteudo = pagina.content()
            return PaginaAmazon(html=conteudo, status_code=status)
        finally:
            navegador.close()


# ---------------------------------------------------------------------------
# Helpers de parsing puro (operam sobre a string de HTML; sem I/O).
# ---------------------------------------------------------------------------

def _primeiro_match(html_str: str, sinal: str) -> Optional[str]:
    """Retorna o texto (grupo 1, ``strip``) do primeiro seletor que casar.

    Percorre os padroes candidatos de ``sinal`` em :data:`_SELETORES_COMPILADOS`
    na ordem definida e retorna o primeiro grupo capturado nao vazio. Retorna
    ``None`` quando nenhum seletor casa (sinal indisponivel — sem crash).
    """
    for padrao in _SELETORES_COMPILADOS.get(sinal, ()):  # ordem = prioridade
        m = padrao.search(html_str)
        if m:
            texto = _html.unescape(m.group(1)).strip()
            if texto:
                return texto
    return None


def _parse_preco_br(texto: Optional[str]) -> Optional[Decimal]:
    """Converte uma string de preco BR (ex.: "R$ 1.234,56") em ``Decimal``.

    Regras BR: ``.`` e separador de milhar e ``,`` e separador decimal. Remove o
    simbolo de moeda e espacos. Retorna ``Decimal`` exato (nunca ``float``) ou
    ``None`` se ``texto`` for ``None``/nao contiver digitos/nao for parseavel.
    """
    if not texto:
        return None
    # Mantem apenas digitos, ponto e virgula.
    limpo = re.sub(r"[^\d.,]", "", texto)
    if not limpo or not any(c.isdigit() for c in limpo):
        return None
    # Formato BR: remove milhar '.' e troca decimal ',' -> '.'.
    limpo = limpo.replace(".", "").replace(",", ".")
    try:
        return Decimal(limpo)
    except (InvalidOperation, ValueError):
        return None


def _parse_nota(texto: Optional[str]) -> Optional[float]:
    """Converte a nota media (ex.: "4,6") em ``float`` no intervalo 0..5.

    Aceita virgula ou ponto decimal. Retorna ``None`` quando ausente, nao
    numerico ou fora do intervalo ``0.0..5.0`` (sinal invalido -> indisponivel).
    """
    if not texto:
        return None
    limpo = texto.replace(",", ".").strip()
    m = re.search(r"\d+(?:\.\d+)?", limpo)
    if not m:
        return None
    try:
        nota = float(m.group(0))
    except ValueError:
        return None
    if 0.0 <= nota <= 5.0:
        return nota
    return None


def _parse_num_avaliacoes(texto: Optional[str]) -> Optional[int]:
    """Converte o numero de avaliacoes (ex.: "1.234" / "1,234") em ``int >= 0``.

    Remove separadores de milhar (``.`` e ``,``). Retorna ``None`` quando
    ausente ou sem digitos.
    """
    if not texto:
        return None
    somente_digitos = re.sub(r"[^\d]", "", texto)
    if not somente_digitos:
        return None
    try:
        valor = int(somente_digitos)
    except ValueError:
        return None
    return valor if valor >= 0 else None


def _parse_vendido_por_amazon(html_str: str) -> Optional[bool]:
    """Deduz ``amz_vendido_por_amazon`` a partir de vendedor/entrega (R6.2).

    Extrai "Vendido por"/"Enviado por" via seletores centralizados e verifica se
    a Amazon aparece como responsavel. Retorna:

    - ``True``  quando a Amazon e o vendedor/entregador;
    - ``False`` quando ha um vendedor terceiro identificado (que nao a Amazon);
    - ``None``  quando o sinal esta ausente (indisponivel — sem crash).
    """
    vendido = _primeiro_match(html_str, "vendido_por")
    enviado = _primeiro_match(html_str, "enviado_por")
    if vendido is None and enviado is None:
        return None

    def _e_amazon(valor: Optional[str]) -> bool:
        if not valor:
            return False
        alvo = valor.lower()
        return any(m in alvo for m in _MARCADORES_AMAZON_VENDEDOR)

    if _e_amazon(vendido) or _e_amazon(enviado):
        return True
    # Ha um responsavel identificado, mas nao e a Amazon -> terceiro.
    return False


class Amazon(BaseAdapter):
    """Adapter da Amazon via scraping (Playwright).

    Ver design.md -> "Adapter Amazon" e "Rate limiting e bloqueio da Amazon".

    O adapter aplica um atraso aleatorio antes de cada requisicao (R3.7) e
    desiste do item no ciclo atual quando detecta CAPTCHA/bloqueio, retornando
    ``sucesso=False`` sem nunca reintentar imediatamente. A busca da pagina fica
    atras de um seam injetavel (``buscar_pagina``) para tornar o parsing e a
    deteccao de bloqueio testaveis sem subir um browser real (task 8.3).

    Args:
        delay_min_segundos: limite inferior (em segundos) do atraso aleatorio
            antes de cada requisicao. Default 2.0s (R3.7). Deve ser ``>= 0``.
        delay_max_segundos: limite superior (em segundos) do atraso aleatorio.
            Default 8.0s (R3.7). Deve ser ``>= delay_min_segundos``.
        rng: funcao ``(a, b) -> float`` que sorteia um valor no intervalo
            ``[a, b]``. Default :func:`random.uniform`. Injetavel para tornar o
            calculo do atraso deterministico/testavel sem I/O (Property 12).
        sleep: funcao ``(segundos) -> None`` usada para efetivamente aguardar.
            Default :func:`time.sleep`. Injetavel para que os testes nunca
            durmam de verdade.
        buscar_pagina: seam de fetch — callable ``(url: str) -> PaginaAmazon``
            que retorna o HTML (e opcionalmente o status) da pagina de produto.
            Default: um fetcher baseado em Playwright (Chromium headless) com o
            import do Playwright feito de forma lazy. Injete um fetcher fake nos
            testes para exercitar parsing/bloqueio sem browser (task 8.3).
    """

    site: Site = Site.AMAZON

    def __init__(
        self,
        *,
        delay_min_segundos: float = _DEFAULT_DELAY_MIN_SEGUNDOS,
        delay_max_segundos: float = _DEFAULT_DELAY_MAX_SEGUNDOS,
        rng: Callable[[float, float], float] = random.uniform,
        sleep: Callable[[float], None] = time.sleep,
        buscar_pagina: PageFetcher = _buscar_pagina_playwright,
    ) -> None:
        if delay_min_segundos < 0:
            raise ValueError(
                "delay_min_segundos deve ser >= 0; recebido "
                f"{delay_min_segundos}."
            )
        if delay_max_segundos < delay_min_segundos:
            raise ValueError(
                "delay_max_segundos deve ser >= delay_min_segundos; recebido "
                f"min={delay_min_segundos}, max={delay_max_segundos}."
            )
        self._delay_min = float(delay_min_segundos)
        self._delay_max = float(delay_max_segundos)
        self._rng = rng
        self._sleep = sleep
        self._buscar_pagina = buscar_pagina

    @staticmethod
    def parse_url(url: str) -> Optional[ItemRef]:
        """Extrai ``(site, item_id)`` de uma URL da Amazon.

        Aceita dominios ``amazon.*`` e extrai o ASIN (exatamente 10 caracteres
        alfanumericos) dos padroes ``/dp/<ASIN>`` ou ``/gp/product/<ASIN>``
        (R1.2). Retorna ``None`` quando a URL e vazia, excede ``_MAX_URL_LEN``
        caracteres, nao pertence a Amazon ou nao contem um ASIN no formato
        esperado (R1.3, R1.4 / Property 2).
        """
        if not url or len(url) > _MAX_URL_LEN:
            return None

        try:
            parsed = urlparse(url if "//" in url else f"//{url}")
        except ValueError:
            return None

        host = parsed.hostname or ""
        if not _HOST_RE.search(host):
            return None

        match = _ASIN_RE.search(parsed.path)
        if not match:
            return None

        asin = match.group(1)
        return ItemRef(site=Site.AMAZON, item_id=asin, url_original=url)

    # ------------------------------------------------------------------
    # Rate limiting / atraso aleatorio (R3.7).
    # ------------------------------------------------------------------

    def calcular_atraso(self) -> float:
        """Sorteia o atraso (em segundos) a aplicar antes de uma requisicao.

        Usa o ``rng`` injetado (default :func:`random.uniform`) para sortear um
        valor no intervalo configurado ``[delay_min, delay_max]``. E puro (nao
        dorme, nao faz I/O), separado de :meth:`aguardar_antes_da_consulta`, para
        que os testes possam afirmar que o valor esta sempre em
        ``[min, max]`` sem dormir de verdade (Property 12, task 8.3).

        Como o intervalo e validado no construtor (``0 <= min <= max``), o
        resultado, apos o ``clamp`` defensivo, esta sempre em ``[min, max]``
        mesmo que um ``rng`` injetado retorne fora do intervalo.
        """
        bruto = self._rng(self._delay_min, self._delay_max)
        # Clamp defensivo: garante a Property 12 mesmo com um rng "fora do
        # contrato". random.uniform respeita o intervalo, mas rngs injetados
        # (ou fakes de teste) podem nao respeitar.
        if bruto < self._delay_min:
            return self._delay_min
        if bruto > self._delay_max:
            return self._delay_max
        return bruto

    def aguardar_antes_da_consulta(self) -> float:
        """Aplica o atraso aleatorio antes de uma requisicao Amazon (R3.7).

        Calcula o atraso via :meth:`calcular_atraso` e efetivamente aguarda esse
        tempo chamando a funcao ``sleep`` injetada (default :func:`time.sleep`).
        Retorna o atraso aplicado (em segundos), util para log/observabilidade e
        para os testes verificarem o valor sem depender do relogio real.
        """
        atraso = self.calcular_atraso()
        logger.debug("Amazon: aguardando %.3fs antes da consulta (R3.7).", atraso)
        self._sleep(atraso)
        return atraso

    # ------------------------------------------------------------------
    # Deteccao de CAPTCHA / bloqueio (R3.7).
    # ------------------------------------------------------------------

    @staticmethod
    def detectar_bloqueio(
        conteudo: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> bool:
        """Detecta se uma resposta da Amazon indica CAPTCHA/bloqueio (R3.7).

        Retorna ``True`` quando:

        - ``status_code`` e um dos status de rate limiting/bloqueio
          (``429``/``503``); ou
        - ``conteudo`` (HTML/texto da pagina) contem algum marcador conhecido de
          CAPTCHA/pagina de bloqueio (comparacao case-insensitive).

        A ausencia de informacao (ambos ``None``) e uma pagina de produto normal
        retornam ``False``. Helper puro e ``@staticmethod`` para ser reutilizado
        pela extracao Playwright (task 8.2) e testado sem I/O (task 8.3).
        """
        if status_code is not None and status_code in _STATUS_BLOQUEIO:
            return True

        if conteudo:
            texto = conteudo.lower()
            if any(marcador in texto for marcador in _MARCADORES_BLOQUEIO):
                return True

        return False

    def _registrar_bloqueio(self, produto_site_id: str, contexto: str) -> str:
        """Loga o bloqueio detectado e retorna a mensagem de erro padronizada.

        Centraliza a mensagem/log usados quando o adapter desiste do item por
        CAPTCHA/bloqueio, garantindo consistencia entre ``buscar_preco`` e
        ``buscar_reputacao_vendedor``.
        """
        erro = "bloqueio/CAPTCHA da Amazon detectado"
        logger.warning(
            "Amazon: %s em %s (%s); desistindo do item neste ciclo, nova "
            "tentativa apenas no proximo ciclo agendado (R3.7).",
            erro,
            produto_site_id,
            contexto,
        )
        return erro

    # ------------------------------------------------------------------
    # Fetch da pagina (aplica atraso R3.7 + deteccao de bloqueio). Nunca lanca.
    # ------------------------------------------------------------------

    def _montar_url(self, produto_site_id: str) -> str:
        """Monta a URL de produto da Amazon a partir do ASIN (R6.2)."""
        return _URL_PRODUTO_BASE.format(asin=produto_site_id)

    def _obter_pagina(self, produto_site_id: str) -> PaginaAmazon:
        """Aplica o atraso (R3.7) e busca a pagina via o seam injetado.

        Retorna a :class:`PaginaAmazon` obtida pelo fetcher. Nao trata excecao
        aqui: quem chama (``buscar_*``) envolve tudo num ``try/except`` para
        garantir a convencao "nunca lanca" da :class:`BaseAdapter`.
        """
        self.aguardar_antes_da_consulta()
        url = self._montar_url(produto_site_id)
        return self._buscar_pagina(url)

    # ------------------------------------------------------------------
    # Extracao via Playwright (task 8.2). Nunca lancam por falha externa.
    # ------------------------------------------------------------------

    def buscar_preco(self, produto_site_id: str) -> PrecoResult:
        """Consulta o preco atual do item na Amazon (R6.2).

        Aplica o atraso aleatorio (R3.7), busca a pagina via o seam injetado e
        extrai o preco (``Decimal`` — nunca ``float``) e o titulo dos seletores
        centralizados. Ao detectar CAPTCHA/bloqueio, loga e desiste do item no
        ciclo (``sucesso=False``), sem reintentar imediatamente. Nunca lanca por
        falha externa: em erro retorna ``PrecoResult(sucesso=False, erro=...)``
        (R3.3). Preco ausente e tratado como indisponivel (``sucesso=False``),
        nao como crash.
        """
        try:
            pagina = self._obter_pagina(produto_site_id)

            if self.detectar_bloqueio(pagina.html, pagina.status_code):
                erro = self._registrar_bloqueio(produto_site_id, "buscar_preco")
                return PrecoResult(sucesso=False, erro=erro)

            html_str = pagina.html or ""
            nome_produto = _primeiro_match(html_str, "nome_produto")
            preco = _parse_preco_br(_primeiro_match(html_str, "preco"))

            if preco is None:
                logger.warning(
                    "Amazon buscar_preco(%s): preco indisponivel (seletor nao "
                    "casou).",
                    produto_site_id,
                )
                return PrecoResult(
                    sucesso=False,
                    erro="preço indisponível",
                    nome_produto=nome_produto,
                )

            return PrecoResult(
                sucesso=True,
                preco=preco,
                moeda="BRL",
                nome_produto=nome_produto,
            )
        except Exception as exc:  # nunca lanca por falha externa (R3.3)
            logger.warning(
                "Amazon buscar_preco(%s) falhou: %s", produto_site_id, exc
            )
            return PrecoResult(sucesso=False, erro=str(exc))

    def buscar_reputacao_vendedor(self, produto_site_id: str) -> ReputacaoResult:
        """Consulta os sinais de reputacao do vendedor na Amazon (R6.2).

        Aplica o atraso aleatorio (R3.7), busca a pagina via o seam injetado e
        extrai, dos seletores centralizados: nota media (0..5), numero de
        avaliacoes (>= 0) e o responsavel pela venda/entrega
        (``amz_vendido_por_amazon``). Seletor ausente -> o sinal fica ``None`` e
        ``sinais_indisponiveis=True`` (R6.5), sem crash. Ao detectar
        CAPTCHA/bloqueio, loga e desiste (``sucesso=False``). Nunca lanca por
        falha externa (R6.6).
        """
        try:
            pagina = self._obter_pagina(produto_site_id)

            if self.detectar_bloqueio(pagina.html, pagina.status_code):
                erro = self._registrar_bloqueio(
                    produto_site_id, "buscar_reputacao_vendedor"
                )
                return ReputacaoResult(sucesso=False, erro=erro)

            html_str = pagina.html or ""
            nota = _parse_nota(_primeiro_match(html_str, "nota_media"))
            num_avaliacoes = _parse_num_avaliacoes(
                _primeiro_match(html_str, "num_avaliacoes")
            )
            vendido_por_amazon = _parse_vendido_por_amazon(html_str)

            sinais_indisponiveis = (
                nota is None
                or num_avaliacoes is None
                or vendido_por_amazon is None
            )

            return ReputacaoResult(
                sucesso=True,
                amz_nota_media=nota,
                amz_num_avaliacoes=num_avaliacoes,
                amz_vendido_por_amazon=vendido_por_amazon,
                sinais_indisponiveis=sinais_indisponiveis,
            )
        except Exception as exc:  # nunca lanca por falha externa (R6.6)
            logger.warning(
                "Amazon buscar_reputacao_vendedor(%s) falhou: %s",
                produto_site_id,
                exc,
            )
            return ReputacaoResult(sucesso=False, erro=str(exc))
