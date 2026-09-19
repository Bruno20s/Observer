"""Testes (exemplo, mockados) do agendamento do Job_Monitor (`src/main.py`, task 12.2).

Estes sao testes de EXEMPLO (nao property-based): verificam o **registro** do
job no agendador na frequencia configurada (default 4h = 14400s) SEM sleep real
e SEM entrar no loop infinito. Todas as pecas testaveis (``registrar_job``,
``criar_job_ciclo``, ``loop_agendador``) sao exercitadas em isolamento; nada de
rede nem tempo real e disparado.

Cobrem:

1. ``registrar_job`` registra exatamente UM job no intervalo/unidade esperados.
2. O job agendado de fato invoca ``executar_ciclo`` quando dispara -- via
   ``loop_agendador`` com ``sleep`` no-op e ``continuar`` limitado, forcando o
   job a ficar "due".
3. A guarda de topo (``criar_job_ciclo``) isola excecao do ciclo: nunca propaga.
4. ``registrar_job`` levanta ``ValueError`` para ``frequencia_segundos <= 0``.

**Validates: Requirements 3.5**
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import schedule

from src import main
from src.config import DEFAULT_FREQUENCIA_JOB_SEGUNDOS


# ===========================================================================
# 1. registrar_job: exatamente um job no intervalo/unidade esperados (R3.5)
# ===========================================================================
def test_registrar_job_registra_um_job_na_frequencia_esperada() -> None:
    """registra exatamente UM job com interval == frequencia e unit == 'seconds'.

    Usa o default de 4h (14400s) como frequencia -- a fonte de verdade do R3.5.

    **Validates: Requirements 3.5**
    """
    scheduler = schedule.Scheduler()

    def _job() -> None:
        return None

    job = main.registrar_job(scheduler, _job, DEFAULT_FREQUENCIA_JOB_SEGUNDOS)

    # Default de 4h em segundos (sanity check da frequencia esperada).
    assert DEFAULT_FREQUENCIA_JOB_SEGUNDOS == 14400

    # Exatamente um job, no intervalo/unidade corretos.
    assert len(scheduler.jobs) == 1
    assert scheduler.jobs[0] is job
    assert job.interval == DEFAULT_FREQUENCIA_JOB_SEGUNDOS
    assert job.unit == "seconds"


# ===========================================================================
# 2. O job agendado invoca executar_ciclo quando dispara (sem sleep/loop real)
# ===========================================================================
def test_loop_agendador_dispara_job_due_sem_sleep_real() -> None:
    """loop_agendador roda run_pending e o job "due" chama o run-cycle stubado.

    O ``sleep`` e um no-op e ``continuar`` para apos uma iteracao (sem loop
    infinito). O job e forcado a ficar "due" retroagindo ``next_run``.

    **Validates: Requirements 3.5**
    """
    scheduler = schedule.Scheduler()
    chamadas = {"n": 0}

    def _run_cycle() -> None:
        chamadas["n"] += 1

    job = main.registrar_job(scheduler, _run_cycle, DEFAULT_FREQUENCIA_JOB_SEGUNDOS)

    # Forca o job a ficar "due" (agendado no passado) para run_pending dispara-lo.
    job.next_run = datetime.now() - timedelta(seconds=1)

    # continuar retorna True uma vez, depois False -> exatamente uma iteracao.
    iteracoes = {"n": 0}

    def _continuar() -> bool:
        iteracoes["n"] += 1
        return iteracoes["n"] <= 1

    sleeps: list[float] = []

    main.loop_agendador(
        scheduler,
        sleep=lambda segundos: sleeps.append(segundos),
        continuar=_continuar,
    )

    # O job disparou exatamente uma vez; nunca dormiu de verdade.
    assert chamadas["n"] == 1
    assert sleeps == [main.DEFAULT_INTERVALO_LOOP_SEGUNDOS]


def test_loop_agendador_nao_dispara_job_nao_due() -> None:
    """Sem forcar "due", o job futuro NAO dispara numa iteracao curta."""
    scheduler = schedule.Scheduler()
    chamadas = {"n": 0}

    job = main.registrar_job(
        scheduler, lambda: chamadas.__setitem__("n", chamadas["n"] + 1), 3600
    )
    # next_run fica no futuro (default do schedule) -> nao esta due.
    assert job.next_run > datetime.now()

    iteracoes = {"n": 0}

    def _continuar() -> bool:
        iteracoes["n"] += 1
        return iteracoes["n"] <= 2

    main.loop_agendador(scheduler, sleep=lambda _s: None, continuar=_continuar)

    assert chamadas["n"] == 0


# ===========================================================================
# 3. criar_job_ciclo isola excecao do ciclo (guarda de topo)
# ===========================================================================
def test_criar_job_ciclo_isola_excecao_do_ciclo(monkeypatch) -> None:
    """O closure de criar_job_ciclo NAO propaga excecoes de executar_ciclo.

    **Validates: Requirements 3.5**
    """
    chamadas = {"n": 0}

    def _executar_ciclo_que_falha(conn, *, config, adapters):
        chamadas["n"] += 1
        raise RuntimeError("falha simulada dentro do ciclo")

    monkeypatch.setattr(main, "executar_ciclo", _executar_ciclo_que_falha)

    # conn/config/adapters sao apenas repassados ao stub; podem ser sentinelas.
    job = main.criar_job_ciclo(conn=object(), config=object(), adapters={})

    # A guarda de topo captura a excecao: chamar o job NUNCA propaga.
    job()  # nao deve levantar

    assert chamadas["n"] == 1


def test_criar_job_ciclo_repassa_conn_config_adapters(monkeypatch) -> None:
    """O closure chama executar_ciclo com conn/config/adapters capturados."""
    recebidos = {}

    def _executar_ciclo(conn, *, config, adapters):
        recebidos["conn"] = conn
        recebidos["config"] = config
        recebidos["adapters"] = adapters

    monkeypatch.setattr(main, "executar_ciclo", _executar_ciclo)

    conn_sentinela = object()
    config_sentinela = object()
    adapters_sentinela = {"x": 1}

    job = main.criar_job_ciclo(
        conn=conn_sentinela, config=config_sentinela, adapters=adapters_sentinela
    )
    job()

    assert recebidos["conn"] is conn_sentinela
    assert recebidos["config"] is config_sentinela
    assert recebidos["adapters"] is adapters_sentinela


# ===========================================================================
# 4. registrar_job valida frequencia_segundos > 0
# ===========================================================================
@pytest.mark.parametrize("frequencia_invalida", [0, -1, -3600])
def test_registrar_job_rejeita_frequencia_nao_positiva(frequencia_invalida) -> None:
    """registrar_job levanta ValueError para frequencia_segundos <= 0.

    **Validates: Requirements 3.5**
    """
    scheduler = schedule.Scheduler()

    with pytest.raises(ValueError):
        main.registrar_job(scheduler, lambda: None, frequencia_invalida)

    # Nenhum job foi registrado quando a frequencia e invalida.
    assert len(scheduler.jobs) == 0
