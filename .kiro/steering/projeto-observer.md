---
inclusion: always
---

# Observer — Rastreador de Preços com IA (MVP) — Guia rápido para o Kiro

> Arquivo de contexto resumido para novas sessões. Explica o que é o projeto,
> como está organizado, as decisões-chave e como rodar/testar. Os docs de
> steering `product.md`, `tech.md` e `structure.md` (na raiz) têm o detalhe
> completo; os specs vivem em `.kiro/specs/rastreador-precos-mvp/`.

## O que é

Sistema pessoal (usuário único) em **Python 3.11+** que monitora o preço de
produtos no **Mercado Livre** e na **Amazon**, guarda histórico **append-only**
em **SQLite**, calcula um **score de confiança determinístico** do vendedor e
**notifica por e-mail** (SMTP/Gmail) quando o preço muda. Roda como job agendado
(a cada 4h por padrão), pensado para VPS pequena ou Raspberry Pi / notebook.

O nome da pasta é `Observer`; o produto se chama "Rastreador de Preços com IA".

## Estado atual

- MVP implementado e coberto por testes (pytest + hypothesis). O spec
  `.kiro/specs/rastreador-precos-mvp/tasks.md` tem o plano de implementação com
  a maioria das tarefas marcadas concluídas.
- `rastreador.db` (SQLite local) existe na raiz e não é versionado.
- Ambiente Python local observado: **3.14** (o alvo suportado do projeto é 3.11+).

## Arquitetura (fluxo end-to-end)

```
URL do produto
   → Cadastro (src/cadastro.py): roteia p/ parse_url do adapter, dedup, persiste ProdutoSite
   → Scheduler (src/main.py, lib `schedule`) dispara a cada 4h
   → Job_Monitor (src/jobs/monitor.py): para cada ProdutoSite
        · respeita Intervalo_Minimo (>=1h, default 4h)
        · adapter.buscar_preco() com timeout (default 30s)
        · grava HistoricoPreco (append-only)
        · detecta mudança (Decimal exato, sem tolerância)
        · adapter.buscar_reputacao_vendedor() → calcular_score() → grava HistoricoReputacao
        · se mudou → montar_mensagem() + enviar_notificacao() (e-mail)
```

Três princípios centrais (não violar):
1. **Isolamento por site via adapters.** Nenhum adapter importa outro. Adapters
   só *buscam dados*; a decisão de notificar vive em `jobs/monitor.py`.
2. **Histórico append-only.** Preço/reputação nunca são sobrescritos; cada
   coleta gera novo registro com timestamp. Sem UPDATE/DELETE de histórico.
3. **Extensibilidade** para o futuro modo "caçador de pechincha" (usados) e
   novos marketplaces, reaproveitando banco + notificador.

## Layout do código (`src/`)

- `config.py` — lê as **8 variáveis de ambiente obrigatórias** do `.env` (sem
  defaults embutidos), valida presença/não-vazio e aborta com `ConfigError` se
  faltar. Expõe `ParametrosOperacionais` (Intervalo_Minimo, frequência do job,
  timeout, delay Amazon) com defaults. Loader `.env` próprio, sem dependência.
- `cadastro.py` — `cadastrar_produto(conn, url, email_destino=None)`. Roteia a
  URL, valida, deduplica via `UNIQUE(site,item_id)`. Retorna `ResultadoCadastro`
  tipado (`SUCESSO`/`URL_INVALIDA`/`DUPLICADO`); nunca lança.
- `main.py` — entrypoint. `inicializar_app()` (config → banco → adapters →
  registra job) e `main()` (loop do `schedule`). Faz um ciclo imediato ao subir.
  Funções fatoradas (`registrar_job`, `loop_agendador`) para teste sem sleep/loop.
- `adapters/base.py` — contrato comum: enum `Site`, dataclasses `ItemRef`,
  `PrecoResult`, `ReputacaoResult`, ABC `BaseAdapter` (`parse_url` @staticmethod,
  `buscar_preco`, `buscar_reputacao_vendedor`). **Convenção: nunca lançam por
  falha externa; retornam `sucesso=False` + `erro`.**
- `adapters/mercado_livre.py` — via **API oficial (OAuth2)**, **estratégia de
  catálogo**. NÃO usa `GET /items/{id}` (retorna 403 no escopo do projeto). Usa
  `GET /products/{id}`, `GET /products/{id}/items`, `GET /users/{seller_id}`.
  Escolhe o **menor preço entre anúncios `new` de vendedor confiável** (level
  `4_light_green`/`5_green` OU selo MercadoLíder). Links são de catálogo
  (`/p/MLB...`); regex de id: `MLB\d+`.
- `adapters/ml_oauth.py` — `ClienteOAuthML`: chamadas Bearer; em 401 renova via
  `refresh_token` e repete 1x; rotaciona o refresh **em memória** (nunca em
  disco); após 3 falhas de refresh sinaliza `ReautenticacaoManualNecessaria` e
  **preserva** as credenciais. Cliente HTTP injetável (`httpx`).
- `adapters/amazon.py` — via **scraping (Playwright, Chromium headless)**. ASIN
  em `/dp/<ASIN>` ou `/gp/product/<ASIN>` (`[A-Z0-9]{10}`). Atraso aleatório
  antes de cada requisição (default 2–8s), detecta CAPTCHA/bloqueio (429/503 +
  marcadores textuais) e **desiste do item no ciclo** sem reintentar. Seletores
  centralizados em `_SELETORES`; parsing puro sobre HTML (fetch via seam
  injetável `buscar_pagina`, import do Playwright é lazy). Preço em `Decimal`
  (formato BR: `.` milhar, `,` decimal).
- `db/models.py` — dataclasses `Produto`, `ProdutoSite`, `HistoricoPreco`,
  `HistoricoReputacao`. **Preço é `Decimal`** (serializado como TEXT), nunca
  float. Timestamps ISO-8601 UTC. `site` é string aberta (extensível).
- `db/database.py` — `sqlite3` puro (sem ORM). Schema idempotente
  (`IF NOT EXISTS`), `PRAGMA foreign_keys = ON`, `UNIQUE(site,item_id)`, índices
  por `(produto_site_id, coletado_em)`. Acessores **só** inserção/leitura:
  `inserir_historico_preco/reputacao`, `obter_ultimo_preco` (maior
  `coletado_em`, desempate por `id`), `listar_produto_site`, `associar_produto`,
  `desassociar_produto`, `definir_email_destino`. Migração idempotente da coluna
  `email_destino`.
- `jobs/monitor.py` — orquestração do ciclo. `detectar_mudanca(anterior, novo)`
  é **pura** (Decimal exato: igual/subida/queda; primeira leitura = sem
  mudança). `executar_ciclo(...)` com **todas as dependências injetáveis**
  (adapters, relógio, executor de timeout, remetente) e **isolamento de erro por
  item** (uma falha nunca derruba o ciclo).
- `confianca/score.py` — score **0–100 determinístico**, sem LLM, sem I/O.
  ML: `0.5*nível + 0.3*selo + 0.2*reclamações`. Amazon:
  `0.6*nota + 0.2*volume + 0.2*vendedor`. Sinal obrigatório ausente/ inválido →
  sentinela `"indisponivel"`. Coeficientes em constantes de módulo.
- `notificacao/email.py` — funções puras `calcular_percentual` /
  `montar_mensagem` (nome, preços, % com rótulo subida/queda, link, comparação
  entre sites quando ≥2 ProdutoSite, resumo de reputação). Envio real por SMTP
  Gmail (`smtp.gmail.com:587`, STARTTLS, Senha de App) com retry + backoff.
  `enviar_notificacao` **nunca lança e nunca escreve no banco** (fallback local).

## Scripts utilitários (`scripts/`) — execução manual

- `obter_tokens_ml.py` — setup único: fluxo OAuth (Authorization Code + PKCE),
  imprime `ML_ACCESS_TOKEN`/`ML_REFRESH_TOKEN` para colar no `.env`.
- `cadastrar_produto.py` — cadastra produto(s) por URL (`--listar`, modo
  interativo, `--db`).
- `definir_email.py` — define/limpa o `email_destino` por produto.
- `ver_produtos.py` — resumo no terminal (produtos + histórico, horário local).
- `status.py` — verifica se `python -m src.main` está rodando (usa psutil se houver).
- `dashboard.py` — dashboard web **somente leitura** em `127.0.0.1` (Chart.js via CDN).
- `grafico_preco.py` — gera HTML interativo do histórico de preços.

## Configuração (`.env`, nunca commitado — ver `.env.example`)

8 chaves obrigatórias:
- ML (5): `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, `ML_REDIRECT_URI`,
  `ML_ACCESS_TOKEN`, `ML_REFRESH_TOKEN`.
- E-mail (3): `EMAIL_REMETENTE`, `EMAIL_SENHA_APP` (Senha de App do Gmail),
  `EMAIL_DESTINATARIO`.

## Comandos (Windows / PowerShell)

```powershell
# ambiente
pip install -r requirements.txt
python -m playwright install chromium

# setup de tokens ML (uma vez)
python scripts/obter_tokens_ml.py

# cadastrar e rodar
python scripts/cadastrar_produto.py "https://www.mercadolivre.com.br/.../p/MLB..."
python -m src.main            # inicia o monitoramento (loop bloqueante)

# testes (nunca acessam rede real; tudo mockado)
pytest
```

Dependências (`requirements.txt`): `playwright`, `schedule`, `httpx`, `pytest`,
`hypothesis`. Nada de rede real em testes/CI.

## Testes (`tests/`)

pytest + testes property-based (hypothesis). Cobrem parsing de URL (round-trip e
rejeição), dedup do cadastro, append-only e "último preço", detecção de mudança,
Intervalo_Minimo, isolamento de erro, integração do monitor, score, notificação,
config e main. Há um agente de review em
`.kiro/agents/revisor-rastreador-precos.md`.

## Convenções e regras que o Kiro deve respeitar

- **Docstrings e mensagens em português.** Código segue PEP 8 e usa type hints.
- **Dinheiro sempre `Decimal`** (nunca float). Comparação de preço é exata.
- **Nenhum segredo em código/log.** Credenciais só via `.env`/ambiente.
- **Adapters isolados**; a lógica de notificação fica no `monitor.py`.
- **Append-only**: não introduzir UPDATE/DELETE em histórico.
- **Funções de I/O externo nunca derrubam o job**: capturam e retornam
  `sucesso=False`; o ciclo continua com os demais itens.
- **SQLite via `conn.execute` parametrizado** (sem interpolação de string em SQL).
- **Fora de escopo no MVP**: LLM no score, WhatsApp, múltiplos usuários,
  marketplaces de usados/veículos (planejados como evolução futura).
