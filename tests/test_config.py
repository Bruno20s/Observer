"""Testes de validacao da configuracao (`src/config.py`).

Cobre a tarefa 2.2 do plano do Rastreador de Precos (MVP):

- Todas as 8 chaves obrigatorias presentes -> `carregar_config` retorna um
  `Config` valido com as credenciais lidas do ambiente injetado (R7.1).
- Qualquer chave obrigatoria ausente ou vazia -> interrompe a inicializacao
  levantando `ConfigError` e loga qual(is) chave(s) falta(m) (R7.3).
- O arquivo `.env.example` versionado lista exatamente os nomes das 8 chaves,
  com valores vazios/ficticios (R7.2).

Nenhuma credencial real e usada: os testes injetam um mapeamento de ambiente
(`env=`) para nao tocar em `os.environ`, conforme o design (config injetavel
para testabilidade).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.config import (
    Config,
    ConfigError,
    Credenciais,
    REQUIRED_ENV_KEYS,
    carregar_config,
)

# Raiz do projeto: tests/ -> raiz. Usada para localizar o .env.example real.
_RAIZ_PROJETO = Path(__file__).resolve().parent.parent
_ENV_EXAMPLE = _RAIZ_PROJETO / ".env.example"


def _env_completo() -> dict[str, str]:
    """Mapeamento de ambiente com todas as 8 chaves preenchidas (valores ficticios)."""
    return {chave: f"valor-{chave.lower()}" for chave in REQUIRED_ENV_KEYS}


# ---------------------------------------------------------------------------
# R7.1 - todas as 8 chaves presentes -> ok
# ---------------------------------------------------------------------------


def test_todas_as_chaves_presentes_carrega_config():
    env = _env_completo()

    config = carregar_config(env=env)

    assert isinstance(config, Config)
    assert isinstance(config.credenciais, Credenciais)
    assert config.credenciais.ml_client_id == env["ML_CLIENT_ID"]
    assert config.credenciais.ml_client_secret == env["ML_CLIENT_SECRET"]
    assert config.credenciais.ml_redirect_uri == env["ML_REDIRECT_URI"]
    assert config.credenciais.ml_access_token == env["ML_ACCESS_TOKEN"]
    assert config.credenciais.ml_refresh_token == env["ML_REFRESH_TOKEN"]
    assert config.credenciais.email_remetente == env["EMAIL_REMETENTE"]
    assert config.credenciais.email_senha_app == env["EMAIL_SENHA_APP"]
    assert config.credenciais.email_destinatario == env["EMAIL_DESTINATARIO"]


def test_valores_sao_trimados():
    env = {chave: f"  {chave}-valor  " for chave in REQUIRED_ENV_KEYS}

    config = carregar_config(env=env)

    # Espacos nas extremidades sao removidos ao ler cada credencial.
    assert config.credenciais.ml_client_id == "ML_CLIENT_ID-valor"
    assert config.credenciais.email_destinatario == "EMAIL_DESTINATARIO-valor"


def test_ha_exatamente_oito_chaves_obrigatorias():
    # A fonte de verdade do numero de chaves obrigatorias e REQUIRED_ENV_KEYS.
    assert len(REQUIRED_ENV_KEYS) == 8
    assert len(set(REQUIRED_ENV_KEYS)) == 8  # sem duplicatas


# ---------------------------------------------------------------------------
# R7.3 - chave ausente/vazia -> interrompe (ConfigError) e loga a chave
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chave_ausente", list(REQUIRED_ENV_KEYS))
def test_chave_ausente_interrompe_e_loga(chave_ausente, caplog):
    env = _env_completo()
    del env[chave_ausente]

    with caplog.at_level(logging.ERROR, logger="src.config"):
        with pytest.raises(ConfigError) as exc_info:
            carregar_config(env=env)

    # A mensagem de erro e o log identificam a chave que falta (R7.3).
    assert chave_ausente in str(exc_info.value)
    assert any(chave_ausente in registro.getMessage() for registro in caplog.records)
    assert any(registro.levelno == logging.ERROR for registro in caplog.records)


@pytest.mark.parametrize("valor_invalido", ["", "   ", "\t\n"])
def test_chave_vazia_ou_so_espacos_interrompe(valor_invalido, caplog):
    env = _env_completo()
    env["EMAIL_SENHA_APP"] = valor_invalido

    with caplog.at_level(logging.ERROR, logger="src.config"):
        with pytest.raises(ConfigError) as exc_info:
            carregar_config(env=env)

    assert "EMAIL_SENHA_APP" in str(exc_info.value)
    assert any(
        "EMAIL_SENHA_APP" in registro.getMessage() for registro in caplog.records
    )


def test_multiplas_chaves_ausentes_sao_todas_logadas(caplog):
    env = _env_completo()
    del env["ML_CLIENT_ID"]
    del env["EMAIL_REMETENTE"]

    with caplog.at_level(logging.ERROR, logger="src.config"):
        with pytest.raises(ConfigError) as exc_info:
            carregar_config(env=env)

    mensagem = str(exc_info.value)
    assert "ML_CLIENT_ID" in mensagem
    assert "EMAIL_REMETENTE" in mensagem


def test_env_completamente_vazio_interrompe():
    with pytest.raises(ConfigError):
        carregar_config(env={})


# ---------------------------------------------------------------------------
# R7.2 - .env.example lista exatamente os nomes das 8 chaves
# ---------------------------------------------------------------------------


def _chaves_do_env_example() -> dict[str, str]:
    """Le o `.env.example` real e retorna o mapeamento chave -> valor declarado."""
    conteudo = _ENV_EXAMPLE.read_text(encoding="utf-8")
    chaves: dict[str, str] = {}
    for linha in conteudo.splitlines():
        texto = linha.strip()
        if not texto or texto.startswith("#") or "=" not in texto:
            continue
        chave, _, valor = texto.partition("=")
        chaves[chave.strip()] = valor.strip()
    return chaves


def test_env_example_existe():
    assert _ENV_EXAMPLE.is_file(), f".env.example nao encontrado em {_ENV_EXAMPLE}"


def test_env_example_lista_exatamente_as_oito_chaves():
    chaves = _chaves_do_env_example()

    # Nem falta nenhuma chave obrigatoria, nem sobra chave extra (R7.2).
    assert set(chaves) == set(REQUIRED_ENV_KEYS)
    assert len(chaves) == 8


def test_env_example_nao_contem_credenciais_em_texto_puro():
    # R7.2: apenas nomes das chaves, com valores vazios/ficticios (nao reais).
    chaves = _chaves_do_env_example()
    for chave, valor in chaves.items():
        assert valor == "", (
            f"{chave} deveria ter valor vazio no .env.example, obtido: {valor!r}"
        )
