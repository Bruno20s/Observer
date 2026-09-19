"""Entrypoint da aplicacao — agendamento do Job_Monitor (`src/main.py`).

Este modulo conecta as pecas do MVP e coloca o sistema para rodar
(design.md -> "Fluxo do Job_Monitor" e diagrama de arquitetura -> no
``Scheduler``). Ele:

1. **Valida a config na inicializacao** (R7.3): chama
   :func:`~src.config.carregar_config`, que interrompe a inicializacao com
   :class:`~src.config.ConfigError` (logando qual chave falta) quando uma
   variavel de ambiente obrigatoria esta ausente/vazia. O entrypoint deixa esse
   erro **abortar** a inicializacao com um log claro.
2. **Inicializa o banco** (:func:`~src.db.database.inicializar_banco`).
3. **Monta o mapa de adapters** (Mercado Livre + Amazon) ja fiados com as
   credenciais/parametros da config.
4. **Registra** ``executar_ciclo`` no ``schedule`` na frequencia configuravel
   (``ParametrosOperacionais.frequencia_job_segundos``, default 4h — R3.5) e
   **inicia o loop** do agendador (``schedule.run_pending()`` num laco com sleep).

Testabilidade (task 12.2): as pecas sao fatoradas para permitir testar o
**registro** do job SEM sleep real e SEM o loop infinito:

- :func:`registrar_job` recebe um ``scheduler`` (``schedule.Scheduler``), o
  callable a agendar e a frequencia em segundos, registra exatamente **um** job
  no intervalo esperado e retorna o :class:`schedule.Job` criado. Nao dorme,
  nao roda o loop.
- :func:`loop_agendador` e uma funcao **separada** que executa o laco
  (``run_pending`` + ``sleep``). O ``sleep``, a condicao de parada e o proprio
  ``scheduler`` sao injetaveis, de modo que a task 12.2 possa cobrir o registro
  sem nunca entrar num laco infinito nem dormir de verdade.

Nada de rede real e disparado no import: os adapters/cliente OAuth so tocam a
rede quando ``executar_ciclo`` for de fato chamado pelo agendador.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Mapping, Optional

import schedule

from src.adapters.amazon import Amazon
from src.adapters.base import BaseAdapter, Site
from src.adapters.mercado_livre import MercadoLivre
from src.adapters.ml_oauth import ClienteOAuthML
from src.config import Config, ConfigError, carregar_config
from src.db import database
from src.jobs.monitor import executar_ciclo

logger = logging.getLogger(__name__)

#: Caminho padrao do arquivo SQLite do sistema. O banco e append-only e local
#: (VPS/Raspberry Pi); um arquivo no diretorio de execucao e suficiente para o
#: MVP. Pode ser sobrescrito ao chamar :func:`main`/:func:`inicializar_app`.
DEFAULT_DB_PATH = "rastreador.db"

#: Intervalo (em segundos) entre iteracoes do loop do agendador. O
#: ``schedule.run_pending()`` decide quando o job realmente roda; este sleep
#: apenas evita busy-wait. Mantido pequeno para responsividade.
DEFAULT_INTERVALO_LOOP_SEGUNDOS = 1.0


def construir_adapters(config: Config) -> dict[Site, BaseAdapter]:
    """Monta o mapa ``{Site: BaseAdapter}`` fiado com credenciais/parametros.

    - **Mercado Livre**: :class:`~src.adapters.mercado_livre.MercadoLivre` sobre
      um :class:`~src.adapters.ml_oauth.ClienteOAuthML` construido a partir da
      config (``ClienteOAuthML.from_config``), que carrega os tokens e faz o
      refresh automatico (R7.4/R7.5).
    - **Amazon**: :class:`~src.adapters.amazon.Amazon` com o intervalo de atraso
      aleatorio vindo da config (``amazon_delay_min/max_segundos`` — R3.7).

    O mapa e consumido por ``executar_ciclo`` para o roteamento por site (R3.1).

    Args:
        config: Configuracao validada (credenciais + parametros).

    Returns:
        Mapa de adapters por :class:`~src.adapters.base.Site`.
    """
    cliente_ml = ClienteOAuthML.from_config(config)
    return {
        Site.MERCADO_LIVRE: MercadoLivre(cliente_ml),
        Site.AMAZON: Amazon(
            delay_min_segundos=config.parametros.amazon_delay_min_segundos,
            delay_max_segundos=config.parametros.amazon_delay_max_segundos,
        ),
    }


def registrar_job(
    scheduler: schedule.Scheduler,
    job: Callable[[], None],
    frequencia_segundos: int,
) -> schedule.Job:
    """Registra ``job`` no ``scheduler`` na frequencia dada (R3.5).

    Fatorada de proposito para ser testavel sem rodar o loop nem dormir: apos a
    chamada, ``scheduler.jobs`` contem exatamente um job novo, agendado no
    intervalo ``frequencia_segundos`` (unidade ``seconds``). A frequencia em
    segundos e a fonte de verdade (``ParametrosOperacionais.frequencia_job_segundos``),
    entao usamos ``.seconds`` diretamente em vez de converter para horas.

    Args:
        scheduler: instancia de ``schedule.Scheduler`` (injetavel; o modulo
            ``schedule`` tambem expoe um scheduler default global, mas usar uma
            instancia mantem o registro isolado e testavel).
        job: callable sem argumentos a ser executado a cada intervalo. Tipicamente
            o wrapper :func:`criar_job_ciclo`, que ja captura ``conn``/``config``/
            ``adapters`` e isola excecoes do ciclo.
        frequencia_segundos: intervalo entre execucoes, em segundos (> 0).

    Returns:
        O :class:`schedule.Job` registrado.

    Raises:
        ValueError: se ``frequencia_segundos`` nao for > 0.
    """
    if frequencia_segundos <= 0:
        raise ValueError(
            "frequencia_segundos deve ser > 0; recebido "
            f"{frequencia_segundos}."
        )
    job_registrado = scheduler.every(frequencia_segundos).seconds.do(job)
    logger.info(
        "Job_Monitor registrado no agendador: a cada %ss.", frequencia_segundos
    )
    return job_registrado


def criar_job_ciclo(
    conn,
    config: Config,
    adapters: Mapping[Site, BaseAdapter],
) -> Callable[[], None]:
    """Cria o callable sem-argumentos executado pelo agendador a cada ciclo.

    Captura ``conn``/``config``/``adapters`` num closure e chama
    ``executar_ciclo``. Envolve a chamada num ``try/except`` de topo para que a
    excecao de **um** ciclo nao derrube o agendador — o proximo ciclo agendado
    roda normalmente. (``executar_ciclo`` ja isola erros por item; esta guarda e
    a rede de seguranca do nivel do job.)

    Args:
        conn: conexao SQLite aberta (schema ja criado).
        config: configuracao validada.
        adapters: mapa de adapters por site.

    Returns:
        Callable ``() -> None`` pronto para :func:`registrar_job`.
    """

    def _job() -> None:
        try:
            executar_ciclo(conn, config=config, adapters=adapters)
        except Exception:  # noqa: BLE001 -- um ciclo nunca derruba o agendador.
            logger.exception(
                "Erro nao tratado durante executar_ciclo; o agendador continua "
                "e o proximo ciclo sera executado normalmente."
            )

    return _job


def loop_agendador(
    scheduler: schedule.Scheduler,
    *,
    intervalo_loop_segundos: float = DEFAULT_INTERVALO_LOOP_SEGUNDOS,
    sleep: Callable[[float], None] = time.sleep,
    continuar: Callable[[], bool] = lambda: True,
) -> None:
    """Executa o laco do agendador: ``run_pending`` + ``sleep`` (R3.5).

    Separado de :func:`registrar_job` para que o registro possa ser testado sem
    entrar num laco infinito. ``sleep`` e ``continuar`` sao injetaveis: por
    padrao o laco roda indefinidamente com ``time.sleep``; nos testes pode-se
    passar um ``continuar`` que retorna ``False`` apos N iteracoes e um ``sleep``
    no-op para nunca dormir de verdade.

    Args:
        scheduler: instancia de ``schedule.Scheduler`` com o job ja registrado.
        intervalo_loop_segundos: tempo de espera entre verificacoes.
        sleep: seam de espera injetavel (default ``time.sleep``).
        continuar: predicado avaliado a cada iteracao; enquanto ``True``, o laco
            segue. Default: sempre ``True`` (loop infinito em producao).
    """
    while continuar():
        scheduler.run_pending()
        sleep(intervalo_loop_segundos)


def inicializar_app(
    *,
    db_path: str = DEFAULT_DB_PATH,
    scheduler: Optional[schedule.Scheduler] = None,
) -> tuple[schedule.Scheduler, object, Config]:
    """Valida a config, inicializa o banco, monta adapters e registra o job.

    Executa toda a sequencia de startup **sem** entrar no loop do agendador,
    para que o entrypoint (:func:`main`) — ou um teste — decida quando iniciar
    o laco. Se uma variavel de ambiente obrigatoria faltar, ``carregar_config``
    levanta :class:`~src.config.ConfigError` e a inicializacao e abortada (R7.3);
    aqui apenas logamos e deixamos o erro propagar.

    Args:
        db_path: caminho do arquivo SQLite (default :data:`DEFAULT_DB_PATH`).
        scheduler: ``schedule.Scheduler`` a usar; se ``None``, cria um novo.

    Returns:
        Tupla ``(scheduler, conn, config)`` com o job ja registrado.

    Raises:
        ConfigError: quando a validacao da config falha (R7.3).
    """
    # 1. Valida a config na inicializacao (R7.3). ConfigError aborta o startup.
    try:
        config = carregar_config()
    except ConfigError:
        logger.error(
            "Inicializacao abortada: configuracao invalida (ver log acima "
            "para a chave ausente)."
        )
        raise

    # 2. Inicializa o banco (schema idempotente).
    conn = database.inicializar_banco(db_path)

    # 3. Monta o mapa de adapters (ML + Amazon) fiado com credenciais/params.
    adapters = construir_adapters(config)

    # 4. Registra executar_ciclo no agendador na frequencia configuravel (R3.5).
    scheduler = scheduler if scheduler is not None else schedule.Scheduler()
    job = criar_job_ciclo(conn, config, adapters)
    registrar_job(scheduler, job, config.parametros.frequencia_job_segundos)

    return scheduler, conn, config


def main(
    *,
    db_path: str = DEFAULT_DB_PATH,
    intervalo_loop_segundos: float = DEFAULT_INTERVALO_LOOP_SEGUNDOS,
) -> None:
    """Ponto de entrada da aplicacao: startup + loop do agendador.

    Valida a config, inicializa o banco, monta os adapters, registra o job e
    entao **inicia o loop** do agendador (bloqueante). Em producao roda
    indefinidamente ate ser interrompido (ex.: SIGINT).

    Args:
        db_path: caminho do arquivo SQLite (default :data:`DEFAULT_DB_PATH`).
        intervalo_loop_segundos: espera entre verificacoes do agendador.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    scheduler, _conn, config = inicializar_app(db_path=db_path)
    logger.info(
        "Rastreador de Precos iniciado; Job_Monitor a cada %ss. Aguardando o "
        "agendador (Ctrl+C para sair).",
        config.parametros.frequencia_job_segundos,
    )
    loop_agendador(scheduler, intervalo_loop_segundos=intervalo_loop_segundos)


if __name__ == "__main__":
    main()
