#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Define (ou limpa) o e-mail de destino das notificacoes de um produto.

Cada ProdutoSite pode notificar um e-mail proprio (coluna email_destino). Este
utilitario permite ajustar isso para produtos JA cadastrados, sem recadastrar.
Quando o e-mail e limpo (--limpar), o produto volta a usar o EMAIL_DESTINATARIO
padrao do .env.

Uso:
    python scripts/definir_email.py --id 1 --email destino@x.com
    python scripts/definir_email.py --id 3 --limpar     # volta ao padrao do .env
    python scripts/definir_email.py --listar            # mostra os destinos atuais

Somente altera a coluna email_destino; nao toca em nenhum historico.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from src.db import database

DB_PADRAO = "rastreador.db"


def _validar_email(email: str) -> bool:
    partes = email.split("@")
    return len(partes) == 2 and bool(partes[0]) and bool(partes[1])


def _listar(conn) -> None:
    entradas = database.listar_produto_site(conn)
    if not entradas:
        print("(nenhum ProdutoSite cadastrado)")
        return
    print(f"{len(entradas)} ProdutoSite cadastrado(s):")
    for ps in entradas:
        destino = ps.email_destino or "(padrao do .env)"
        print(f"  #{ps.id}  {ps.site}  {ps.item_id}  ->  {destino}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Define/limpa o e-mail de destino de notificacao de um produto."
    )
    parser.add_argument("--db", default=DB_PADRAO, help="Caminho do banco SQLite.")
    parser.add_argument("--id", type=int, default=None,
                        help="Id do ProdutoSite a ajustar.")
    parser.add_argument("--email", default=None,
                        help="E-mail de destino a definir (formato usuario@dominio).")
    parser.add_argument("--limpar", action="store_true",
                        help="Limpa o e-mail (volta a usar o EMAIL_DESTINATARIO do .env).")
    parser.add_argument("--listar", action="store_true",
                        help="Apenas lista os destinos atuais e sai.")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"Banco '{args.db}' nao encontrado.")
        return

    conn = database.inicializar_banco(args.db)
    try:
        if args.listar:
            _listar(conn)
            return

        if args.id is None:
            print("Informe --id do produto (ou use --listar para ver os ids).")
            _listar(conn)
            return

        # Confirma que o produto existe.
        existe = any(ps.id == args.id for ps in database.listar_produto_site(conn))
        if not existe:
            print(f"ProdutoSite #{args.id} nao encontrado.")
            _listar(conn)
            return

        if args.limpar:
            database.definir_email_destino(conn, args.id, None)
            print(f"#{args.id}: e-mail de destino limpo (voltou ao padrao do .env).")
        elif args.email:
            if not _validar_email(args.email):
                print(f"E-mail invalido: {args.email!r} (esperado usuario@dominio).")
                return
            database.definir_email_destino(conn, args.id, args.email.strip())
            print(f"#{args.id}: notificacoes agora vao para {args.email.strip()}")
        else:
            print("Informe --email <endereco> ou --limpar.")
            return

        print("")
        _listar(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(1)
