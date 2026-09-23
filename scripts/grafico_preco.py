#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grafico ASCII do historico de precos de um produto monitorado.

Desenha, direto no terminal (sem dependencias externas), a evolucao do preco
de um ProdutoSite ao longo do tempo, a partir do historico gravado no banco
(o mesmo `rastreador.db` usado por `python -m src.main`).

Uso:
    python scripts/grafico_preco.py                 # 1o produto, ultimas 30 leituras
    python scripts/grafico_preco.py --id 1          # escolhe o ProdutoSite por id
    python scripts/grafico_preco.py --historico 60  # quantas leituras plotar
    python scripts/grafico_preco.py --altura 15     # altura do grafico (linhas)

Somente leitura: nao altera o banco.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from src.db import database

DB_PADRAO = "rastreador.db"
HISTORICO_PADRAO = 30
ALTURA_PADRAO = 12
LARGURA_MAX = 70  # colunas maximas do desenho (alem do rotulo do eixo Y)


def _to_decimal(valor) -> Decimal | None:
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return None


def _hora_local(iso: str | None) -> str:
    """Converte um timestamp ISO (gravado em UTC) para HH:MM no fuso local."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return str(iso)[11:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d/%m %H:%M")


def _historico(conn, produto_site_id: int, limite: int):
    """Leituras de preco (mais ANTIGA -> mais nova), com preco valido."""
    rows = conn.execute(
        """
        SELECT preco, moeda, coletado_em
        FROM historico_preco
        WHERE produto_site_id = ? AND preco IS NOT NULL
        ORDER BY coletado_em DESC, id DESC
        LIMIT ?
        """,
        (produto_site_id, limite),
    ).fetchall()
    return list(reversed(rows))  # cronologico: antigo -> novo


def _desenhar_grafico(precos: list[Decimal], altura: int) -> list[str]:
    """Monta as linhas de um grafico ASCII de linha para a serie `precos`."""
    minimo = min(precos)
    maximo = max(precos)
    faixa = maximo - minimo

    # Mapeia cada preco para uma "linha" (0 = base, altura-1 = topo).
    def nivel(preco: Decimal) -> int:
        if faixa == 0:
            return altura // 2  # serie plana: meio do grafico
        pos = (preco - minimo) / faixa  # 0..1
        return int(round(float(pos) * (altura - 1)))

    niveis = [nivel(p) for p in precos]
    largura = len(precos)

    # Matriz de caracteres (altura x largura), preenchida de espaco.
    grade = [[" "] * largura for _ in range(altura)]
    for x, n in enumerate(niveis):
        grade[n][x] = "*"  # ponto da leitura
    # Liga os pontos verticalmente entre leituras consecutivas (visual de linha).
    for x in range(1, largura):
        a, b = niveis[x - 1], niveis[x]
        if a != b:
            passo = 1 if b > a else -1
            for y in range(a + passo, b, passo):
                if grade[y][x] == " ":
                    grade[y][x] = "|"

    # Rotulos do eixo Y: topo = maximo, base = minimo.
    linhas: list[str] = []
    for linha_idx in range(altura - 1, -1, -1):  # topo primeiro
        if linha_idx == altura - 1:
            rotulo = f"R$ {maximo:.2f}"
        elif linha_idx == 0:
            rotulo = f"R$ {minimo:.2f}"
        else:
            rotulo = ""
        eixo = f"{rotulo:>11} |"
        linhas.append(eixo + "".join(grade[linha_idx]))
    # Eixo X (base).
    linhas.append(" " * 11 + " +" + "-" * largura)
    return linhas


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grafico ASCII do historico de precos de um produto."
    )
    parser.add_argument("--db", default=DB_PADRAO, help="Caminho do banco SQLite.")
    parser.add_argument("--id", type=int, default=None,
                        help="Id do ProdutoSite (default: o primeiro cadastrado).")
    parser.add_argument("--historico", type=int, default=HISTORICO_PADRAO,
                        help="Quantas leituras plotar (default 30).")
    parser.add_argument("--altura", type=int, default=ALTURA_PADRAO,
                        help="Altura do grafico em linhas (default 12).")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"Banco '{args.db}' nao encontrado.")
        return

    altura = max(5, min(args.altura, 40))
    limite = max(2, min(args.historico, LARGURA_MAX))

    conn = database.inicializar_banco(args.db)
    try:
        produtos = database.listar_produto_site(conn)
        if not produtos:
            print("(nenhum produto cadastrado)")
            return

        if args.id is not None:
            alvo = next((p for p in produtos if p.id == args.id), None)
            if alvo is None:
                print(f"ProdutoSite #{args.id} nao encontrado.")
                print("Ids disponiveis: " + ", ".join(str(p.id) for p in produtos))
                return
        else:
            alvo = produtos[0]

        rows = _historico(conn, alvo.id, limite)
        precos = [_to_decimal(r["preco"]) for r in rows]
        precos = [p for p in precos if p is not None]

        print(f"=== Grafico de preco — ProdutoSite #{alvo.id} ({alvo.site} {alvo.item_id}) ===")
        if len(precos) < 2:
            print("Historico insuficiente para um grafico (precisa de >= 2 leituras).")
            n = len(precos)
            if n == 1:
                print(f"Unica leitura ate agora: R$ {precos[0]:.2f}")
            print("O grafico aparece conforme os ciclos forem rodando.")
            return

        for linha in _desenhar_grafico(precos, altura):
            print(linha)

        # Legenda do eixo X: primeira e ultima data/hora (local).
        primeira = _hora_local(rows[0]["coletado_em"])
        ultima = _hora_local(rows[-1]["coletado_em"])
        print(" " * 13 + f"{primeira}  ...  {ultima}  (horario local)")

        # Resumo abaixo do grafico.
        atual = precos[-1]
        minimo = min(precos)
        maximo = max(precos)
        print("")
        print(f"leituras plotadas: {len(precos)}")
        print(f"atual: R$ {atual:.2f}   |   minimo: R$ {minimo:.2f}   |   maximo: R$ {maximo:.2f}")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(1)
