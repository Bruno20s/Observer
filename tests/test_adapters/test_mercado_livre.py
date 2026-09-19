"""Testes (mockados) do adapter do Mercado Livre — estrategia de CATALOGO.

Estes sao testes de EXEMPLO (nao property-based), portanto NAO carregam o
marcador ``# Feature:``. Toda a camada HTTP e simulada por um cliente fake que
satisfaz o protocolo ``SuporteHTTP`` (metodos ``request`` e ``post``) esperado
por :class:`ClienteOAuthML`. NUNCA ha rede real.

O adapter usa SOMENTE os endpoints de catalogo:
- ``GET /products/{product_id}``       -> ficha (``name``).
- ``GET /products/{product_id}/items`` -> ``{"results": [...]}`` (anuncios).
- ``GET /users/{seller_id}``           -> ``seller_reputation``.
NUNCA usa ``/items/{id}`` (que retorna 403 com o token do usuario).

Cobertura:
- Selecao do mais barato CONFIAVEL (o mais barato absoluto tem vendedor nao
  confiavel; vence o segundo, cujo vendedor e confiavel).
- Nenhum vendedor confiavel -> sucesso=False.
- Filtro de ``condition``: um item "used" mais barato e ignorado.
- Preco Decimal exato.
- ``buscar_reputacao_vendedor`` reflete o MESMO vendedor vencedor; percentual de
  reclamacoes None quando ``transactions.ratings`` vazio -> sinais_indisponiveis.
- Fluxo ``401 -> refresh -> retry`` sobre ``/products/{pid}/items`` (R7.4).
- Falha de refresh 3x -> reautenticacao manual, sem propagar excecao (R7.5).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional

import httpx
import pytest

from src.adapters.base import Site
from src.adapters.mercado_livre import MercadoLivre
from src.adapters.ml_oauth import (
    API_BASE,
    MAX_TENTATIVAS_REFRESH,
    TOKEN_ENDPOINT,
    ClienteOAuthML,
)

# ---------------------------------------------------------------------------
# Fixtures de dados (fake JSON representativo da API de catalogo do ML)
# ---------------------------------------------------------------------------

PRODUCT_ID = "MLB46056259"

SELLER_CONFIAVEL = 111111111
SELLER_CONFIAVEL_2 = 222222222
SELLER_NAO_CONFIAVEL = 999999999

_PRODUCT_JSON: dict[str, Any] = {
    "id": PRODUCT_ID,
    "name": "Monitor Gamer 27 Polegadas 144Hz",
    "domain_id": "MLB-MONITORS",
    "attributes": [{"id": "BRAND", "value_name": "XPTO"}],
    "buy_box_winner": None,
}

# Reputacao confiavel: 5_green + platinum, com ratings.negative presente.
_USER_CONFIAVEL: dict[str, Any] = {
    "id": SELLER_CONFIAVEL,
    "seller_reputation": {
        "level_id": "5_green",
        "power_seller_status": "platinum",
        "transactions": {"ratings": {"negative": 0.01}},
    },
}

# Reputacao confiavel 2: 4_light_green, sem selo, ratings VAZIO (comum na API
# real) -> percentual de reclamacoes indisponivel.
_USER_CONFIAVEL_2: dict[str, Any] = {
    "id": SELLER_CONFIAVEL_2,
    "seller_reputation": {
        "level_id": "4_light_green",
        "power_seller_status": None,
        "transactions": {"ratings": {}},
    },
}

# Reputacao NAO confiavel: 1_red, sem selo.
_USER_NAO_CONFIAVEL: dict[str, Any] = {
    "id": SELLER_NAO_CONFIAVEL,
    "seller_reputation": {
        "level_id": "1_red",
        "power_seller_status": None,
        "transactions": {"ratings": {}},
    },
}


def _items_payload(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Envelopa uma lista de anuncios no formato de /products/{id}/items."""
    return {"paging": {"total": len(results)}, "results": results}


# ---------------------------------------------------------------------------
# Cliente HTTP fake (satisfaz o protocolo SuporteHTTP: request + post)
# ---------------------------------------------------------------------------


def _resposta(url: str, status: int, corpo: Any) -> httpx.Response:
    """Constroi uma ``httpx.Response`` realista ligada a um ``Request``."""
    request = httpx.Request("GET", url)
    return httpx.Response(
        status_code=status,
        request=request,
        content=json.dumps(corpo).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class FakeHTTPClient:
    """Cliente HTTP fake e roteavel por URL.

    - ``request`` roteia por ``url`` para uma resposta programada. Opcionalmente
      retorna 401 nas primeiras ``unauthorized_ate`` chamadas (token expirado).
    - ``post`` (refresh de token) retorna sucesso (200 com novos tokens) ou uma
      falha programada (status != 200), permitindo simular a falha 3x.

    As respostas sao programadas por PREFIXO/substring de URL (ex.:
    ``/products/MLB.../items``), do mais especifico ao menos especifico.
    """

    def __init__(
        self,
        *,
        respostas: dict[str, tuple[int, Any]],
        unauthorized_ate: int = 0,
        refresh_status: int = 200,
        novo_access_token: str = "novo-access-token",
        novo_refresh_token: Optional[str] = "novo-refresh-token",
    ) -> None:
        # Ordena por comprimento decrescente da chave: casa o padrao mais
        # especifico primeiro (ex.: ".../items" antes de "/products/...").
        self._respostas = dict(
            sorted(respostas.items(), key=lambda kv: len(kv[0]), reverse=True)
        )
        self._unauthorized_ate = unauthorized_ate
        self._refresh_status = refresh_status
        self._novo_access = novo_access_token
        self._novo_refresh = novo_refresh_token

        self.request_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.post_calls: list[dict[str, Any]] = []
        self._auth_call_count = 0

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._auth_call_count += 1
        self.request_calls.append((method, url, kwargs))

        if self._auth_call_count <= self._unauthorized_ate:
            return _resposta(url, 401, {"message": "invalid_token"})

        for prefixo, (status, corpo) in self._respostas.items():
            if prefixo in url:
                return _resposta(url, status, corpo)

        raise AssertionError(f"URL nao programada no fake: {url}")

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.post_calls.append({"url": url, **kwargs})
        if self._refresh_status == 200:
            corpo: dict[str, Any] = {
                "access_token": self._novo_access,
                "token_type": "bearer",
                "expires_in": 21600,
            }
            if self._novo_refresh is not None:
                corpo["refresh_token"] = self._novo_refresh
            return _resposta(url, 200, corpo)
        return _resposta(url, self._refresh_status, {"error": "invalid_grant"})


def _fazer_cliente(
    fake: FakeHTTPClient,
    *,
    access_token: str = "access-inicial",
    refresh_token: str = "refresh-inicial",
    on_reautenticacao: Optional[Any] = None,
) -> ClienteOAuthML:
    """Cria um ``ClienteOAuthML`` com credenciais ficticias e o http fake."""
    return ClienteOAuthML(
        client_id="client-id-ficticio",
        client_secret="client-secret-ficticio",
        access_token=access_token,
        refresh_token=refresh_token,
        http_client=fake,
        timeout=5.0,
        on_reautenticacao_manual=on_reautenticacao,
    )


def _fake_catalogo(
    results: list[dict[str, Any]],
    users: dict[int, dict[str, Any]],
    **kwargs: Any,
) -> FakeHTTPClient:
    """Monta um FakeHTTPClient com product, items e um /users por seller."""
    respostas: dict[str, tuple[int, Any]] = {
        f"/products/{PRODUCT_ID}/items": (200, _items_payload(results)),
        f"/products/{PRODUCT_ID}": (200, _PRODUCT_JSON),
    }
    for seller_id, user_json in users.items():
        respostas[f"/users/{seller_id}"] = (200, user_json)
    return FakeHTTPClient(respostas=respostas, **kwargs)


# ===========================================================================
# 1) Selecao do mais barato CONFIAVEL (R3.1)
# ===========================================================================


def test_buscar_preco_escolhe_mais_barato_confiavel() -> None:
    """O mais barato absoluto e nao confiavel; vence o proximo confiavel."""
    results = [
        {  # mais barato absoluto, porem vendedor NAO confiavel -> descartado
            "item_id": "MLBAAA",
            "seller_id": SELLER_NAO_CONFIAVEL,
            "price": 999.0,
            "currency_id": "BRL",
            "condition": "new",
        },
        {  # segundo mais barato, vendedor confiavel -> VENCE
            "item_id": "MLBBBB",
            "seller_id": SELLER_CONFIAVEL,
            "price": 1053,
            "currency_id": "BRL",
            "condition": "new",
        },
        {  # mais caro, confiavel, mas nao e o mais barato confiavel
            "item_id": "MLBCCC",
            "seller_id": SELLER_CONFIAVEL_2,
            "price": 1200.0,
            "currency_id": "BRL",
            "condition": "new",
        },
    ]
    fake = _fake_catalogo(
        results,
        {
            SELLER_NAO_CONFIAVEL: _USER_NAO_CONFIAVEL,
            SELLER_CONFIAVEL: _USER_CONFIAVEL,
            SELLER_CONFIAVEL_2: _USER_CONFIAVEL_2,
        },
    )
    adapter = MercadoLivre(_fazer_cliente(fake))

    resultado = adapter.buscar_preco(PRODUCT_ID)

    assert resultado.sucesso is True
    assert resultado.preco == Decimal("1053")  # nao o 999 (nao confiavel)
    assert resultado.moeda == "BRL"
    assert resultado.nome_produto == "Monitor Gamer 27 Polegadas 144Hz"
    assert resultado.erro is None


def test_buscar_preco_selo_torna_vendedor_confiavel() -> None:
    """Vendedor sem nivel confiavel mas COM selo MercadoLider e elegivel."""
    user_com_selo = {
        "id": SELLER_CONFIAVEL,
        "seller_reputation": {
            "level_id": "3_yellow",  # nivel nao confiavel...
            "power_seller_status": "gold",  # ...mas tem selo -> confiavel
            "transactions": {"ratings": {}},
        },
    }
    results = [
        {
            "item_id": "MLBBBB",
            "seller_id": SELLER_CONFIAVEL,
            "price": 1060.28,
            "currency_id": "BRL",
            "condition": "new",
        }
    ]
    fake = _fake_catalogo(results, {SELLER_CONFIAVEL: user_com_selo})
    adapter = MercadoLivre(_fazer_cliente(fake))

    resultado = adapter.buscar_preco(PRODUCT_ID)

    assert resultado.sucesso is True
    assert resultado.preco == Decimal("1060.28")


# ===========================================================================
# 2) Nenhum vendedor confiavel -> sucesso=False (R3.1)
# ===========================================================================


def test_buscar_preco_sem_vendedor_confiavel() -> None:
    """Todos os candidatos tem vendedor nao confiavel -> sucesso=False."""
    results = [
        {
            "item_id": "MLBAAA",
            "seller_id": SELLER_NAO_CONFIAVEL,
            "price": 100.0,
            "currency_id": "BRL",
            "condition": "new",
        }
    ]
    fake = _fake_catalogo(results, {SELLER_NAO_CONFIAVEL: _USER_NAO_CONFIAVEL})
    adapter = MercadoLivre(_fazer_cliente(fake))

    resultado = adapter.buscar_preco(PRODUCT_ID)

    assert resultado.sucesso is False
    assert resultado.preco is None
    assert resultado.erro == "nenhum vendedor confiável com o produto disponível"
    # O nome do produto ainda e obtido (nao fatal).
    assert resultado.nome_produto == "Monitor Gamer 27 Polegadas 144Hz"


def test_buscar_preco_lista_vazia() -> None:
    """Catalogo sem anuncios -> sucesso=False."""
    fake = _fake_catalogo([], {})
    adapter = MercadoLivre(_fazer_cliente(fake))

    resultado = adapter.buscar_preco(PRODUCT_ID)

    assert resultado.sucesso is False
    assert resultado.erro == "nenhum vendedor confiável com o produto disponível"


# ===========================================================================
# 3) Filtro de condition (used ignorado) (R3.1)
# ===========================================================================


def test_buscar_preco_ignora_condition_used() -> None:
    """Um item 'used' mais barato e ignorado; vence o 'new' confiavel."""
    results = [
        {  # usado mais barato -> deve ser ignorado
            "item_id": "MLBUSED",
            "seller_id": SELLER_CONFIAVEL,
            "price": 500.0,
            "currency_id": "BRL",
            "condition": "used",
        },
        {  # novo confiavel -> VENCE mesmo sendo mais caro que o usado
            "item_id": "MLBNEW",
            "seller_id": SELLER_CONFIAVEL,
            "price": 1053.0,
            "currency_id": "BRL",
            "condition": "new",
        },
    ]
    fake = _fake_catalogo(results, {SELLER_CONFIAVEL: _USER_CONFIAVEL})
    adapter = MercadoLivre(_fazer_cliente(fake))

    resultado = adapter.buscar_preco(PRODUCT_ID)

    assert resultado.sucesso is True
    assert resultado.preco == Decimal("1053.0")


# ===========================================================================
# 4) buscar_reputacao_vendedor reflete o vendedor VENCEDOR (R6.1)
# ===========================================================================


def test_reputacao_reflete_vendedor_vencedor() -> None:
    """A reputacao retornada e a do mesmo vendedor escolhido no preco."""
    results = [
        {
            "item_id": "MLBAAA",
            "seller_id": SELLER_NAO_CONFIAVEL,
            "price": 999.0,
            "currency_id": "BRL",
            "condition": "new",
        },
        {
            "item_id": "MLBBBB",
            "seller_id": SELLER_CONFIAVEL,
            "price": 1053.0,
            "currency_id": "BRL",
            "condition": "new",
        },
    ]
    fake = _fake_catalogo(
        results,
        {
            SELLER_NAO_CONFIAVEL: _USER_NAO_CONFIAVEL,
            SELLER_CONFIAVEL: _USER_CONFIAVEL,
        },
    )
    adapter = MercadoLivre(_fazer_cliente(fake))

    resultado = adapter.buscar_reputacao_vendedor(PRODUCT_ID)

    assert resultado.sucesso is True
    # Vendedor vencedor e o SELLER_CONFIAVEL (5_green / platinum).
    assert resultado.ml_level_id == "5_green"
    assert resultado.ml_selo_mercadolider == "platinum"
    assert resultado.ml_percentual_reclamacoes == pytest.approx(1.0)
    assert resultado.sinais_indisponiveis is False


def test_reputacao_ratings_vazio_sinais_indisponiveis() -> None:
    """transactions.ratings vazio -> percentual None -> sinais_indisponiveis."""
    results = [
        {
            "item_id": "MLBBBB",
            "seller_id": SELLER_CONFIAVEL_2,
            "price": 1053.0,
            "currency_id": "BRL",
            "condition": "new",
        }
    ]
    fake = _fake_catalogo(results, {SELLER_CONFIAVEL_2: _USER_CONFIAVEL_2})
    adapter = MercadoLivre(_fazer_cliente(fake))

    resultado = adapter.buscar_reputacao_vendedor(PRODUCT_ID)

    assert resultado.sucesso is True
    assert resultado.ml_level_id == "4_light_green"
    assert resultado.ml_selo_mercadolider == "ausente"  # power_seller_status None
    assert resultado.ml_percentual_reclamacoes is None
    assert resultado.sinais_indisponiveis is True


def test_reputacao_sem_vendedor_confiavel() -> None:
    """Nenhum vendedor confiavel -> sucesso=False na reputacao."""
    results = [
        {
            "item_id": "MLBAAA",
            "seller_id": SELLER_NAO_CONFIAVEL,
            "price": 100.0,
            "currency_id": "BRL",
            "condition": "new",
        }
    ]
    fake = _fake_catalogo(results, {SELLER_NAO_CONFIAVEL: _USER_NAO_CONFIAVEL})
    adapter = MercadoLivre(_fazer_cliente(fake))

    resultado = adapter.buscar_reputacao_vendedor(PRODUCT_ID)

    assert resultado.sucesso is False
    assert resultado.erro == "nenhum vendedor confiável com o produto disponível"


# ===========================================================================
# 5) Fluxo 401 -> refresh -> retry sobre /products/{pid}/items (R7.4)
# ===========================================================================


def test_401_dispara_refresh_e_retry_com_sucesso() -> None:
    """401 na 1a chamada -> refresh -> retry com novo token -> sucesso."""
    results = [
        {
            "item_id": "MLBBBB",
            "seller_id": SELLER_CONFIAVEL,
            "price": 1053.0,
            "currency_id": "BRL",
            "condition": "new",
        }
    ]
    fake = _fake_catalogo(
        results,
        {SELLER_CONFIAVEL: _USER_CONFIAVEL},
        unauthorized_ate=1,  # so a primeira chamada autenticada retorna 401
        novo_access_token="access-renovado",
        novo_refresh_token="refresh-rotacionado",
    )
    cliente = _fazer_cliente(fake)
    adapter = MercadoLivre(cliente)

    resultado = adapter.buscar_preco(PRODUCT_ID)

    assert resultado.sucesso is True
    assert resultado.preco == Decimal("1053.0")

    # O refresh de fato aconteceu (uma chamada de post ao endpoint de token).
    assert len(fake.post_calls) == 1
    assert fake.post_calls[0]["url"] == TOKEN_ENDPOINT

    # Tokens rotacionados EM MEMORIA apos o refresh (R7.4).
    assert cliente.access_token == "access-renovado"
    assert cliente.refresh_token == "refresh-rotacionado"
    assert cliente.reautenticacao_manual_necessaria is False


# ===========================================================================
# 6) Falha de refresh 3x -> reautenticacao manual, sem propagar (R7.5)
# ===========================================================================


def test_refresh_falha_3x_sinaliza_reautenticacao_e_nao_propaga() -> None:
    """3 falhas de refresh -> flag + callback observaveis; erro isolado."""
    chamado: list[bool] = []
    results = [
        {
            "item_id": "MLBBBB",
            "seller_id": SELLER_CONFIAVEL,
            "price": 1053.0,
            "currency_id": "BRL",
            "condition": "new",
        }
    ]
    fake = _fake_catalogo(
        results,
        {SELLER_CONFIAVEL: _USER_CONFIAVEL},
        unauthorized_ate=1,  # dispara o refresh na 1a chamada (products/name)
        refresh_status=400,  # todo refresh falha
    )
    cliente = _fazer_cliente(
        fake, on_reautenticacao=lambda: chamado.append(True)
    )
    adapter = MercadoLivre(cliente)

    resultado = adapter.buscar_preco(PRODUCT_ID)

    assert resultado.sucesso is False
    assert resultado.erro == "reautenticação manual necessária"
    assert resultado.preco is None

    assert cliente.reautenticacao_manual_necessaria is True
    assert chamado == [True]
    assert len(fake.post_calls) == MAX_TENTATIVAS_REFRESH

    # Credenciais/tokens PRESERVADOS (nao corrompidos) apos a falha.
    assert cliente.access_token == "access-inicial"
    assert cliente.refresh_token == "refresh-inicial"


def test_reputacao_com_refresh_falho_tambem_isola_o_erro() -> None:
    """buscar_reputacao_vendedor tambem nunca propaga em falha de refresh."""
    results = [
        {
            "item_id": "MLBBBB",
            "seller_id": SELLER_CONFIAVEL,
            "price": 1053.0,
            "currency_id": "BRL",
            "condition": "new",
        }
    ]
    fake = _fake_catalogo(
        results,
        {SELLER_CONFIAVEL: _USER_CONFIAVEL},
        unauthorized_ate=1,
        refresh_status=400,
    )
    cliente = _fazer_cliente(fake)
    adapter = MercadoLivre(cliente)

    resultado = adapter.buscar_reputacao_vendedor(PRODUCT_ID)

    assert resultado.sucesso is False
    assert resultado.erro == "reautenticação manual necessária"
    assert cliente.reautenticacao_manual_necessaria is True


# ===========================================================================
# 7) Sanidade: endpoints de catalogo resolvidos contra API_BASE
# ===========================================================================


def test_endpoints_de_catalogo_resolvidos_contra_api_base() -> None:
    """As chamadas usam /products e /users (nunca /items/{id})."""
    results = [
        {
            "item_id": "MLBBBB",
            "seller_id": SELLER_CONFIAVEL,
            "price": 1053.0,
            "currency_id": "BRL",
            "condition": "new",
        }
    ]
    fake = _fake_catalogo(results, {SELLER_CONFIAVEL: _USER_CONFIAVEL})
    adapter = MercadoLivre(_fazer_cliente(fake))

    adapter.buscar_preco(PRODUCT_ID)

    urls = [url for _, url, _ in fake.request_calls]
    assert f"{API_BASE}/products/{PRODUCT_ID}" in urls
    assert f"{API_BASE}/products/{PRODUCT_ID}/items" in urls
    assert f"{API_BASE}/users/{SELLER_CONFIAVEL}" in urls
    # Nenhuma chamada ao endpoint proibido /items/{id}.
    assert not any(f"{API_BASE}/items/" in u for u in urls)
    assert MercadoLivre.site == Site.MERCADO_LIVRE
