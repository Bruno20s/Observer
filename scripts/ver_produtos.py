#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resumo, no terminal, dos produtos monitorados e seu historico de precos.

Le o banco (o mesmo usado por `python -m src.main`, `rastreador.db`) e imprime,
para cada ProdutoSite cadastrado:
  - nome do produto (obtido do ultimo registro de preco, se houver) + site + id;
  - o ultimo preco coletado e quando;
  - a reputacao mais recente do vendedor (nivel/selo/score), se houver;
  - um mini-historico das ultimas leituras de preco, com a variacao entre elas.

Somente leitura: nao altera o banco. Uso:

    python scripts/ver_produtos.py
    python scripts/ver_produtos.py --db caminho.db      # outro banco
    python scripts/ver_produtos.py --historico 20        # N leituras por produto
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
HISTORICO_PADRAO = 10

# Rotulos legiveis por site.
_NOME_SITE = {"mercado_livre": "Mercado Livre", "amazon": "Amazon"}


def _fmt_preco(valor: str | None, moeda: str = "BRL") -> str:
    """Formata um preco (texto do banco) como 'R$ 1053.00'. '-' se ausente."""
    if valor is None or valor == "":
        return "-"
    try:
        dec = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return str(valor)
    simbolo = "R$" if moeda == "BRL" else (moeda or "")
    return f"{simbolo} {dec:.2f}".strip()


def _rotulo_fuso() -> str:
    """Rotulo curto do fuso local, no formato 'horario local UTC-03'.

    Deixa explicito que os horarios exibidos foram convertidos de UTC (como o
    banco grava) para o fuso local de quem executa o script. Usa o offset
    numerico (ex.: -0300 -> UTC-03) por ser curto e inequivoco, evitando o
    nome longo do fuso que alguns sistemas retornam.
    """
    offset = datetime.now().astimezone().strftime("%z")  # ex.: '-0300'
    if not offset:
        return "horario local"
    return f"horario local UTC{offset[:3]}"  # ex.: 'horario local UTC-03'


def _fmt_data(iso: str | None) -> str:
    """Formata um timestamp ISO (gravado em UTC) no HORARIO LOCAL da maquina.

    O banco grava timestamps em UTC (ISO-8601). Aqui convertemos para o fuso
    local de quem executa o script, exibindo 'AAAA-MM-DD HH:MM'. Timestamps
    sem informacao de fuso sao assumidos como UTC (convencao do sistema).
    """
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        # Formato inesperado: devolve o texto cru (truncado), sem quebrar.
        return str(iso).replace("T", " ")[:16]
    if dt.tzinfo is None:
        # Sem fuso -> assume UTC (o sistema sempre grava em UTC).
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()  # converte para o fuso local da maquina
    return local.strftime("%Y-%m-%d %H:%M")


def _variacao(anterior: str | None, novo: str | None) -> str:
    """Retorna a variacao percentual entre dois precos (texto), ou '' se n/a."""
    if not anterior or not novo:
        return ""
    try:
        a = Decimal(str(anterior))
        n = Decimal(str(novo))
    except (InvalidOperation, ValueError):
        return ""
    if a == 0:
        return ""
    pct = (n - a) / a * Decimal(100)
    seta = "^" if pct > 0 else ("v" if pct < 0 else "=")
    sinal = "+" if pct > 0 else ""
    return f"  ({seta} {sinal}{pct:.2f}%)"


def _historico_preco(conn, produto_site_id: int, limite: int):
    """Ultimas `limite` leituras de preco (mais recentes primeiro)."""
    return conn.execute(
        """
        SELECT preco, moeda, coletado_em, mudanca_detectada
        FROM historico_preco
        WHERE produto_site_id = ?
        ORDER BY coletado_em DESC, id DESC
        LIMIT ?
        """,
        (produto_site_id, limite),
    ).fetchall()


def _ultima_reputacao(conn, produto_site_id: int):
    row = conn.execute(
        """
        SELECT ml_level_id, ml_selo, ml_percentual_reclamacoes,
               amz_nota_media, amz_num_avaliacoes, amz_vendido_por_amazon,
               score_confianca, coletado_em
        FROM historico_reputacao
        WHERE produto_site_id = ?
        ORDER BY coletado_em DESC, id DESC
        LIMIT 1
        """,
        (produto_site_id,),
    ).fetchone()
    return row


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resumo dos produtos monitorados e historico de precos."
    )
    parser.add_argument("--db", default=DB_PADRAO, help="Caminho do banco SQLite.")
    parser.add_argument(
        "--historico", type=int, default=HISTORICO_PADRAO,
        help="Quantas leituras de preco mostrar por produto (default 10).",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"Banco '{args.db}' nao encontrado. Rode o sistema/cadastro primeiro.")
        return

    conn = database.inicializar_banco(args.db)
    try:
        produtos = database.listar_produto_site(conn)
        if not produtos:
            print("(nenhum produto cadastrado)")
            print("Cadastre com: python scripts/cadastrar_produto.py \"<url>\"")
            return

        print(f"=== {len(produtos)} produto(s) monitorado(s) ===\n")

        for ps in produtos:
            historico = _historico_preco(conn, ps.id, args.historico)
            # Nome: nao ha coluna de nome no ProdutoSite; usamos o item_id como
            # rotulo tecnico e a URL como referencia (o nome vem so na notificacao).
            site_legivel = _NOME_SITE.get(ps.site, ps.site)
            grupo = f"  [grupo produto_id={ps.produto_id}]" if ps.produto_id is not None else ""
            print(f"#{ps.id}  {site_legivel}  ({ps.item_id}){grupo}")
            print(f"   URL: {ps.url_original}")

            if not historico:
                print("   preco: (ainda nao coletado)\n")
                continue

            # historico[0] e a leitura mais recente.
            atual = historico[0]
            print(f"   ultimo preco: {_fmt_preco(atual['preco'], atual['moeda'])}"
                  f"   em {_fmt_data(atual['coletado_em'])} ({_rotulo_fuso()})")

            rep = _ultima_reputacao(conn, ps.id)
            if rep is not None:
                partes = []
                if rep["ml_level_id"]:
                    partes.append(f"nivel {rep['ml_level_id']}")
                if rep["ml_selo"] and rep["ml_selo"] != "ausente":
                    partes.append(f"selo {rep['ml_selo']}")
                if rep["amz_nota_media"] is not None:
                    partes.append(f"nota {rep['amz_nota_media']}/5")
                if rep["amz_num_avaliacoes"] is not None:
                    partes.append(f"{rep['amz_num_avaliacoes']} avaliacoes")
                score = rep["score_confianca"]
                partes.append(f"score {score}")
                print("   reputacao: " + ", ".join(partes))

            # Mini-historico: da leitura mais antiga (na janela) para a mais nova,
            # mostrando a variacao entre leituras consecutivas.
            if len(historico) > 1:
                print(f"   historico (ultimas {len(historico)} leituras):")
                ordenado = list(reversed(historico))  # mais antiga -> mais nova
                anterior = None
                for h in ordenado:
                    var = _variacao(anterior, h["preco"]) if anterior else ""
                    marca = " *" if h["mudanca_detectada"] else ""
                    print(f"     {_fmt_data(h['coletado_em'])}  "
                          f"{_fmt_preco(h['preco'], h['moeda'])}{var}{marca}")
                    anterior = h["preco"]
                print("     (* = mudanca que gerou notificacao)")
            print()
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(1)
