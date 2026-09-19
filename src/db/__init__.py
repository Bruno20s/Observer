"""Camada de dados SQLite (MVP): entidades, schema e acessores append-only."""

from src.db.database import (
    IN_MEMORY,
    associar_produto,
    conectar,
    criar_schema,
    desassociar_produto,
    inicializar_banco,
    inserir_historico_preco,
    inserir_historico_reputacao,
    listar_produto_site,
    obter_ultimo_preco,
)
from src.db.models import (
    HistoricoPreco,
    HistoricoReputacao,
    Produto,
    ProdutoSite,
)

__all__ = [
    "IN_MEMORY",
    "conectar",
    "criar_schema",
    "inicializar_banco",
    "inserir_historico_preco",
    "inserir_historico_reputacao",
    "obter_ultimo_preco",
    "listar_produto_site",
    "associar_produto",
    "desassociar_produto",
    "Produto",
    "ProdutoSite",
    "HistoricoPreco",
    "HistoricoReputacao",
]
