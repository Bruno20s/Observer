#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ferramenta de setup UNICO: obter os tokens OAuth iniciais do Mercado Livre.

Este script NAO faz parte do sistema principal (o adapter, o job e o
notificador nunca o importam). Ele existe apenas para executar, uma vez, o
fluxo OAuth 2.0 Authorization Code + PKCE do Mercado Livre e imprimir o
ML_ACCESS_TOKEN e o ML_REFRESH_TOKEN iniciais para voce colar no .env. Depois
disso, o proprio sistema renova o access token sozinho via refresh token
(ver src/adapters/ml_oauth.py).

Le apenas ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI do .env (mesma
interface de credenciais definida em tech.md). NAO usa
src.config.carregar_config de proposito: aquela funcao exige as 8 chaves
preenchidas (incluindo e-mail e os proprios tokens que ainda nao existem), o
que impediria rodar este utilitario justamente quando ele e necessario.

Uso:

    python scripts/obter_tokens_ml.py
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

# Token endpoint: o mesmo usado pelo sistema (ver src/adapters/ml_oauth.py).
TOKEN_ENDPOINT = "https://api.mercadolibre.com/oauth/token"
# Endpoint de autorizacao: dominio de auth por pais. Brasil = .com.br.
# Ajuste AUTH_HOST se sua conta/aplicacao for de outro pais (ex.: .com.ar).
AUTH_HOST = "https://auth.mercadolivre.com.br"
AUTH_PATH = "/authorization"

CHAVES_NECESSARIAS = ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REDIRECT_URI")

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
CAMINHO_ENV = RAIZ_PROJETO / ".env"


def _ler_env(caminho: Path) -> dict:
    """Le pares chave=valor de um .env (parser minimo, sem dependencia)."""
    valores: dict[str, str] = {}
    if caminho.is_file():
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            texto = linha.strip()
            if not texto or texto.startswith("#") or "=" not in texto:
                continue
            if texto.startswith("export "):
                texto = texto[len("export "):].strip()
            chave, _, valor = texto.partition("=")
            valores[chave.strip()] = valor.strip().strip("'").strip('"')
    for chave in CHAVES_NECESSARIAS:
        if os.environ.get(chave):
            valores[chave] = os.environ[chave]
    return valores


def _gerar_pkce() -> tuple:
    """Gera (code_verifier, code_challenge) para PKCE com metodo S256."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _montar_url_autorizacao(client_id: str, redirect_uri: str, code_challenge: str, state: str) -> str:
    """Monta a URL de autorizacao (Authorization Code + PKCE S256)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return AUTH_HOST + AUTH_PATH + "?" + urlencode(params)


def _extrair_code(entrada: str) -> str:
    """Extrai o parametro code de uma URL de redirect, ou aceita o code direto."""
    entrada = entrada.strip()
    if not entrada:
        return ""
    if entrada.lower().startswith("http"):
        query = parse_qs(urlparse(entrada).query)
        return query.get("code", [""])[0]
    if entrada.startswith("code="):
        return entrada[len("code="):].strip()
    return entrada


def _trocar_code_por_tokens(client_id, client_secret, redirect_uri, code, code_verifier) -> dict:
    """Troca o authorization code por tokens em /oauth/token (inclui code_verifier)."""
    dados = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    resposta = httpx.post(TOKEN_ENDPOINT, data=dados, headers=headers, timeout=30.0)
    if resposta.status_code != 200:
        raise SystemExit("Falha na troca do code por tokens (HTTP " + str(resposta.status_code) + "): " + resposta.text)
    return resposta.json()


def main() -> None:
    print("== Setup de tokens OAuth do Mercado Livre (PKCE) ==\n")

    env = _ler_env(CAMINHO_ENV)
    faltando = [c for c in CHAVES_NECESSARIAS if not env.get(c)]
    if faltando:
        raise SystemExit("Preencha estas chaves no .env antes de rodar: " + ", ".join(faltando))

    client_id = env["ML_CLIENT_ID"]
    client_secret = env["ML_CLIENT_SECRET"]
    redirect_uri = env["ML_REDIRECT_URI"]

    # Checagem de sanidade: Client ID e Client Secret NAO podem ser iguais.
    if client_id == client_secret:
        raise SystemExit(
            "ML_CLIENT_ID e ML_CLIENT_SECRET estao IDENTICOS no .env -- quase "
            "certamente erro de copia. Reconfira o Client Secret no DevCenter "
            "do Mercado Livre e cole o valor correto antes de continuar."
        )

    code_verifier, code_challenge = _gerar_pkce()
    state = secrets.token_urlsafe(16)
    url = _montar_url_autorizacao(client_id, redirect_uri, code_challenge, state)

    print("1) Abra esta URL no navegador e autorize a aplicacao:\n")
    print(url)
    print()
    print("2) Apos autorizar, o Mercado Livre redireciona para o seu ML_REDIRECT_URI")
    print("   (" + redirect_uri + ") com um parametro ?code=... na URL.")
    print("   Copie a URL inteira do navegador (ou apenas o valor do code).\n")
    print("   [seguranca] state esperado no redirect: " + state)
    print("   (confira que o 'state' na URL de retorno e igual a este valor.)\n")

    entrada = input("3) Cole aqui a URL de redirect (ou o code) e tecle Enter: ")
    code = _extrair_code(entrada)
    if not code:
        raise SystemExit("Nenhum 'code' foi identificado na entrada. Abortando.")

    print("\nTrocando o code por tokens...\n")
    tokens = _trocar_code_por_tokens(client_id, client_secret, redirect_uri, code, code_verifier)

    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    expira_em = tokens.get("expires_in")

    if not access_token or not refresh_token:
        raise SystemExit("Resposta do ML sem access_token/refresh_token: " + str(tokens))

    print("== Tokens obtidos com sucesso! Cole no seu .env: ==\n")
    print("ML_ACCESS_TOKEN=" + access_token)
    print("ML_REFRESH_TOKEN=" + refresh_token)
    print()
    if expira_em is not None:
        print("(access_token expira em ~" + str(expira_em) + "s; o sistema renova sozinho via refresh_token.)")
    print("\nNAO commite o .env. Guarde o refresh_token com cuidado.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(1)
