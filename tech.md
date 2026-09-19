---
inclusion: always
---

# Stack Tecnológica e Decisões Técnicas

## Linguagem
Python 3.11+ para todo o backend (scraper, adapters, job agendado, notificador).

## Adapters de site (módulo por site, interface comum)
Cada adapter deve implementar a mesma interface, por exemplo:
- `buscar_preco(produto_site_id) -> PrecoResult`
- `buscar_reputacao_vendedor(produto_site_id) -> ReputacaoResult`

### Mercado Livre — API oficial (não fazer scraping deste site)
- Autenticação: OAuth 2.0. Requer Client ID e Client Secret de uma aplicação
  criada em developers.mercadolivre.com.br.
- Estratégia de CATÁLOGO (decidida após teste empírico): o endpoint de anúncio
  individual `GET /items/{item_id}` retorna **403** com o escopo de leitura
  pública usado pelo projeto, e os links monitorados são de catálogo
  (`/p/<PRODUCT_ID>`). Portanto o adapter usa somente:
  - `GET /products/{product_id}` — ficha do catálogo (nome do produto).
  - `GET /products/{product_id}/items` — anúncios do produto (cada um com
    `item_id`, `seller_id`, `price`, `currency_id`, `condition`).
  - `GET /users/{seller_id}` — `seller_reputation` (`level_id`,
    `power_seller_status`, `transactions.ratings`; na prática `ratings` costuma
    vir vazio, tornando o percentual de reclamações indisponível).
- Seleção do preço: entre os anúncios `condition == "new"`, escolhe-se o de
  MENOR preço cujo vendedor é **confiável** — `level_id` em
  {`4_light_green`, `5_green`} OU com selo MercadoLíder
  (`power_seller_status` não nulo). Isso monitora o melhor preço do MESMO
  produto de catálogo (mesmas características), entre vendedores confiáveis.
- Preferir, quando possível, assinar notificações via webhook (tópico
  `items_prices`) em vez de só fazer polling — avaliar na fase de design.
- Access Token expira; implementar renovação via Refresh Token.
- Nunca commitar Client ID / Client Secret / tokens no código — usar variáveis
  de ambiente (ver `.env.example`).

### Amazon — scraping (não existe API de preço pública acessível)
- Ferramenta: Playwright (preferir sobre Selenium/BeautifulSoup puro, pois o
  conteúdo é renderizado via JS).
- Extrair: preço atual, nota média (estrelas), número de avaliações, se o
  produto é "Vendido e entregue pela Amazon" ou por vendedor terceiro.
- Rate limiting obrigatório: nunca consultar o mesmo produto com frequência
  menor que algumas horas. Delays aleatórios entre requisições.
- Tratar bloqueios/CAPTCHA de forma graciosa: logar o erro e tentar de novo no
  próximo ciclo, nunca insistir imediatamente.

## Banco de dados
- MVP: SQLite (arquivo local, sem servidor).
- Evolução futura (não implementar agora): migração para PostgreSQL.
- Nunca sobrescrever registros de preço/reputação — sempre inserir novo
  registro com timestamp (histórico append-only).

## Agendamento
- Biblioteca `schedule` (Python) para rodar o job de verificação periodicamente,
  ou alternativa via cron do sistema operacional se rodando em servidor Linux.
- Frequência default sugerida: a cada 4 horas (ajustável por configuração).

## Notificação — E-mail
- MVP: envio via SMTP (biblioteca `smtplib`, nativa do Python) usando uma
  conta Gmail com Senha de App (não a senha normal da conta).
- Motivo da escolha: CallMeBot (WhatsApp) foi avaliado, mas descartado no
  MVP por instabilidade prática (número de ativação muda com frequência,
  sem SLA). E-mail via SMTP não depende de serviço terceiro não-oficial.
- Evolução futura (não implementar agora): migração para notificação via
  WhatsApp usando Twilio, caso o projeto evolua para atender múltiplos
  usuários ou o usuário prefira notificação mais imediata no celular.
- Fallback: se o envio de e-mail falhar, logar o erro localmente (sem
  bloquear o restante do job).

## Score de confiança do vendedor
- MVP: fórmula simples e determinística combinando os sinais disponíveis por
  site (não usar LLM no MVP, para manter custo e complexidade baixos).
- Evolução futura (não implementar agora): combinar sinais via chamada a um
  LLM (API da Anthropic ou OpenAI) para gerar um resumo em texto explicando o
  motivo do score.

## Testes
- `pytest` para testes unitários dos adapters (com respostas de API/HTML
  mockadas, nunca testes que dependem de rede real rodando em CI).

## Variáveis de ambiente esperadas (ver `.env.example`)
- `ML_CLIENT_ID`
- `ML_CLIENT_SECRET`
- `ML_REDIRECT_URI`
- `ML_ACCESS_TOKEN` (gerado após o fluxo OAuth, não é fixo)
- `ML_REFRESH_TOKEN`
- `EMAIL_REMETENTE`
- `EMAIL_SENHA_APP`
- `EMAIL_DESTINATARIO`

## Convenções de código
- Cada adapter em seu próprio módulo/arquivo, isolado dos demais.
- Nenhuma credencial ou segredo em texto puro no código-fonte.
- Toda função que faz requisição externa (API ou scraping) deve ter tratamento
  de erro explícito e não deve derrubar o job inteiro em caso de falha pontual.