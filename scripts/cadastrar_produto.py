#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ferramenta de setup: cadastrar produtos para monitoramento.

Popula o banco (mesmo arquivo usado por `python -m src.main`,
`DEFAULT_DB_PATH = "rastreador.db"`) com entradas ProdutoSite a partir de URLs
de anuncios do Mercado Livre ou da Amazon. Delega ao fluxo oficial de cadastro
(`src.cadastro.cadastrar_produto`), que valida a URL, roteia para o adapter
correto e deduplica por (site, item_id) — nenhuma logica de cadastro nova vive
aqui.

Uso:

  # Cadastrar uma ou mais URLs passadas como argumento:
  python scripts/cadastrar_produto.py "<url1>" "<url2>" ...

  # Sem argumentos: entra em modo interativo (cola uma URL por linha,
  # linha vazia encerra):
  python scripts/cadastrar_produto.py

  # Apenas listar o que ja esta cadastrado:
  python scripts/cadastrar_produto.py --listar

Opcional: --db <caminho> para usar um banco diferente do padrao.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Garante que a raiz do projeto esteja no sys.path quando rodado como script.
RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from src.cadastro import StatusCadastro, cadastrar_produto
from src.db import database

# Mesmo arquivo de banco usado pelo entrypoint (src/main.py -> DEFAULT_DB_PATH).
DB_PADRAO = "rastreador.db"


def _listar(conn) -> None:
    entradas = database.listar_produto_site(conn)
    if not entradas:
        print("(nenhum ProdutoSite cadastrado ainda)")
        return
    print(f"{len(entradas)} ProdutoSite cadastrado(s):")
    for ps in entradas:
        grupo = f" [produto_id={ps.produto_id}]" if ps.produto_id is not None else ""
        print(f"  #{ps.id}  {ps.site}  {ps.item_id}{grupo}")
        print(f"        {ps.url_original}")


def _cadastrar_uma(conn, url: str) -> bool:
    """Cadastra uma URL. Retorna True se persistiu uma nova entrada."""
    resultado = cadastrar_produto(conn, url)
    if resultado.status is StatusCadastro.SUCESSO:
        ps = resultado.produto_site
        print(f"[OK] cadastrado: {ps.site} / {ps.item_id} (#{ps.id})")
        return True
    if resultado.status is StatusCadastro.DUPLICADO:
        print(f"[JA EXISTE] {resultado.mensagem}")
        return False
    # URL_INVALIDA ou qualquer outra rejeicao.
    print(f"[REJEITADO] {resultado.mensagem}")
    print(f"            URL: {url}")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cadastra produtos (URLs de ML/Amazon) para monitoramento."
    )
    parser.add_argument("urls", nargs="*", help="URLs de anuncios a cadastrar.")
    parser.add_argument("--db", default=DB_PADRAO, help="Caminho do banco SQLite.")
    parser.add_argument(
        "--listar", action="store_true",
        help="Apenas listar os ProdutoSite ja cadastrados e sair.",
    )
    args = parser.parse_args()

    conn = database.inicializar_banco(args.db)
    try:
        if args.listar:
            _listar(conn)
            return

        urls = list(args.urls)
        if not urls:
            print("Modo interativo: cole uma URL por linha; linha vazia encerra.\n")
            while True:
                try:
                    linha = input("URL> ").strip()
                except EOFError:
                    break
                if not linha:
                    break
                urls.append(linha)

        if not urls:
            print("Nenhuma URL informada. Nada a cadastrar.")
            print("Dica: rode com --listar para ver o que ja esta cadastrado.")
            return

        novos = 0
        for url in urls:
            if _cadastrar_uma(conn, url):
                novos += 1

        print(f"\nConcluido: {novos} novo(s) cadastrado(s) de {len(urls)} URL(s).\n")
        _listar(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(1)
