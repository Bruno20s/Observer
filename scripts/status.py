#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verifica se o monitoramento (o processo `python -m src.main`) esta rodando.

Uso:
    python scripts/status.py

Procura por um processo Python cuja linha de comando contenha "src.main"
(o entrypoint do monitor). Mostra tambem um resumo do banco: quantos produtos
estao cadastrados e quando foi a ultima coleta de preco registrada — util para
confirmar nao so que o processo esta vivo, mas que ele ja trabalhou.

Detecta o processo com `psutil` quando disponivel (multiplataforma) e, se nao,
cai para os comandos nativos do sistema (Windows/Linux). Somente leitura.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

#: Substring que identifica o processo do monitor na linha de comando.
_MARCADOR = "src.main"
DB_PADRAO = "rastreador.db"


def _processos_via_psutil() -> list[tuple[int, str]]:
    """Lista (pid, inicio) dos processos do monitor usando psutil."""
    import psutil  # import local: fallback trata a ausencia
    from datetime import datetime

    encontrados: list[tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # Ignora este proprio processo de status.
        if _MARCADOR in cmdline and "status.py" not in cmdline:
            inicio = datetime.fromtimestamp(
                proc.info.get("create_time", 0)
            ).strftime("%Y-%m-%d %H:%M:%S")
            encontrados.append((proc.info["pid"], inicio))
    return encontrados


def _processos_via_so() -> list[tuple[int, str]]:
    """Fallback sem psutil: usa comando nativo do SO (Windows/Linux)."""
    encontrados: list[tuple[int, str]] = []
    try:
        if os.name == "nt":
            # PowerShell: lista PID + CommandLine dos processos python.
            saida = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter "
                    "\"Name='python.exe' OR Name='pythonw.exe'\" | "
                    "Select-Object ProcessId,CommandLine | Format-Table -HideTableHeaders -AutoSize",
                ],
                capture_output=True, text=True, timeout=20,
            ).stdout
        else:
            saida = subprocess.run(
                ["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=20
            ).stdout
    except Exception:
        return encontrados

    for linha in saida.splitlines():
        if _MARCADOR in linha and "status.py" not in linha:
            partes = linha.split(None, 1)
            if partes and partes[0].isdigit():
                encontrados.append((int(partes[0]), "?"))
    return encontrados


def _processos_do_monitor() -> list[tuple[int, str]]:
    try:
        import psutil  # noqa: F401
    except ImportError:
        return _processos_via_so()
    return _processos_via_psutil()


def _resumo_banco(db_path: str) -> None:
    """Mostra qtde de produtos e a ultima coleta registrada (se o banco existir)."""
    if not Path(db_path).exists():
        print(f"  banco: '{db_path}' ainda nao existe (nenhuma coleta feita).")
        return
    try:
        from src.db import database

        conn = database.inicializar_banco(db_path)
        try:
            produtos = database.listar_produto_site(conn)
            ultima = conn.execute(
                "SELECT MAX(coletado_em) FROM historico_preco"
            ).fetchone()[0]
        finally:
            conn.close()
        print(f"  produtos cadastrados: {len(produtos)}")
        if ultima:
            print(f"  ultima coleta de preco: {str(ultima).replace('T', ' ')[:19]} (UTC)")
        else:
            print("  ultima coleta de preco: (nenhuma ainda)")
    except Exception as exc:  # nao e fatal para o status
        print(f"  (nao foi possivel ler o banco: {exc})")


def main() -> None:
    processos = _processos_do_monitor()

    print("=== Status do monitoramento (Observer) ===")
    if processos:
        print("Estado: RODANDO")
        for pid, inicio in processos:
            if inicio and inicio != "?":
                print(f"  PID {pid}  (iniciado em {inicio})")
            else:
                print(f"  PID {pid}")
    else:
        print("Estado: PARADO")
        print("  Para iniciar:  python -m src.main")

    print("")
    print("Banco de dados:")
    _resumo_banco(DB_PADRAO)


if __name__ == "__main__":
    main()
