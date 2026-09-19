"""Cliente OAuth2 do Mercado Livre (MVP).

Este modulo implementa a camada de autenticacao/HTTP do adapter do Mercado
Livre, conforme a secao "Fluxo OAuth do Mercado Livre (R7.4, R7.5)" do design.
Ele encapsula:

1. Chamadas HTTP autenticadas com ``Authorization: Bearer <access_token>``.
2. Renovacao automatica do ``access_token`` via ``refresh_token``
   (``grant_type=refresh_token``) ao detectar token expirado (HTTP 401), dentro
   de um timeout (default 30s), seguida de UMA nova tentativa da chamada
   original (R7.4).
3. Rotacao do ``refresh_token`` EM MEMORIA: o Mercado Livre retorna um novo
   ``refresh_token`` a cada refresh; o mais recente e usado nas proximas
   renovacoes. Nenhum segredo e escrito em disco.
4. Apos 3 tentativas consecutivas de refresh sem sucesso (R7.5): loga o erro,
   sinaliza de forma OBSERVAVEL que uma reautenticacao manual e necessaria
   (via a flag :attr:`ClienteOAuthML.reautenticacao_manual_necessaria`, a
   excecao :class:`ReautenticacaoManualNecessaria` e um callback opcional) e
   PRESERVA a configuracao/credenciais ja carregadas (nao corrompe nem limpa).

O ``Credenciais`` (``src/config.py``) e um dataclass ``frozen`` — imutavel. Por
isso os tokens correntes sao mantidos em um estado mutavel proprio deste
cliente (:attr:`ClienteOAuthML.access_token` / :attr:`refresh_token`),
inicializado a partir das credenciais, SEM mutar o dataclass frozen.

A camada HTTP e injetavel/mockavel: o cliente aceita um ``httpx.Client`` (ou
qualquer objeto compativel). Isso permite testar 401->refresh->retry e a falha
de refresh 3x sem rede real (task 7.3). NUNCA ha rede real nos testes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional, Protocol

import httpx

from ..config import Config, Credenciais

logger = logging.getLogger(__name__)

#: Endpoint de token OAuth do Mercado Livre (design.md).
TOKEN_ENDPOINT = "https://api.mercadolibre.com/oauth/token"
#: Base da API do Mercado Livre (design.md).
API_BASE = "https://api.mercadolibre.com"
#: Numero maximo de tentativas de refresh consecutivas antes de sinalizar
#: reautenticacao manual (R7.5).
MAX_TENTATIVAS_REFRESH = 3
#: Timeout (segundos) da chamada de refresh de token (R7.4).
REFRESH_TIMEOUT_SEGUNDOS = 30.0


class ReautenticacaoManualNecessaria(RuntimeError):
    """Sinaliza que o refresh do token falhou de forma irrecuperavel.

    Levantada apos ``MAX_TENTATIVAS_REFRESH`` tentativas consecutivas de refresh
    sem sucesso (R7.5). O caller pode capturar esta excecao para converter a
    falha em ``PrecoResult.sucesso=False`` (isolamento por item), enquanto a
    flag observavel :attr:`ClienteOAuthML.reautenticacao_manual_necessaria`
    permanece ``True`` ate que uma reautenticacao manual ocorra.

    As credenciais/configuracao originais sao PRESERVADAS (nao corrompidas).
    """


class SuporteHTTP(Protocol):
    """Contrato minimo do cliente HTTP injetavel.

    ``httpx.Client`` satisfaz este protocolo. Nos testes, um fake que implemente
    ``request`` e ``post`` permite exercitar o fluxo sem rede real.
    """

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        ...

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        ...


class ClienteOAuthML:
    """Cliente HTTP autenticado do Mercado Livre com refresh automatico.

    Mantem ``access_token`` e ``refresh_token`` correntes EM MEMORIA,
    inicializados a partir de ``Credenciais`` (frozen). O ``client_id`` e
    ``client_secret`` sao usados na renovacao do token.

    Uso tipico::

        cliente = ClienteOAuthML.from_config(config)
        resp = cliente.get(f"/items/{item_id}")
        dados = resp.json()

    Args:
        client_id: ML_CLIENT_ID.
        client_secret: ML_CLIENT_SECRET.
        access_token: token de acesso inicial (rotacionado em memoria).
        refresh_token: token de refresh inicial (rotacionado em memoria).
        http_client: cliente HTTP injetavel (``httpx.Client`` por padrao). Torna
            a camada de rede mockavel nos testes.
        timeout: timeout em segundos das requisicoes (default 30s, R7.4).
        on_reautenticacao_manual: callback opcional invocado uma vez quando o
            estado de reautenticacao manual e atingido (R7.5), permitindo ao
            caller observar/reagir (ex.: notificar o usuario).
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str,
        http_client: Optional[SuporteHTTP] = None,
        timeout: float = REFRESH_TIMEOUT_SEGUNDOS,
        on_reautenticacao_manual: Optional[Callable[[], None]] = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        # Tokens correntes: estado MUTAVEL em memoria (nunca em disco).
        self.access_token = access_token
        self.refresh_token = refresh_token

        self._timeout = timeout
        self._owns_http = http_client is None
        self._http: SuporteHTTP = http_client or httpx.Client(timeout=timeout)
        self._on_reautenticacao_manual = on_reautenticacao_manual

        #: Flag observavel: True apos 3 falhas de refresh (R7.5). Permanece True
        #: ate uma reautenticacao manual atualizar os tokens.
        self.reautenticacao_manual_necessaria = False

    # ------------------------------------------------------------------
    # Construtores auxiliares
    # ------------------------------------------------------------------

    @classmethod
    def from_credenciais(
        cls,
        credenciais: Credenciais,
        *,
        http_client: Optional[SuporteHTTP] = None,
        timeout: float = REFRESH_TIMEOUT_SEGUNDOS,
        on_reautenticacao_manual: Optional[Callable[[], None]] = None,
    ) -> "ClienteOAuthML":
        """Cria o cliente a partir de ``Credenciais`` (frozen).

        Os tokens sao COPIADOS para o estado mutavel do cliente; o dataclass
        frozen nunca e alterado.
        """
        return cls(
            client_id=credenciais.ml_client_id,
            client_secret=credenciais.ml_client_secret,
            access_token=credenciais.ml_access_token,
            refresh_token=credenciais.ml_refresh_token,
            http_client=http_client,
            timeout=timeout,
            on_reautenticacao_manual=on_reautenticacao_manual,
        )

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        http_client: Optional[SuporteHTTP] = None,
        on_reautenticacao_manual: Optional[Callable[[], None]] = None,
    ) -> "ClienteOAuthML":
        """Cria o cliente a partir de ``Config``.

        Usa o timeout de consulta operacional (default 30s, R3.6/R7.4).
        """
        return cls.from_credenciais(
            config.credenciais,
            http_client=http_client,
            timeout=config.parametros.timeout_consulta_segundos,
            on_reautenticacao_manual=on_reautenticacao_manual,
        )

    # ------------------------------------------------------------------
    # API publica de requisicao
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET autenticado. Ver :meth:`request`."""
        return self.request("GET", path, **kwargs)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Faz uma chamada autenticada, renovando o token em 401 (R7.4).

        - Envia ``Authorization: Bearer <access_token>``.
        - Se a resposta for 401 (token expirado), renova via refresh_token e
          REPETE a chamada original UMA vez com o novo token.
        - Se o refresh falhar 3x consecutivas, levanta
          :class:`ReautenticacaoManualNecessaria` (R7.5).

        Args:
            method: metodo HTTP (ex.: ``"GET"``).
            path: caminho relativo (ex.: ``"/items/MLB123"``) ou URL absoluta.
            **kwargs: repassados ao cliente HTTP subjacente.

        Returns:
            A ``httpx.Response`` da chamada (possivelmente apos o retry).

        Raises:
            ReautenticacaoManualNecessaria: apos 3 falhas de refresh (R7.5).
        """
        url = self._resolver_url(path)

        resposta = self._request_autenticado(method, url, kwargs)
        if resposta.status_code != httpx.codes.UNAUTHORIZED:
            return resposta

        # 401 -> token expirado: renova e repete a chamada original UMA vez.
        logger.info(
            "Token de acesso expirado (401) em %s %s; renovando via refresh_token.",
            method,
            url,
        )
        self._renovar_token()
        return self._request_autenticado(method, url, kwargs)

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _resolver_url(self, path: str) -> str:
        """Resolve ``path`` relativo contra ``API_BASE`` (ou usa URL absoluta)."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return API_BASE + path

    def _request_autenticado(
        self, method: str, url: str, kwargs: Mapping[str, Any]
    ) -> httpx.Response:
        """Executa a requisicao injetando o header Bearer corrente."""
        opcoes = dict(kwargs)
        headers = dict(opcoes.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.access_token}"
        return self._http.request(method, url, headers=headers, **opcoes)

    def _renovar_token(self) -> None:
        """Renova o ``access_token`` via ``refresh_token`` (R7.4, R7.5).

        Tenta ate ``MAX_TENTATIVAS_REFRESH`` vezes. Em sucesso, atualiza os
        tokens correntes em memoria (rotacionando o refresh_token quando o ML
        retorna um novo) e limpa a flag de reautenticacao. Apos todas as
        tentativas falharem, loga, ativa a flag observavel, invoca o callback
        (uma vez) e levanta :class:`ReautenticacaoManualNecessaria`, PRESERVANDO
        os tokens/credenciais atuais.
        """
        ultimo_erro: Optional[BaseException] = None

        for tentativa in range(1, MAX_TENTATIVAS_REFRESH + 1):
            try:
                resposta = self._http.post(
                    TOKEN_ENDPOINT,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self.refresh_token,
                    },
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:  # rede/timeout
                ultimo_erro = exc
                logger.warning(
                    "Falha de refresh do token (tentativa %d/%d): %s",
                    tentativa,
                    MAX_TENTATIVAS_REFRESH,
                    exc,
                )
                continue

            if resposta.status_code == httpx.codes.OK:
                dados = resposta.json()
                novo_access = dados.get("access_token")
                if not novo_access:
                    ultimo_erro = ValueError(
                        "resposta de refresh sem 'access_token'"
                    )
                    logger.warning(
                        "Refresh do token retornou 200 sem access_token "
                        "(tentativa %d/%d).",
                        tentativa,
                        MAX_TENTATIVAS_REFRESH,
                    )
                    continue

                # Sucesso: atualiza tokens correntes EM MEMORIA.
                self.access_token = novo_access
                # ML rotaciona o refresh_token; usa o novo quando presente.
                novo_refresh = dados.get("refresh_token")
                if novo_refresh:
                    self.refresh_token = novo_refresh
                self.reautenticacao_manual_necessaria = False
                logger.info(
                    "Access token renovado com sucesso na tentativa %d.",
                    tentativa,
                )
                return

            # Status != 200: tenta novamente ate o limite.
            ultimo_erro = httpx.HTTPStatusError(
                f"refresh retornou HTTP {resposta.status_code}",
                request=resposta.request,
                response=resposta,
            )
            logger.warning(
                "Refresh do token retornou HTTP %d (tentativa %d/%d).",
                resposta.status_code,
                tentativa,
                MAX_TENTATIVAS_REFRESH,
            )

        # Esgotou as tentativas: sinaliza reautenticacao manual (R7.5).
        self._sinalizar_reautenticacao_manual(ultimo_erro)
        raise ReautenticacaoManualNecessaria(
            "Falha ao renovar o access_token do Mercado Livre apos "
            f"{MAX_TENTATIVAS_REFRESH} tentativas; reautenticacao manual "
            "necessaria."
        ) from ultimo_erro

    def _sinalizar_reautenticacao_manual(
        self, ultimo_erro: Optional[BaseException]
    ) -> None:
        """Ativa o estado observavel de reautenticacao manual (R7.5).

        Loga em nivel ERROR, ativa a flag e invoca o callback opcional uma vez.
        NAO altera ``access_token``/``refresh_token`` nem as credenciais: a
        configuracao carregada e preservada.
        """
        logger.error(
            "Reautenticacao manual necessaria: o refresh do token do Mercado "
            "Livre falhou %d vezes consecutivas. Ultimo erro: %s",
            MAX_TENTATIVAS_REFRESH,
            ultimo_erro,
        )
        ja_sinalizado = self.reautenticacao_manual_necessaria
        self.reautenticacao_manual_necessaria = True
        if not ja_sinalizado and self._on_reautenticacao_manual is not None:
            try:
                self._on_reautenticacao_manual()
            except Exception:  # callback do caller nunca derruba o cliente
                logger.exception(
                    "Callback on_reautenticacao_manual levantou excecao; "
                    "ignorada para preservar o isolamento de erro."
                )

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Fecha o cliente HTTP subjacente se este cliente o criou."""
        if self._owns_http:
            fechar = getattr(self._http, "close", None)
            if callable(fechar):
                fechar()

    def __enter__(self) -> "ClienteOAuthML":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
