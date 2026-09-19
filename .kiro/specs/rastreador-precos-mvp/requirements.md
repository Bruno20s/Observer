# Requirements Document

## Introduction

Sistema pessoal que monitora produtos específicos cadastrados pelo usuário no
Mercado Livre e na Amazon, mantém histórico de preços, notifica o usuário via
e-mail quando o preço muda, e exibe a reputação do vendedor para apoiar a
decisão de compra.

## Glossary

- **Produto**: Item lógico real que o usuário deseja monitorar, podendo agregar
  múltiplas entradas `ProdutoSite` de sites diferentes.
- **ProdutoSite**: Entrada que representa um produto em um site específico,
  incluindo site, id do item (ex.: `item_id` do Mercado Livre ou `ASIN` da
  Amazon) e URL original.
- **HistoricoPreco**: Série histórica de registros de preço de um `ProdutoSite`,
  cada registro com valor e timestamp, sem sobrescrita.
- **HistoricoReputacao**: Série histórica de registros de reputação de vendedor
  associados a um `ProdutoSite`, cada registro com timestamp, sem sobrescrita.
- **Job_Monitor**: Job agendado que consulta periodicamente o preço e a
  reputação de cada `ProdutoSite` cadastrado.
- **Score_Confianca**: Score de confiança determinístico calculado a partir dos
  sinais de reputação do vendedor, sem depender de LLM no MVP.
- **Access_Token**: Token de acesso OAuth do Mercado Livre usado nas consultas.
- **Refresh_Token**: Token usado para renovar automaticamente o `Access_Token`
  do Mercado Livre quando este expira.
- **Intervalo_Minimo**: Intervalo mínimo configurável entre consultas ao mesmo
  `ProdutoSite`, para evitar excesso de requisições.
- **Notificador**: Componente responsável por enviar notificações ao usuário via
  e-mail (SMTP) quando uma mudança de preço é detectada.

## Requirements

### Requirement 1: Cadastro de Produto

**User Story:** Como usuário, quero cadastrar um produto colando o link do
anúncio, para que o sistema comece a monitorar seu preço.

#### Acceptance Criteria

1. QUANDO o usuário fornecer uma URL de um anúncio do Mercado Livre, O SISTEMA
   DEVE identificar o site e extrair o `item_id` correspondente.
2. QUANDO o usuário fornecer uma URL de um produto da Amazon, O SISTEMA DEVE
   identificar o site e extrair o `ASIN` correspondente.
3. SE a URL fornecida não corresponder a nenhum site suportado, ENTÃO O
   SISTEMA DEVE rejeitar o cadastro e informar o motivo.
4. QUANDO um produto for cadastrado com sucesso, O SISTEMA DEVE persistir uma
   entrada `ProdutoSite` associada a ele, incluindo site, id do item e URL
   original.

### Requirement 2: Agrupamento de Produto Entre Sites

**User Story:** Como usuário, quero agrupar o mesmo produto real cadastrado em
sites diferentes, para que o sistema compare os preços entre eles.

#### Acceptance Criteria

1. QUANDO o usuário associar duas ou mais entradas `ProdutoSite` a um mesmo
   `Produto` lógico, O SISTEMA DEVE tratar essas entradas como referentes ao
   mesmo item para fins de comparação.
2. SE um `Produto` tiver apenas uma entrada `ProdutoSite`, ENTÃO O SISTEMA
   DEVE funcionar normalmente sem exibir comparação entre sites.

### Requirement 3: Monitoramento Periódico de Preço

**User Story:** Como usuário, quero que o sistema verifique o preço dos
produtos monitorados periodicamente, sem que eu precise checar manualmente.

#### Acceptance Criteria

1. QUANDO o job agendado for executado, O SISTEMA DEVE consultar o preço
   atual de cada `ProdutoSite` cadastrado, usando o adapter correspondente ao
   seu site.
2. QUANDO uma consulta de preço for concluída com sucesso, O SISTEMA DEVE
   persistir um novo registro em `HistoricoPreco` com o valor e o timestamp,
   sem apagar ou sobrescrever registros anteriores.
3. SE a consulta a um adapter falhar (erro de rede, bloqueio, mudança na
   página), ENTÃO O SISTEMA DEVE registrar o erro e continuar o processamento
   dos demais produtos, sem interromper o job inteiro.
4. O SISTEMA DEVE respeitar um intervalo mínimo configurável entre consultas
   ao mesmo `ProdutoSite`, para evitar excesso de requisições (especialmente
   ao adapter da Amazon).

### Requirement 4: Detecção de Mudança de Preço

**User Story:** Como usuário, quero ser avisado apenas quando o preço
realmente mudar, não a cada verificação.

#### Acceptance Criteria

1. QUANDO um novo preço for obtido para um `ProdutoSite`, O SISTEMA DEVE
   compará-lo com o último preço registrado em `HistoricoPreco` para o mesmo
   `ProdutoSite`.
2. SE o novo preço for diferente do último preço registrado, ENTÃO O SISTEMA
   DEVE marcar essa ocorrência como "mudança detectada" e disparar uma
   notificação (ver Requirement 5).
3. SE o novo preço for igual ao último preço registrado, ENTÃO O SISTEMA NÃO
   DEVE disparar notificação.

### Requirement 5: Notificação via E-mail

**User Story:** Como usuário, quero receber um e-mail sempre que o preço de
um produto monitorado mudar.

#### Acceptance Criteria

1. QUANDO uma mudança de preço for detectada, O SISTEMA DEVE enviar um
   e-mail (via SMTP) contendo: nome do produto, preço anterior, preço
   novo, percentual de variação, e link do anúncio.
2. SE o `Produto` tiver mais de uma entrada `ProdutoSite` (agrupado entre
   sites), ENTÃO a mensagem DEVE incluir o preço atual em cada site
   monitorado, não apenas no site onde a mudança ocorreu.
3. A mensagem DEVE incluir um resumo da reputação do vendedor associada ao
   `ProdutoSite` onde a mudança ocorreu (ver Requirement 6).
4. SE o envio da notificação falhar, ENTÃO O SISTEMA DEVE registrar o erro
   localmente, sem interromper o restante do job.

### Requirement 6: Reputação do Vendedor

**User Story:** Como usuário, quero ver a reputação/confiabilidade do
vendedor junto do preço, para evitar golpes ou produtos de má qualidade.

#### Acceptance Criteria

1. QUANDO o adapter do Mercado Livre consultar um produto, O SISTEMA DEVE
   também obter o `seller_reputation` do vendedor (nível, selo MercadoLíder,
   percentual de reclamações).
2. QUANDO o adapter da Amazon consultar um produto, O SISTEMA DEVE também
   obter a nota média, o número de avaliações, e se o item é vendido/entregue
   pela própria Amazon ou por terceiro.
3. QUANDO uma consulta de reputação for concluída, O SISTEMA DEVE persistir
   um novo registro em `HistoricoReputacao`, associado ao `ProdutoSite` e com
   timestamp (histórico, não sobrescrita).
4. O SISTEMA DEVE calcular um score de confiança simples a partir desses
   sinais, seguindo uma fórmula determinística definida no design técnico
   (sem depender de LLM no MVP).

### Requirement 7: Configuração e Segurança de Credenciais

**User Story:** Como usuário/desenvolvedor, quero que credenciais sensíveis
nunca fiquem expostas no código-fonte.

#### Acceptance Criteria

1. O SISTEMA DEVE ler todas as credenciais (Client ID/Secret do Mercado
   Livre, tokens OAuth, e as credenciais de e-mail EMAIL_REMETENTE,
   EMAIL_SENHA_APP e EMAIL_DESTINATARIO) exclusivamente de variáveis de
   ambiente.
2. O SISTEMA NÃO DEVE conter nenhuma credencial em texto puro no
   código-fonte versionado.
3. QUANDO o Access Token do Mercado Livre expirar, O SISTEMA DEVE renová-lo
   automaticamente usando o Refresh Token, sem exigir nova autenticação
   manual do usuário.
