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

## Critérios de sucesso
- O sistema roda de forma autônoma (job agendado) sem intervenção manual diária.
- Uma mudança real de preço em qualquer produto monitorado gera uma notificação
  no e-mail do usuário em até poucas horas.
- As informações de reputação/confiança do vendedor aparecem junto de cada
  notificação e de cada consulta de produto.
