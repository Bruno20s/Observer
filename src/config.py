"""Configuracao do Rastreador de Precos (MVP).

Este modulo centraliza:

1. A leitura das 8 variaveis de ambiente OBRIGATORIAS (credenciais/config
   sensiveis do Mercado Livre e do e-mail SMTP), sem valores default embutidos
   no codigo (R7.1). Na inicializacao, valida presenca e nao-vazio de cada
   uma; se qualquer chave faltar, loga quais chaves estao ausentes e interrompe
   a inicializacao levantando ``ConfigError`` (R7.3).
2. Parametros operacionais configuraveis com defaults sensatos (R3.4, R3.5,
   R3.6, R3.7): intervalo minimo entre consultas, frequencia do job, timeout de
   consulta e o intervalo de atraso aleatorio da Amazon.

Nenhuma credencial e escrita em texto puro aqui: tudo vem do ambiente
(ver ``.env.example``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Chaves obrigatorias de ambiente (R7.1)
# ---------------------------------------------------------------------------

#: Nomes das 8 variaveis de ambiente obrigatorias. A ordem e a fonte de
#: verdade tanto para leitura quanto para a mensagem de erro de validacao.
REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "ML_CLIENT_ID",
    "ML_CLIENT_SECRET",
    "ML_REDIRECT_URI",
    "ML_ACCESS_TOKEN",
    "ML_REFRESH_TOKEN",
    "EMAIL_REMETENTE",
    "EMAIL_SENHA_APP",
    "EMAIL_DESTINATARIO",
)

# ---------------------------------------------------------------------------
# Defaults dos parametros operacionais (R3.4, R3.5, R3.6, R3.7)
# ---------------------------------------------------------------------------

_SEGUNDOS_POR_HORA = 3600

#: Intervalo minimo padrao entre consultas ao mesmo ProdutoSite: 4 horas (R3.4).
DEFAULT_INTERVALO_MINIMO_SEGUNDOS = 4 * _SEGUNDOS_POR_HORA
#: Minimo permitido para o intervalo minimo: 1 hora (R3.4).
MINIMO_INTERVALO_MINIMO_SEGUNDOS = 1 * _SEGUNDOS_POR_HORA
#: Frequencia padrao de execucao do Job_Monitor: 4 horas (R3.5).
DEFAULT_FREQUENCIA_JOB_SEGUNDOS = 4 * _SEGUNDOS_POR_HORA
#: Timeout padrao de uma consulta individual: 30 segundos (R3.6).
DEFAULT_TIMEOUT_CONSULTA_SEGUNDOS = 30.0
#: Intervalo padrao de atraso aleatorio da Amazon: entre 2 e 8 segundos (R3.7).
DEFAULT_AMAZON_DELAY_MIN_SEGUNDOS = 2.0
DEFAULT_AMAZON_DELAY_MAX_SEGUNDOS = 8.0


class ConfigError(RuntimeError):
    """Erro de configuracao que impede a inicializacao do sistema.

    Levantado quando uma ou mais variaveis de ambiente obrigatorias estao
    ausentes ou vazias (R7.3).
    """


@dataclass(frozen=True)
class Credenciais:
    """Credenciais sensiveis lidas exclusivamente do ambiente (R7.1).

    Nenhum campo possui valor default: todos sao obrigatorios e vem do
    ambiente. Instancias sao imutaveis (``frozen``) para evitar mutacao
    acidental das credenciais em runtime.
    """

    ml_client_id: str
    ml_client_secret: str
    ml_redirect_uri: str
    ml_access_token: str
    ml_refresh_token: str
    email_remetente: str
    email_senha_app: str
    email_destinatario: str


@dataclass(frozen=True)
class ParametrosOperacionais:
    """Parametros operacionais configuraveis com defaults (R3.4-R3.7).

    Todos os tempos sao expressos em segundos.
    """

    intervalo_minimo_segundos: int = DEFAULT_INTERVALO_MINIMO_SEGUNDOS
    frequencia_job_segundos: int = DEFAULT_FREQUENCIA_JOB_SEGUNDOS
    timeout_consulta_segundos: float = DEFAULT_TIMEOUT_CONSULTA_SEGUNDOS
    amazon_delay_min_segundos: float = DEFAULT_AMAZON_DELAY_MIN_SEGUNDOS
    amazon_delay_max_segundos: float = DEFAULT_AMAZON_DELAY_MAX_SEGUNDOS

    def __post_init__(self) -> None:
        # R3.4: o intervalo minimo nao pode ser inferior a 1 hora.
        if self.intervalo_minimo_segundos < MINIMO_INTERVALO_MINIMO_SEGUNDOS:
            raise ConfigError(
                "Intervalo_Minimo deve ser >= "
                f"{MINIMO_INTERVALO_MINIMO_SEGUNDOS}s (1 hora); recebido "
                f"{self.intervalo_minimo_segundos}s."
            )
        if self.frequencia_job_segundos <= 0:
            raise ConfigError(
                "frequencia do job deve ser > 0s; recebido "
                f"{self.frequencia_job_segundos}s."
            )
        if self.timeout_consulta_segundos <= 0:
            raise ConfigError(
                "timeout de consulta deve ser > 0s; recebido "
                f"{self.timeout_consulta_segundos}s."
            )
        if self.amazon_delay_min_segundos < 0:
            raise ConfigError(
                "atraso minimo da Amazon deve ser >= 0s; recebido "
                f"{self.amazon_delay_min_segundos}s."
            )
        if self.amazon_delay_max_segundos < self.amazon_delay_min_segundos:
            raise ConfigError(
                "atraso maximo da Amazon deve ser >= atraso minimo; recebido "
                f"min={self.amazon_delay_min_segundos}s, "
                f"max={self.amazon_delay_max_segundos}s."
            )


@dataclass(frozen=True)
class Config:
    """Configuracao completa do sistema: credenciais + parametros."""

    credenciais: Credenciais
    parametros: ParametrosOperacionais = field(default_factory=ParametrosOperacionais)


def _validar_chaves_obrigatorias(env: Mapping[str, str]) -> Credenciais:
    """Le e valida as 7 chaves obrigatorias a partir de ``env``.

    Uma chave e considerada valida quando esta presente e nao e vazia apos
    remover espacos em branco nas extremidades. Se qualquer chave faltar,
    loga a lista completa de chaves ausentes e levanta ``ConfigError`` (R7.3).
    """
    valores: dict[str, str] = {}
    ausentes: list[str] = []

    for chave in REQUIRED_ENV_KEYS:
        bruto = env.get(chave)
        if bruto is None or bruto.strip() == "":
            ausentes.append(chave)
        else:
            valores[chave] = bruto.strip()

    if ausentes:
        # R7.3: logar quais chaves faltam e interromper a inicializacao.
        logger.error(
            "Inicializacao interrompida: variaveis de ambiente obrigatorias "
            "ausentes ou vazias: %s",
            ", ".join(ausentes),
        )
        raise ConfigError(
            "Variaveis de ambiente obrigatorias ausentes ou vazias: "
            + ", ".join(ausentes)
        )

    return Credenciais(
        ml_client_id=valores["ML_CLIENT_ID"],
        ml_client_secret=valores["ML_CLIENT_SECRET"],
        ml_redirect_uri=valores["ML_REDIRECT_URI"],
        ml_access_token=valores["ML_ACCESS_TOKEN"],
        ml_refresh_token=valores["ML_REFRESH_TOKEN"],
        email_remetente=valores["EMAIL_REMETENTE"],
        email_senha_app=valores["EMAIL_SENHA_APP"],
        email_destinatario=valores["EMAIL_DESTINATARIO"],
    )


def _carregar_dotenv(caminho: Path) -> None:
    """Carrega pares chave=valor de um arquivo ``.env`` em ``os.environ``.

    Loader minimo e sem dependencias externas: ignora linhas em branco e
    comentarios (``#``), aceita o prefixo opcional ``export`` e remove aspas
    simples/duplas ao redor do valor. Variaveis ja definidas no ambiente tem
    precedencia (nao sao sobrescritas), permitindo override via shell/CI.
    """
    try:
        conteudo = caminho.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Nao foi possivel ler o arquivo .env %s: %s", caminho, exc)
        return

    for linha in conteudo.splitlines():
        texto = linha.strip()
        if not texto or texto.startswith("#"):
            continue
        if texto.startswith("export "):
            texto = texto[len("export ") :].strip()
        if "=" not in texto:
            continue
        chave, _, valor = texto.partition("=")
        chave = chave.strip()
        valor = valor.strip().strip("'").strip('"')
        if chave and chave not in os.environ:
            os.environ[chave] = valor


def carregar_config(
    env: Mapping[str, str] | None = None,
    *,
    parametros: ParametrosOperacionais | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> Config:
    """Carrega e valida a configuracao completa do sistema.

    Args:
        env: Mapeamento de variaveis de ambiente a usar. Se ``None``, usa
            ``os.environ`` (comportamento normal em runtime). Injetar um
            mapeamento facilita os testes sem tocar no ambiente real.
        parametros: Parametros operacionais ja construidos. Se ``None``, usa os
            defaults (R3.4-R3.7).
        dotenv_path: Caminho opcional de um arquivo ``.env`` para carregar em
            ``os.environ`` antes da leitura. So e utilizado quando ``env`` e
            ``None``. Se o arquivo nao existir, e ignorado silenciosamente.

    Returns:
        Config validado.

    Raises:
        ConfigError: se qualquer chave obrigatoria estiver ausente/vazia (R7.3)
            ou se um parametro operacional for invalido (R3.4).
    """
    if env is None:
        if dotenv_path is not None:
            caminho = Path(dotenv_path)
            if caminho.is_file():
                _carregar_dotenv(caminho)
        env = os.environ

    credenciais = _validar_chaves_obrigatorias(env)
    parametros = parametros if parametros is not None else ParametrosOperacionais()
    return Config(credenciais=credenciais, parametros=parametros)
