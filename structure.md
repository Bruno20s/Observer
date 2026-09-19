---
inclusion: always
---

# Estrutura de Projeto Sugerida

```
rastreador-precos/
├── .kiro/
│   ├── steering/          # este contexto persistente (product.md, tech.md, structure.md)
│   └── specs/             # specs de features (requirements.md, design.md, tasks.md)
├── src/
│   ├── adapters/
│   │   ├── base.py        # interface comum dos adapters
│   │   ├── mercado_livre.py
│   │   └── amazon.py
│   ├── db/
│   │   ├── models.py      # Produto, ProdutoSite, HistoricoPreco, HistoricoReputacao
│   │   └── database.py
│   ├── jobs/
│   │   └── monitor.py     # job agendado que consulta os adapters
│   ├── notificacao/
│   │   └── email.py       # envio via SMTP (Gmail)
│   ├── confianca/
│   │   └── score.py       # cálculo do score de confiança do vendedor
│   └── config.py          # leitura de variáveis de ambiente
├── tests/
│   ├── test_adapters/
│   └── test_jobs/
├── .env.example
├── .env                    # nunca commitado (adicionar ao .gitignore)
├── requirements.txt
└── README.md
```

## Regras de organização
- Nenhum adapter deve importar diretamente de outro adapter.
- A lógica de detecção de mudança de preço vive em `jobs/monitor.py`, não dentro
  dos adapters (adapters só buscam dados, não decidem se notifica).
- O `.env` real nunca deve ser commitado; `.env.example` documenta as chaves
  esperadas sem valores reais.