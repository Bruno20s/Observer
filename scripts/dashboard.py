#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dashboard web LOCAL (somente leitura) do Observer.

Sobe um servidor HTTP em 127.0.0.1 (apenas a propria maquina; nao exposto a
rede) que serve uma pagina unica com status do monitoramento, lista de produtos
e um grafico de preco interativo (Chart.js via CDN) com filtros de produto e
periodo. E SOMENTE LEITURA: os endpoints so fazem SELECT no banco e checam
processos; nenhuma acao altera o sistema. Escuta apenas em 127.0.0.1.

Uso:
    python scripts/dashboard.py            # http://localhost:8765
    python scripts/dashboard.py --porta 9000
    python scripts/dashboard.py --nao-abrir

Requer internet para carregar o Chart.js (CDN). Nenhuma dependencia Python nova.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from src.db import database

DB_PADRAO = "rastreador.db"
PORTA_PADRAO = 8765
HOST = "127.0.0.1"  # apenas localhost; nunca 0.0.0.0
_MARCADOR = "src.main"
_NOME_SITE = {"mercado_livre": "Mercado Livre", "amazon": "Amazon"}

_DB_ABS = str(RAIZ_PROJETO / DB_PADRAO)


def _to_float(valor):
    if valor is None or valor == "":
        return None
    try:
        return float(Decimal(str(valor)))
    except (InvalidOperation, ValueError):
        return None


def _iso_local(iso):
    """Converte timestamp ISO (UTC no banco) para 'dd/mm HH:MM' local."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso))
    except (ValueError, TypeError):
        return str(iso)[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d/%m %H:%M")


def _processos_monitor():
    """Lista (pid, inicio_local) dos processos do monitor. Usa psutil se houver."""
    try:
        import psutil
    except ImportError:
        return []
    achados = []
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if _MARCADOR in cmd and "dashboard.py" not in cmd and "status.py" not in cmd:
            inicio = datetime.fromtimestamp(
                proc.info.get("create_time", 0)
            ).strftime("%d/%m %H:%M:%S")
            achados.append((proc.info["pid"], inicio))
    return achados


def api_status():
    procs = _processos_monitor()
    conn = database.inicializar_banco(_DB_ABS)
    try:
        n_prod = len(database.listar_produto_site(conn))
        ultima = conn.execute(
            "SELECT MAX(coletado_em) FROM historico_preco"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "rodando": bool(procs),
        "processos": [{"pid": p, "inicio": i} for p, i in procs],
        "produtos": n_prod,
        "ultima_coleta": _iso_local(ultima),
    }


def api_produtos():
    conn = database.inicializar_banco(_DB_ABS)
    try:
        out = []
        for ps in database.listar_produto_site(conn):
            ult = database.obter_ultimo_preco(conn, ps.id)
            rep = conn.execute(
                "SELECT ml_level_id, ml_selo, score_confianca, amz_nota_media, "
                "amz_num_avaliacoes FROM historico_reputacao "
                "WHERE produto_site_id=? ORDER BY id DESC LIMIT 1",
                (ps.id,),
            ).fetchone()
            out.append({
                "id": ps.id,
                "site": _NOME_SITE.get(ps.site, ps.site),
                "item_id": ps.item_id,
                "url": ps.url_original,
                "preco": _to_float(ult.preco) if ult else None,
                "moeda": (ult.moeda if ult else "BRL"),
                "coletado_em": _iso_local(ult.coletado_em) if ult else None,
                "reputacao": (dict(rep) if rep else None),
            })
        return out
    finally:
        conn.close()


def api_historico(produto_site_id, dias):
    conn = database.inicializar_banco(_DB_ABS)
    try:
        if dias and dias > 0:
            rows = conn.execute(
                "SELECT preco, moeda, coletado_em FROM historico_preco "
                "WHERE produto_site_id=? AND preco IS NOT NULL "
                "AND coletado_em >= datetime('now', ?) "
                "ORDER BY coletado_em ASC, id ASC",
                (produto_site_id, f"-{int(dias)} days"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT preco, moeda, coletado_em FROM historico_preco "
                "WHERE produto_site_id=? AND preco IS NOT NULL "
                "ORDER BY coletado_em ASC, id ASC",
                (produto_site_id,),
            ).fetchall()
        return {
            "labels": [_iso_local(r["coletado_em"]) for r in rows],
            "precos": [_to_float(r["preco"]) for r in rows],
        }
    finally:
        conn.close()


PAGINA = r'''<!DOCTYPE html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Observer - Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 body{font-family:'Segoe UI',Arial,sans-serif;background:#0f1115;color:#e6e6e6;margin:0;padding:24px}
 .wrap{max-width:1000px;margin:0 auto}
 h1{font-size:20px;margin:0 0 16px}
 .card{background:#171a21;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 4px 20px rgba(0,0,0,.35)}
 .row{display:flex;gap:16px;flex-wrap:wrap;align-items:center}
 .badge{display:inline-flex;align-items:center;gap:8px;font-weight:600}
 .dot{width:10px;height:10px;border-radius:50%}
 .on{background:#4ade80}.off{background:#f87171}
 .muted{color:#9aa4b2;font-size:13px}
 button,select{background:#1f2430;color:#e6e6e6;border:1px solid #2b313d;border-radius:8px;padding:8px 12px;font-size:14px;cursor:pointer}
 button:hover{background:#252b39}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #232838}
 th{color:#9aa4b2;font-weight:600}
 a{color:#60a5fa;text-decoration:none}
 .preco{font-weight:600;color:#4ade80}
</style></head>
<body><div class="wrap">
 <h1>Observer - Dashboard</h1>
 <div class="card">
  <div class="row" style="justify-content:space-between">
   <div id="status" class="badge"><span class="dot off"></span> carregando...</div>
   <button onclick="carregarTudo()">Atualizar</button>
  </div>
  <div id="statusInfo" class="muted" style="margin-top:8px"></div>
 </div>
 <div class="card">
  <h3 style="margin:0 0 12px">Produtos monitorados</h3>
  <table id="tabela"><thead><tr>
   <th>#</th><th>Site</th><th>Item</th><th>Ultimo preco</th><th>Coletado</th><th>Reputacao</th>
  </tr></thead><tbody></tbody></table>
 </div>
 <div class="card">
  <div class="row" style="justify-content:space-between;margin-bottom:12px">
   <h3 style="margin:0">Grafico de preco</h3>
   <div class="row">
    <select id="selProduto" onchange="carregarGrafico()"></select>
    <select id="selPeriodo" onchange="carregarGrafico()">
     <option value="7">Ultimos 7 dias</option>
     <option value="30" selected>Ultimos 30 dias</option>
     <option value="90">Ultimos 90 dias</option>
     <option value="0">Tudo</option>
    </select>
   </div>
  </div>
  <canvas id="grafico" height="110"></canvas>
 </div>
 <div class="muted" style="text-align:center">Observer - dashboard local (somente leitura)</div>
</div>
<script>
let chart=null;
async function jget(u){const r=await fetch(u);return await r.json();}
async function carregarStatus(){
 const s=await jget('/api/status');
 const el=document.getElementById('status');
 const dot=s.rodando?'<span class="dot on"></span>':'<span class="dot off"></span>';
 el.innerHTML=dot+(s.rodando?' Monitoramento RODANDO':' Monitoramento PARADO');
 let info=s.produtos+' produto(s)';
 if(s.ultima_coleta) info+=' - ultima coleta: '+s.ultima_coleta;
 if(s.rodando && s.processos.length) info+=' - desde '+s.processos[0].inicio;
 document.getElementById('statusInfo').textContent=info;
}
async function carregarProdutos(){
 const ps=await jget('/api/produtos');
 const tb=document.querySelector('#tabela tbody');tb.innerHTML='';
 const sel=document.getElementById('selProduto');const atual=sel.value;sel.innerHTML='';
 ps.forEach(p=>{
  const rep=p.reputacao?[p.reputacao.ml_level_id,p.reputacao.ml_selo,('score '+p.reputacao.score_confianca)].filter(Boolean).join(', '):'-';
  const preco=p.preco!=null?('R$ '+p.preco.toFixed(2)):'-';
  tb.innerHTML+='<tr><td>'+p.id+'</td><td>'+p.site+'</td><td><a href="'+p.url+'" target="_blank">'+p.item_id+'</a></td><td class="preco">'+preco+'</td><td>'+(p.coletado_em||'-')+'</td><td>'+rep+'</td></tr>';
  const o=document.createElement('option');o.value=p.id;o.textContent=p.site+' '+p.item_id;sel.appendChild(o);
 });
 if(atual) sel.value=atual;
}
async function carregarGrafico(){
 const id=document.getElementById('selProduto').value;
 if(!id) return;
 const dias=document.getElementById('selPeriodo').value;
 const h=await jget('/api/historico?id='+id+'&dias='+dias);
 const ctx=document.getElementById('grafico');
 if(chart) chart.destroy();
 chart=new Chart(ctx,{type:'line',data:{labels:h.labels,datasets:[{label:'Preco (R$)',data:h.precos,borderColor:'#4ade80',backgroundColor:'rgba(74,222,128,.12)',fill:true,tension:.25,pointRadius:3,pointHoverRadius:6,pointBackgroundColor:'#4ade80'}]},options:{plugins:{legend:{labels:{color:'#e6e6e6'}},tooltip:{callbacks:{label:c=>'  R$ '+c.parsed.y.toFixed(2)}}},scales:{x:{ticks:{color:'#9aa4b2'},grid:{color:'#232838'}},y:{ticks:{color:'#9aa4b2',callback:v=>'R$ '+v},grid:{color:'#232838'}}}}});
}
async function carregarTudo(){await carregarStatus();await carregarProdutos();await carregarGrafico();}
carregarTudo();
</script>
</body></html>'''


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        corpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _html(self, texto):
        corpo = texto.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        parsed = urlparse(self.path)
        rota = parsed.path
        q = parse_qs(parsed.query)
        try:
            if rota in ("/", "/index.html"):
                return self._html(PAGINA)
            if rota == "/api/status":
                return self._json(api_status())
            if rota == "/api/produtos":
                return self._json(api_produtos())
            if rota == "/api/historico":
                try:
                    pid = int(q.get("id", ["0"])[0])
                    dias = int(q.get("dias", ["30"])[0])
                except (ValueError, TypeError):
                    return self._json({"erro": "parametros invalidos"}, 400)
                return self._json(api_historico(pid, dias))
            return self._json({"erro": "nao encontrado"}, 404)
        except Exception as exc:
            return self._json({"erro": str(exc)}, 500)

    def log_message(self, *args):
        return


def main():
    parser = argparse.ArgumentParser(description="Dashboard web local (leitura) do Observer.")
    parser.add_argument("--porta", type=int, default=PORTA_PADRAO)
    parser.add_argument("--nao-abrir", action="store_true")
    args = parser.parse_args()

    servidor = ThreadingHTTPServer((HOST, args.porta), Handler)
    url = "http://localhost:" + str(args.porta)
    print("Dashboard rodando em " + url + "  (somente leitura, apenas localhost)")
    print("Pressione Ctrl+C para parar.")
    if not args.nao_abrir:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando dashboard...")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
