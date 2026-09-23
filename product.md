---
inclusion: always
---

# Produto: Rastreador de Preços com IA

## O que é
Sistema pessoal que monitora produtos específicos marcados pelo usuário em múltiplos
sites (Mercado Livre e Amazon), guarda o histórico de preços, notifica o usuário via
e-mail sempre que o preço mudar, e mostra a reputação/confiabilidade do vendedor
para ajudar a evitar golpes ou produtos de má qualidade.

## Problema que resolve
O usuário hoje verifica manualmente o preço de produtos de interesse (ex: um monitor
específico) de tempos em tempos. Isso é repetitivo, não guarda histórico estruturado,
e não considera a reputação do vendedor na hora de decidir se vale a pena comprar.

## Usuário-alvo
Uso pessoal (um único usuário, o dono do projeto), sem necessidade de suportar
múltiplos usuários no MVP. Arquitetura deve ser simples o suficiente para rodar
localmente ou em um servidor pessoal barato (ex: VPS pequena ou Raspberry Pi).

## Funcionalidades essenciais (MVP)
1. Cadastro de produto via URL (Mercado Livre e/ou Amazon).
2. Agrupamento manual de um mesmo produto real cadastrado em sites diferentes
   (ex: "Monitor LG 27 Ultragear" existindo em ML e Amazon).
3. Monitoramento periódico de preço (job agendado, não sob demanda).
4. Histórico de preços persistido (nunca sobrescrever, sempre acrescentar).
5. Notificação via e-mail sempre que o preço de um produto monitorado mudar
   (subida ou queda), incluindo comparação entre sites quando aplicável.
6. Exibição da reputação do vendedor (Mercado Livre: nível de reputação, selo
   MercadoLíder, % de reclamações; Amazon: nota média, número de avaliações,
   se é vendido/entregue pela própria Amazon ou por terceiro).

## Fora de escopo no MVP (mas planejado para o futuro — ver seção "Expansão Futura")
- Monitoramento de marketplaces de produtos usados (OLX, Facebook Marketplace).
- Suporte a múltiplos usuários / autenticação de usuários.
- Dashboard web (pode vir em uma fase posterior).
- Score de confiança combinado via LLM (pode ser fórmula simples no MVP).

## Expansão Futura (não implementar agora, mas manter a arquitetura extensível para isso)
Adicionar um modo "caçador de pechincha" que monitora categorias inteiras em
marketplaces de produtos usados (ex: peças de PC, celulares usados, consoles),
calculando preço médio de mercado e sinalizando anúncios muito abaixo da média
como possível oportunidade ou possível golpe. Este modo deve reaproveitar a
mesma infraestrutura de banco de dados e notificação do MVP.

### Marketplaces de veículos (ex: Webmotors) — desejado, porém complexo
Incluir monitoramento de veículos (Webmotors e similares) é um objetivo
desejado, registrado aqui como evolução futura de maior esforço. A avaliação
de viabilidade (feita durante o MVP) apontou os seguintes desafios, que
precisam ser resolvidos antes de implementar:

- **Sem API pública de leitura de mercado.** A Webmotors possui um Portal do
  Desenvolvedor (via Sensedia), mas todas as APIs são voltadas ao LOJISTA
  (publicar/gerenciar o próprio estoque, receber leads) e exigem contrato
  comercial. Nenhuma delas permite consultar preços de anúncios de terceiros.
  Portanto, ao contrário do Mercado Livre (API oficial de leitura), aqui não
  há caminho oficial para obter os dados de mercado.
- **Só resta scraping**, com a mesma fragilidade e risco de bloqueio já
  identificados para outros sites sem API (ex: Shopee): proteção anti-bot,
  seletores que quebram quando o layout muda, e necessidade de manutenção
  contínua.
- **Itens únicos, não catálogo.** Um carro usado é único (quilometragem, ano,
  estado, opcionais). "Monitorar o preço" muda de significado: em vez de
  comparar o mesmo item entre vendedores, é preciso definir se o alvo é um
  anúncio específico (que some quando o carro é vendido) ou o preço médio de
  um modelo (que exige coletar e normalizar muitos anúncios).

Por essas razões, a Webmotors encaixa-se melhor no modo "caçador de
pechincha" acima (preço médio de mercado + deteccão de oportunidade), e não
como um adapter simples de preço à vista. Fica planejada para quando esse
modo for desenvolvido, reaproveitando a infraestrutura de banco e notificação.

## Critérios de sucesso
- O sistema roda de forma autônoma (job agendado) sem intervenção manual diária.
- Uma mudança real de preço em qualquer produto monitorado gera uma notificação
  no e-mail do usuário em até poucas horas.
- As informações de reputação/confiança do vendedor aparecem junto de cada
  notificação e de cada consulta de produto.
