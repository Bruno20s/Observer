"""Entidades do dominio persistidas no SQLite (MVP).

Este modulo define as quatro entidades do modelo de dados descritas no design
("Modelo de Dados"): :class:`Produto`, :class:`ProdutoSite`,
:class:`HistoricoPreco` e :class:`HistoricoReputacao`.

Decisoes de modelagem (ver design.md, secao "Data Models"):

- **Preco como ``Decimal``.** O preco monetario e representado por
  :class:`decimal.Decimal` na camada de dominio e serializado como ``TEXT`` no
  banco, preservando o valor exato e permitindo comparacao sem arredondamento
  (R4.2). Nunca ``float``/``REAL`` para preco.
- **Timestamps ISO-8601 UTC.** ``criado_em``/``coletado_em`` sao strings
  ISO-8601 em UTC (``TEXT``), portaveis para uma futura migracao a PostgreSQL.
- **Historico append-only.** ``HistoricoPreco`` e ``HistoricoReputacao`` nunca
  sao atualizados/sobrescritos; cada consulta gera um novo registro (R3.2,
  R6.3). A garantia append-only e imposta pela camada de acesso
  (``database.py``), nao por estas dataclasses.
- **``site`` como string aberta.** ``ProdutoSite.site`` e uma string livre (nao
  um enum fechado no banco) para acomodar novos marketplaces no futuro
  (ex.: ``"olx"``) sem migracao.

As dataclasses aqui sao objetos de dominio leves. O campo ``id`` e opcional
(``None`` antes da insercao; preenchido pelo banco via AUTOINCREMENT).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Produto:
    """Entidade logica que agrupa uma ou mais entradas :class:`ProdutoSite`.

    Um mesmo produto real pode existir em varios sites; ``Produto`` e o vinculo
    que permite compara-los (R2).
    """

    nome: str
    #: Timestamp ISO-8601 UTC de criacao.
    criado_em: str
    #: Chave primaria; ``None`` antes da persistencia (preenchida pelo banco).
    id: Optional[int] = None


@dataclass(frozen=True)
class ProdutoSite:
    """Associa um :class:`Produto` a um anuncio especifico em um site.

    Pode existir com ``produto_id = None`` (modo single-site, sem agrupamento)
    e ser associado a um ``Produto`` posteriormente (R2). O par
    (``site``, ``item_id``) e unico no banco, impedindo duplicatas (R1.5).
    """

    #: String aberta identificando o site (ex.: ``"mercado_livre"``, ``"amazon"``).
    site: str
    #: Identificador do item no site: ``"MLB..."`` no ML ou ASIN na Amazon.
    item_id: str
    #: URL original do anuncio informada no cadastro.
    url_original: str
    #: Timestamp ISO-8601 UTC de criacao.
    criado_em: str
    #: FK para :class:`Produto`; ``None`` quando single-site (sem agrupamento).
    produto_id: Optional[int] = None
    #: Chave primaria; ``None`` antes da persistencia.
    id: Optional[int] = None


@dataclass(frozen=True)
class HistoricoPreco:
    """Registro append-only de um preco coletado de um :class:`ProdutoSite`.

    O preco e um :class:`decimal.Decimal` para preservar valor monetario exato;
    ``None`` representa preco indisponivel/invalido no momento da coleta (R4.6).
    """

    produto_site_id: int
    #: Preco exato coletado; ``None`` quando indisponivel.
    preco: Optional[Decimal]
    #: Timestamp ISO-8601 UTC da consulta.
    coletado_em: str
    moeda: str = "BRL"
    #: ``True`` quando esta coleta representou uma mudanca de preco (R4.3).
    mudanca_detectada: bool = False
    #: Chave primaria; ``None`` antes da persistencia.
    id: Optional[int] = None


@dataclass(frozen=True)
class HistoricoReputacao:
    """Registro append-only dos sinais de reputacao de um :class:`ProdutoSite`.

    Cada adapter preenche o subconjunto de campos do seu site (ML ou Amazon);
    os campos do outro site permanecem ``None``. ``score_confianca`` guarda o
    score normalizado como string (``"0"``..``"100"``) ou ``"indisponivel"``
    quando um sinal obrigatorio falta (R6.5).
    """

    produto_site_id: int
    #: Timestamp ISO-8601 UTC da consulta.
    coletado_em: str

    # --- Sinais do Mercado Livre (None quando nao se aplica) ---
    ml_level_id: Optional[str] = None
    ml_selo: Optional[str] = None
    ml_percentual_reclamacoes: Optional[float] = None

    # --- Sinais da Amazon (None quando nao se aplica) ---
    amz_nota_media: Optional[float] = None
    amz_num_avaliacoes: Optional[int] = None
    #: ``True``/``False``/``None`` (vendido e entregue pela Amazon?).
    amz_vendido_por_amazon: Optional[bool] = None

    # --- Comuns ---
    #: ``"0"``..``"100"`` ou ``"indisponivel"``.
    score_confianca: Optional[str] = None
    #: ``True`` se algum sinal esperado faltou nesta coleta (R6.5).
    sinais_indisponiveis: bool = False
    #: Chave primaria; ``None`` antes da persistencia.
    id: Optional[int] = None
