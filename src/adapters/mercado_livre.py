"""Adapter do Mercado Livre (MVP) — estrategia de CATALOGO.

Este modulo implementa o adapter isolado do Mercado Livre, conforme a secao
"Adapter Mercado Livre" do design. Nenhum adapter importa outro adapter.

Motivacao da estrategia de catalogo
------------------------------------
O endpoint ``GET /items/{item_id}`` retorna **403** com o escopo/token do
usuario (confirmado empiricamente contra a API real) e os links usados sao os de
**catalogo** (``/p/<PRODUCT_ID>``). Portanto o adapter NUNCA usa ``/items/{id}``.
Em vez disso, opera sobre o catalogo, usando apenas:

- ``GET /products/{product_id}``       -> ficha do catalogo (``name``, etc.).
- ``GET /products/{product_id}/items`` -> anuncios do produto (``results``),
  cada um com ``item_id``, ``seller_id``, ``price``, ``currency_id``,
  ``condition`` ("new"/"used"), etc. (nao traz ``attributes`` por item).
- ``GET /users/{seller_id}``           -> ``seller_reputation`` (``level_id``,
  ``power_seller_status``, ``transactions.ratings`` — que na pratica costuma vir
  vazio, tornando o percentual de reclamacoes indisponivel).

Estrategia A2 ("melhor preco entre vendedores confiaveis, mesmo produto")
-------------------------------------------------------------------------
- ``buscar_preco`` (R3.1) lista os anuncios do produto no catalogo, filtra
  ``condition == "new"`` com ``price`` numerico ``> 0``, ordena por preco
  ascendente e escolhe o PRIMEIRO (mais barato) cujo vendedor e **confiavel**
  (ver :func:`_vendedor_confiavel`). O preco do vencedor vira ``PrecoResult``.
- ``buscar_reputacao_vendedor`` (R6.1) reflete o MESMO vendedor vencedor,
  mapeando ``level_id``, o selo (via ``power_seller_status``) e o percentual de
  reclamacoes (``transactions.ratings.negative`` x 100), quando disponiveis.

Toda a camada HTTP/OAuth e delegada a :class:`ClienteOAuthML`, injetada no
construtor (mockavel nos testes). Seguindo a convencao de :class:`BaseAdapter`,
``buscar_preco`` e ``buscar_reputacao_vendedor`` NUNCA lancam por falha externa
(rede/parsing/reautenticacao): retornam um resultado com ``sucesso=False`` e
``erro`` preenchido (R3.3, R6.6).
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .base import BaseAdapter, ItemRef, PrecoResult, ReputacaoResult, Site
from .ml_oauth import ClienteOAuthML, ReautenticacaoManualNecessaria

logger = logging.getLogger(__name__)

#: Comprimento maximo aceito para uma URL (R1.3 / Property 2).
_MAX_URL_LEN = 2048

#: Regex do identificador do Mercado Livre: "MLB" seguido de digitos (R1.1).
#: Em links de catalogo o ID aparece no segmento ``/p/<ID>`` (ex.:
#: ``/p/MLB46056259``); em URLs de anuncio antigas aparece igualmente como
#: ``MLB`` + digitos. Case-insensitive para aceitar variacoes; o resultado e
#: normalizado para maiusculas. Semanticamente, o ``item_id`` armazenado passa a
#: ser um **Product ID de catalogo**.
_ITEM_ID_RE = re.compile(r"MLB\d+", re.IGNORECASE)

#: Dominios aceitos: mercadolivre.* (pt-BR) e mercadolibre.* (es).
_HOST_RE = re.compile(r"(?:^|\.)mercadoli(?:vre|bre)\.", re.IGNORECASE)

#: Mapeamento ``power_seller_status`` (API do ML) -> selo MercadoLider usado em
#: ``ReputacaoResult.ml_selo_mercadolider`` (design -> "Score de confianca").
_SELO_POR_STATUS: dict[Optional[str], str] = {
    None: "ausente",
    "": "ausente",
    "silver": "mercadolider",
    "gold": "gold",
    "platinum": "platinum",
}

#: Niveis de reputacao considerados confiaveis (criterio aprovado pelo usuario).
NIVEIS_CONFIAVEIS: frozenset[str] = frozenset({"4_light_green", "5_green"})

#: Limite de vendedores avaliados por consulta. Cada candidato exige uma chamada
#: ``GET /users/{seller_id}``; avaliamos apenas os N mais baratos para nao
#: estourar o numero de chamadas quando o catalogo tem muitos anuncios. Como os
#: candidatos sao ordenados por preco ascendente, o vencedor (o mais barato
#: confiavel) esta, na pratica, entre os primeiros.
MAX_CANDIDATOS_AVALIADOS = 10

#: Mensagem padrao quando nenhum vendedor confiavel oferece o produto.
_ERRO_SEM_VENDEDOR = "nenhum vendedor confiável com o produto disponível"


def _vendedor_confiavel(seller_reputation: dict[str, Any]) -> bool:
    """Criterio de vendedor confiavel (aprovado pelo usuario).

    Elegivel quando o nivel de reputacao esta em :data:`NIVEIS_CONFIAVEIS`
    (``4_light_green``/``5_green``) OU quando o vendedor possui selo MercadoLider
    (``power_seller_status`` nao nulo/vazio).
    """
    level = seller_reputation.get("level_id")
    selo = seller_reputation.get("power_seller_status")
    return (level in NIVEIS_CONFIAVEIS) or bool(selo)


class MercadoLivre(BaseAdapter):
    """Adapter do Mercado Livre via API oficial (OAuth2), estrategia de catalogo.

    Ver design.md -> "Adapter Mercado Livre".

    Args:
        cliente: :class:`ClienteOAuthML` ja configurado (injetado). Encapsula as
            chamadas autenticadas, o refresh de token (R7.4) e o sinal de
            reautenticacao manual (R7.5). Torna o adapter testavel sem rede real.
    """

    site: Site = Site.MERCADO_LIVRE

    def __init__(self, cliente: ClienteOAuthML) -> None:
        self._cliente = cliente

    @staticmethod
    def parse_url(url: str) -> Optional[ItemRef]:
        """Extrai ``(site, item_id)`` de uma URL do Mercado Livre.

        Aceita dominios ``mercadolivre.*`` e ``mercadolibre.*`` e extrai o
        identificador no formato ``MLB`` + digitos (R1.1), normalizando-o para
        maiusculas. Isso captura tanto o Product ID de catalogo no segmento
        ``/p/<ID>`` (ex.: ``.../p/MLB46056259``) quanto os IDs de URLs de anuncio
        antigas. Retorna ``None`` quando a URL e vazia, excede ``_MAX_URL_LEN``
        caracteres, nao pertence ao Mercado Livre ou nao contem um identificador
        no formato esperado (R1.3, R1.4 / Property 2).
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

        match = _ITEM_ID_RE.search(url)
        if not match:
            return None

        item_id = match.group(0).upper()
        return ItemRef(site=Site.MERCADO_LIVRE, item_id=item_id, url_original=url)

    # ------------------------------------------------------------------
    # Consultas autenticadas (R3.1, R6.1). Nunca lancam por falha externa.
    # ------------------------------------------------------------------

    def buscar_preco(self, produto_site_id: str) -> PrecoResult:
        """Consulta o melhor preco entre vendedores confiaveis (R3.1).

        Estrategia de catalogo A2: obtem o ``name`` via
        ``GET /products/{product_id}`` (nao fatal se falhar), lista os anuncios
        via ``GET /products/{product_id}/items`` e escolhe o mais barato entre os
        anuncios ``new`` cujo vendedor e confiavel (:func:`_vendedor_confiavel`).

        Retorna ``PrecoResult(sucesso=True, ...)`` com o preco do vencedor
        (sempre ``Decimal``) ou ``sucesso=False`` quando nenhum vendedor
        confiavel oferece o produto. Nunca lanca por falha externa (R3.3, R7.5).
        """
        try:
            nome = self._obter_nome_produto(produto_site_id)
            vencedor = self._selecionar_item_vencedor(produto_site_id)

            if vencedor is None:
                return PrecoResult(
                    sucesso=False, erro=_ERRO_SEM_VENDEDOR, nome_produto=nome
                )

            item, _reputacao = vencedor
            preco = Decimal(str(item.get("price")))
            return PrecoResult(
                sucesso=True,
                preco=preco,
                moeda=item.get("currency_id") or "BRL",
                nome_produto=nome,
            )
        except ReautenticacaoManualNecessaria as exc:
            logger.error(
                "buscar_preco(%s): reautenticacao manual necessaria: %s",
                produto_site_id,
                exc,
            )
            return PrecoResult(
                sucesso=False, erro="reautenticação manual necessária"
            )
        except (httpx.HTTPError, ValueError, KeyError, InvalidOperation) as exc:
            logger.warning("buscar_preco(%s) falhou: %s", produto_site_id, exc)
            return PrecoResult(sucesso=False, erro=str(exc))

    def buscar_reputacao_vendedor(self, produto_site_id: str) -> ReputacaoResult:
        """Consulta a reputacao do vendedor VENCEDOR (R6.1).

        Reflete o MESMO vendedor escolhido em :meth:`buscar_preco` (o mais barato
        confiavel), reutilizando :meth:`_selecionar_item_vencedor`. Mapeia
        ``level_id``, ``power_seller_status`` -> selo e
        ``transactions.ratings.negative`` (fracao ``0..1``) x 100 ->
        ``ml_percentual_reclamacoes`` (``None`` quando ausente — comum, pois
        ``transactions.ratings`` costuma vir vazio). Nunca lanca (R6.6).
        """
        try:
            vencedor = self._selecionar_item_vencedor(produto_site_id)
            if vencedor is None:
                return ReputacaoResult(sucesso=False, erro=_ERRO_SEM_VENDEDOR)

            _item, reputacao = vencedor
            level_id = reputacao.get("level_id")
            status = reputacao.get("power_seller_status")
            selo = _SELO_POR_STATUS.get(status, "ausente")

            percentual: Optional[float] = None
            ratings = (reputacao.get("transactions") or {}).get("ratings") or {}
            negativo = ratings.get("negative")
            if negativo is not None:
                try:
                    percentual = round(float(negativo) * 100, 2)
                except (TypeError, ValueError):
                    percentual = None

            sinais_indisponiveis = level_id is None or percentual is None

            return ReputacaoResult(
                sucesso=True,
                ml_level_id=level_id,
                ml_selo_mercadolider=selo,
                ml_percentual_reclamacoes=percentual,
                sinais_indisponiveis=sinais_indisponiveis,
            )
        except ReautenticacaoManualNecessaria as exc:
            logger.error(
                "buscar_reputacao_vendedor(%s): reautenticacao manual "
                "necessaria: %s",
                produto_site_id,
                exc,
            )
            return ReputacaoResult(
                sucesso=False, erro="reautenticação manual necessária"
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning(
                "buscar_reputacao_vendedor(%s) falhou: %s", produto_site_id, exc
            )
            return ReputacaoResult(sucesso=False, erro=str(exc))

    # ------------------------------------------------------------------
    # Internos (estrategia de catalogo).
    # ------------------------------------------------------------------

    def _obter_nome_produto(self, product_id: str) -> Optional[str]:
        """Obtem o ``name`` da ficha do catalogo (``GET /products/{id}``).

        Nao e fatal: se a chamada falhar (rede/parsing), retorna ``None`` e o
        fluxo segue sem o nome do produto, preservando o isolamento de erro. A
        reautenticacao manual (R7.5) NAO e engolida aqui — propaga para o caller
        converter em ``sucesso=False`` de forma uniforme.
        """
        try:
            resp = self._cliente.get(f"/products/{product_id}")
            resp.raise_for_status()
            dados = resp.json()
            nome = dados.get("name")
            return nome if isinstance(nome, str) else None
        except ReautenticacaoManualNecessaria:
            raise
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.info(
                "_obter_nome_produto(%s): nome indisponivel: %s",
                product_id,
                exc,
            )
            return None

    def _listar_candidatos(self, product_id: str) -> list[dict[str, Any]]:
        """Lista anuncios ``new`` com preco > 0, ordenados por preco crescente.

        Consulta ``GET /products/{product_id}/items`` e aplica o filtro de
        candidatos (``condition == "new"`` e ``price`` numerico ``> 0``). O
        resultado e ordenado por ``price`` ascendente para que o primeiro
        vendedor confiavel encontrado seja tambem o mais barato.
        """
        resp = self._cliente.get(f"/products/{product_id}/items")
        resp.raise_for_status()
        results = resp.json().get("results") or []

        candidatos: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("condition") != "new":
                continue
            preco = self._preco_do_item(item)
            if preco is None or preco <= 0:
                continue
            candidatos.append(item)

        candidatos.sort(key=lambda it: self._preco_do_item(it) or Decimal(0))
        return candidatos

    @staticmethod
    def _preco_do_item(item: dict[str, Any]) -> Optional[Decimal]:
        """Extrai o ``price`` de um anuncio como ``Decimal`` (ou ``None``)."""
        bruto = item.get("price")
        if bruto is None:
            return None
        try:
            return Decimal(str(bruto))
        except (InvalidOperation, ValueError):
            return None

    def _selecionar_item_vencedor(
        self, product_id: str
    ) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
        """Escolhe o anuncio mais barato de um vendedor confiavel.

        Lista os candidatos (``new``, preco > 0, ordenados por preco crescente),
        percorre do mais barato ao mais caro (ate :data:`MAX_CANDIDATOS_AVALIADOS`
        avaliacoes de vendedor) e retorna o PRIMEIRO cujo vendedor e confiavel,
        junto com o ``seller_reputation`` ja obtido — permitindo que
        :meth:`buscar_preco` e :meth:`buscar_reputacao_vendedor` reflitam o mesmo
        vendedor. Retorna ``None`` quando nenhum candidato passa no criterio.
        """
        candidatos = self._listar_candidatos(product_id)

        for item in candidatos[:MAX_CANDIDATOS_AVALIADOS]:
            seller_id = item.get("seller_id")
            if seller_id is None:
                continue
            reputacao = self._obter_reputacao(seller_id)
            if _vendedor_confiavel(reputacao):
                return item, reputacao
        return None

    def _obter_reputacao(self, seller_id: Any) -> dict[str, Any]:
        """Obtem ``seller_reputation`` via ``GET /users/{seller_id}``.

        Retorna o dict de reputacao (possivelmente vazio). A reautenticacao
        manual (R7.5) propaga; demais falhas externas propagam como
        ``httpx.HTTPError``/``ValueError`` para o caller isolar.
        """
        resp = self._cliente.get(f"/users/{seller_id}")
        resp.raise_for_status()
        reputacao = resp.json().get("seller_reputation")
        return reputacao if isinstance(reputacao, dict) else {}
