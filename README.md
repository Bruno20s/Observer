# Rastreador de Preços com IA (MVP)

Sistema pessoal que **monitora o preço de produtos** no Mercado Livre e na
Amazon e **avisa por e-mail** quando o preço muda — mostrando também a
reputação do vendedor, para ajudar a decidir a compra e evitar golpes.

Você cadastra o link de um produto uma vez. A partir daí, um robô verifica o
preço de tempos em tempos (a cada 4 horas por padrão), guarda um histórico e te
manda um e-mail sempre que o preço subir ou cair.

---

## Como funciona (visão geral)

```
   Você cola o link do produto
              │
              ▼
   ┌───────────────────┐
   │  Cadastro          │  identifica o site (ML/Amazon), extrai o id do
   │  (cadastro.py)     │  produto e salva no banco (sem duplicar)
   └───────────────────┘
              │
              ▼
   ┌───────────────────┐     a cada 4h (agendador)
   │  Job_Monitor       │◄─────────────────────────────┐
   │  (jobs/monitor.py) │                               │
   └───────────────────┘                               │
        │        │                                      │
        │        │ para cada produto cadastrado:        │
        ▼        ▼                                      │
   ┌─────────┐  ┌──────────────┐                        │
   │ Adapter │  │ Adapter      │   buscam PREÇO e        │
   │ Merc.   │  │ Amazon       │   REPUTAÇÃO do vendedor │
   │ Livre   │  │ (Playwright) │                        │
   └─────────┘  └──────────────┘                        │
        │                                               │
        ▼                                               │
   ┌───────────────────┐                                │
   │ Banco de dados     │  guarda cada preço/reputação   │
   │ (SQLite)           │  com data/hora (nunca apaga)   │
   └───────────────────┘                                │
        │                                               │
        ▼                                               │
   O preço mudou em relação à última leitura? ──── Não ─┘ (só registra)
        │
        │ Sim
        ▼
   ┌───────────────────┐
   │ Notificador        │  monta a mensagem (preço antigo, novo, %,
   │ (notificacao/      │  link, reputação) e envia por e-mail
   │  email.py)         │
   └───────────────────┘
              │
              ▼
     📧 E-mail na sua caixa
```

---

## O fluxo, passo a passo

### 1. Cadastro do produto
Você fornece a URL de um anúncio. O sistema descobre de qual site é, extrai o
identificador do produto e salva uma entrada no banco. Se você cadastrar o mesmo
produto duas vezes, ele **não duplica**.

- Mercado Livre: aceita links de catálogo (`.../p/MLBxxxxxxxx`).
- Amazon: aceita links com ASIN (`/dp/XXXXXXXXXX`).

### 2. Monitoramento periódico (o "robô")
Um agendador executa o **Job_Monitor** de tempos em tempos (padrão: 4 em 4
horas). A cada execução, ele percorre todos os produtos cadastrados e, para cada
um, chama o **adapter** do site correspondente.

- Se um produto falhar (site fora do ar, bloqueio, etc.), o erro é registrado e
  o robô **continua** com os outros — uma falha não derruba o ciclo inteiro.
- Há um intervalo mínimo entre consultas ao mesmo produto, para não exagerar nas
  requisições (importante para a Amazon).

### 3. Buscar preço e reputação (adapters)
Cada site tem seu próprio "adapter", isolado dos demais:

- **Mercado Livre** (via API oficial): usa o catálogo do produto. Entre os
  anúncios do mesmo produto, escolhe o **menor preço de um vendedor confiável**
  (vendedor com nível de reputação verde ou com selo MercadoLíder). Assim você
  acompanha o melhor preço do *mesmo* produto, sem cair num anúncio suspeito.
- **Amazon** (via navegador automatizado / Playwright): abre a página e extrai
  preço, nota, número de avaliações e quem vende/entrega. Faz isso com atrasos
  aleatórios e desiste educadamente se encontrar bloqueio/CAPTCHA.

Os adapters só **buscam dados** — quem decide se notifica é o Job_Monitor.

### 4. Guardar no histórico (banco de dados)
Cada leitura de preço e de reputação vira um **novo registro** com data/hora, em
um banco SQLite local (`rastreador.db`). O histórico é **append-only**: nunca
sobrescreve nem apaga — sempre acrescenta. Assim dá para ver a evolução do preço
ao longo do tempo.

### 5. Detectar mudança de preço
O sistema compara o preço novo com o **último preço registrado** daquele
produto:

- Preço **diferente** → é uma mudança (subida ou queda) → dispara notificação.
- Preço **igual** ou **primeira leitura** → só registra, sem notificar.

A comparação é exata (sem arredondamento), usando valores decimais.

### 6. Notificar por e-mail
Quando há mudança, o **Notificador** monta uma mensagem com: nome do produto,
preço anterior, preço novo, o percentual de variação (subida/queda), o link e um
resumo da reputação do vendedor. Envia por e-mail (SMTP/Gmail). Se o envio
falhar, o erro é registrado e o robô segue — o histórico nunca é revertido.

---

## Estrutura do projeto

```
Observer/
├── src/
│   ├── config.py            # lê as credenciais/config do arquivo .env
│   ├── main.py              # ponto de entrada: valida config e liga o agendador
│   ├── cadastro.py          # cadastro de produto a partir da URL
│   ├── adapters/
│   │   ├── base.py          # interface comum dos adapters
│   │   ├── mercado_livre.py # adapter do Mercado Livre (API, via catálogo)
│   │   ├── ml_oauth.py      # login/renovação de token do Mercado Livre
│   │   └── amazon.py        # adapter da Amazon (scraping com Playwright)
│   ├── db/
│   │   ├── models.py        # entidades (Produto, ProdutoSite, históricos)
│   │   └── database.py      # acesso ao SQLite (append-only)
│   ├── jobs/
│   │   └── monitor.py       # o robô: orquestra o ciclo de verificação
│   ├── confianca/
│   │   └── score.py         # calcula o score de confiança do vendedor
│   └── notificacao/
│       └── email.py         # monta e envia a notificação por e-mail
├── scripts/
│   ├── obter_tokens_ml.py   # setup: obtém os tokens iniciais do Mercado Livre
│   └── cadastrar_produto.py # cadastra produtos (cola a URL)
├── tests/                   # testes automatizados (nunca acessam rede real)
├── .env                     # suas credenciais (NÃO versionar)
├── .env.example             # modelo das credenciais esperadas
└── requirements.txt         # dependências
```

---

## Configuração (uma vez)

Todas as credenciais ficam no arquivo `.env` (nunca commitado). São 8 chaves:

- **Mercado Livre (5):** `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, `ML_REDIRECT_URI`,
  `ML_ACCESS_TOKEN`, `ML_REFRESH_TOKEN`.
- **E-mail (3):** `EMAIL_REMETENTE`, `EMAIL_SENHA_APP` (Senha de App do Gmail,
  não a senha normal), `EMAIL_DESTINATARIO`.

Se faltar alguma na inicialização, o sistema avisa qual e não sobe.

### Preparar o ambiente
```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

### Obter os tokens do Mercado Livre (setup único)
```powershell
python scripts/obter_tokens_ml.py
```
O script imprime uma URL de autorização; você autoriza no navegador, cola de
volta a URL de retorno, e ele imprime `ML_ACCESS_TOKEN` e `ML_REFRESH_TOKEN`
para colar no `.env`. Depois disso, o sistema renova o token de acesso sozinho.

---

## Como usar

### Cadastrar um produto
```powershell
python scripts/cadastrar_produto.py "https://www.mercadolivre.com.br/.../p/MLB46056259"
```
Ou sem argumento para o modo interativo (cola uma URL por linha). Para ver o que
já está cadastrado:
```powershell
python scripts/cadastrar_produto.py --listar
```

### Ligar o monitoramento
```powershell
python -m src.main
```
O sistema valida a configuração, registra o job e fica rodando, verificando os
preços a cada 4 horas. Deixe o processo aberto (ou rode em um servidor/VPS).

---

## Perguntas comuns

**Com que frequência ele verifica?** A cada 4 horas por padrão (definido em
`src/config.py`), respeitando um intervalo mínimo por produto.

**Ele guarda o histórico?** Sim. Cada leitura vira um registro novo no
`rastreador.db`, sem apagar os anteriores.

**Recebo e-mail toda hora?** Não — só quando o preço realmente muda em relação à
última leitura registrada.

**E se um site estiver fora do ar?** O erro daquele produto é registrado e o
robô continua com os demais. Nada trava o ciclo inteiro.

**Preço "indisponível" ou reputação sem score?** Alguns dados dependem do que a
API/página expõe. Quando um sinal obrigatório falta (ex.: percentual de
reclamações do vendedor), o score de confiança fica "indisponível", mas o
monitoramento de preço continua normalmente.
