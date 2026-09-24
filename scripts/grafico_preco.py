#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gera um grafico HTML interativo do historico de precos e abre no navegador.

Le o historico gravado no banco (o mesmo `rastreador.db` usado por
`python -m src.main`), monta um grafico de linha interativo com Chart.js
(carregado via CDN) e salva um arquivo `grafico.html`, abrindo-o no navegador
padrao. Passar o mouse sobre os pontos mostra o preco e a data/hora.

Uso:
    python scripts/grafico_preco.py                 # 1o produto, ate 200 leituras
    python scripts/grafico_preco.py --id 1          # escolhe o ProdutoSite por id
    python scripts/grafico_preco.py --historico 500 # quantas leituras plotar
    python scripts/grafico_preco.py --saida meu.html# nome do arquivo de saida
    python scripts/grafico_preco.py --nao-abrir     # so gera o arquivo, nao abre

Somente leitura: nao altera o banco. Nao adiciona dependencia Python (o Chart.js
vem de CDN; requer internet para renderizar o grafico).
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from src.db import database

DB_PADRAO = "rastreador.db"
HISTORICO_PADRAO = 200
SAIDA_PADRAO = "grafico.html"

_NOME_SITE = {"mercado_livre": "Mercado Livre", "amazon": "Amazon"}


def _to_float(valor) -> float | None:
    """Converte o preco (TEXT/Decimal no banco) para float apenas para plotar.

    Observacao: usamos float SO na exibicao do grafico. O sistema continua
    guardando e comparando precos como Decimal internamente; aqui a precisao de
    float e mais que suficiente para o eixo do grafico.
    """
    if valor is None or valor == "":
        return None
    try:
        return float(Decimal(str(valor)))
    except (InvalidOperation, ValueError):
        return None


def _hora_local(iso: str | None) -> str:
    """Converte um timestamp ISO (gravado em UTC) para 'dd/mm HH:MM' local."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return str(iso)[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d/%m %H:%M")


def _historico(conn, produto_site_id: int, limite: int):
    """Leituras de preco (mais ANTIGA -> mais nova) com preco valido."""
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
    return list(reversed(rows))


def _montar_html(titulo: str, labels: list[str], precos: list[float],
                 moeda: str, resumo: dict) -> str:
    """Monta o HTML do grafico interativo (Chart.js via CDN)."""
    labels_json = json.dumps(labels, ensure_ascii=False)
    precos_json = json.dumps(precos)
    simbolo = "R$" if moeda == "BRL" else moeda
    return f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background:#0f1115;
          color:#e6e6e6; margin:0; padding:24px; }}
  .card {{ max-width: 960px; margin: 0 auto; background:#171a21;
           border-radius:12px; padding:24px; box-shadow:0 4px 24px rgba(0,0,0,.4); }}
  h1 {{ font-size:18px; font-weight:600; margin:0 0 4px; }}
  .sub {{ color:#9aa4b2; font-size:13px; margin-bottom:20px; }}
  .stats {{ display:flex; gap:24px; margin-bottom:20px; flex-wrap:wrap; }}
  .stat {{ background:#1f2430; border-radius:8px; padding:12px 16px; }}
  .stat .rot {{ color:#9aa4b2; font-size:12px; }}
  .stat .val {{ font-size:20px; font-weight:600; margin-top:2px; }}
  .val.atual {{ color:#4ade80; }}
  .val.max {{ color:#f87171; }}
  .val.min {{ color:#60a5fa; }}
  canvas {{ background:#171a21; }}
  .rodape {{ color:#6b7280; font-size:12px; margin-top:16px; text-align:center; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{titulo}</h1>
    <div class="sub">Historico de preco — horario local</div>
    <div class="stats">
      <div class="stat"><div class="rot">Atual</div>
        <div class="val atual">{simbolo} {resumo['atual']:.2f}</div></div>
      <div class="stat"><div class="rot">Minimo</div>
        <div class="val min">{simbolo} {resumo['minimo']:.2f}</div></div>
      <div class="stat"><div class="rot">Maximo</div>
        <div class="val max">{simbolo} {resumo['maximo']:.2f}</div></div>
      <div class="stat"><div class="rot">Leituras</div>
        <div class="val">{resumo['n']}</div></div>
    </div>
    <canvas id="grafico" height="120"></canvas>
    <div class="rodape">Gerado por scripts/grafico_preco.py — Observer</div>
  </div>
<script>
  const ctx = document.getElementById('grafico');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: {labels_json},
      datasets: [{{
        label: 'Preco ({simbolo})',
        data: {precos_json},
        borderColor: '#4ade80',
        backgroundColor: 'rgba(74,222,128,.12)',
        fill: true,
        tension: 0.25,
        pointRadius: 3,
        pointHoverRadius: 6,
        pointBackgroundColor: '#4ade80'
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{ labels: {{ color: '#e6e6e6' }} }},
        tooltip: {{
          callbacks: {{
            label: (c) => '  {simbolo} ' + c.parsed.y.toFixed(2)
          }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color:'#9aa4b2', maxRotation:60, minRotation:0 }},
              grid: {{ color:'#232838' }} }},
        y: {{ ticks: {{ color:'#9aa4b2',
              callback: (v) => '{simbolo} ' + v }},
              grid: {{ color:'#232838' }} }}
      }}
    }}
  }});
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gera um grafico HTML interativo do historico de precos."
    )
    parser.add_argument("--db", default=DB_PADRAO, help="Caminho do banco SQLite.")
    parser.add_argument("--id", type=int, default=None,
                        help="Id do ProdutoSite (default: o primeiro cadastrado).")
    parser.add_argument("--historico", type=int, default=HISTORICO_PADRAO,
                        help="Quantas leituras plotar (default 200).")
    parser.add_argument("--saida", default=SAIDA_PADRAO,
                        help="Arquivo HTML de saida (default grafico.html).")
    parser.add_argument("--nao-abrir", action="store_true",
                        help="Apenas gera o arquivo, sem abrir no navegador.")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"Banco '{args.db}' nao encontrado.")
        return

    limite = max(2, args.historico)
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
        pares = [(_hora_local(r["coletado_em"]), _to_float(r["preco"]),
                  (r["moeda"] or "BRL")) for r in rows]
        pares = [(lbl, pr, mo) for (lbl, pr, mo) in pares if pr is not None]

        if len(pares) < 2:
            print("Historico insuficiente para um grafico (precisa de >= 2 leituras).")
            print("O grafico fica bom conforme os ciclos forem rodando.")
            return

        labels = [p[0] for p in pares]
        precos = [p[1] for p in pares]
        moeda = pares[-1][2]
        site_legivel = _NOME_SITE.get(alvo.site, alvo.site)
        titulo = f"{site_legivel} — {alvo.item_id}"
        resumo = {
            "atual": precos[-1],
            "minimo": min(precos),
            "maximo": max(precos),
            "n": len(precos),
        }

        html = _montar_html(titulo, labels, precos, moeda, resumo)
        saida = Path(args.saida).resolve()
        saida.write_text(html, encoding="utf-8")

        print(f"Grafico gerado: {saida}")
        print(f"  {resumo['n']} leituras | atual {moeda} {resumo['atual']:.2f}"
              f" | min {resumo['minimo']:.2f} | max {resumo['maximo']:.2f}")
        if not args.nao_abrir:
            webbrowser.open(saida.as_uri())
            print("Abrindo no navegador...")
        else:
            print("(--nao-abrir: arquivo gerado, nao aberto)")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(1)
