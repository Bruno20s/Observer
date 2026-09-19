"""Cadastro de produto (registration) — MVP.

Este modulo implementa o fluxo de cadastro (R1): recebe uma URL de anuncio,
roteia-a para o ``parse_url`` do adapter correto (Mercado Livre ou Amazon),
valida e persiste exatamente uma entrada :class:`ProdutoSite` em caso de
sucesso. Segue a rastreabilidade do design (design.md -> "Cadastro de Produto"):
o parsing/roteamento vive nos adapters e a persistencia/dedup na camada
``src/db``.

Regras (R1.3, R1.4, R1.5, R1.6):

- Se nenhum adapter reconhecer a URL (``parse_url`` retorna ``None`` para
  todos) -> rejeita como URL invalida/nao suportada; nada e persistido (R1.3/R1.4).
- Se um adapter reconhecer -> persiste exatamente UMA entrada ``ProdutoSite``
  (site, item_id, url_original) via camada de dados (R1.6).
- Dedup: se ja existir ``ProdutoSite`` com o mesmo (site, item_id), rejeita como
  duplicata (via ``UNIQUE(site, item_id)``) e NAO persiste uma segunda linha
  (R1.5).

A funcao ``cadastrar_produto`` nunca lanca por entrada invalida/duplicada:
retorna um :class:`ResultadoCadastro` tipado com ``sucesso``/``status`` e uma
mensagem em portugues, coerente com a convencao de resultados tipados do
projeto.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Sequence
import sqlite3

from src.adapters.amazon import Amazon
from src.adapters.base import BaseAdapter, ItemRef
from src.adapters.mercado_livre import MercadoLivre
from src.db import database
from src.db.models import ProdutoSite

#: Adapters consultados, em ordem, para rotear a URL pelo ``parse_url``.
#: A ordem (ML depois Amazon) e deterministica; cada ``parse_url`` so casa com
#: URLs do seu proprio site, entao no maximo um adapter reconhece a URL.
_ADAPTERS: tuple[type[BaseAdapter], ...] = (MercadoLivre, Amazon)


class StatusCadastro(str, Enum):
    """Resultado possivel de uma tentativa de cadastro."""

    #: Entrada ProdutoSite persistida com sucesso (R1.6).
    SUCESSO = "sucesso"
    #: URL vazia, longa demais, dominio nao suportado, ou sem id valido (R1.3/R1.4).
    URL_INVALIDA = "url_invalida"
    #: Ja existe ProdutoSite com o mesmo (site, item_id) (R1.5).
    DUPLICADO = "duplicado"


@dataclass(frozen=True)
class ResultadoCadastro:
    """Resultado tipado de :func:`cadastrar_produto`.

    Em sucesso, ``produto_site`` traz a entrada persistida (com ``id``). Em
    rejeicao, ``produto_site`` e ``None`` e ``mensagem`` explica o motivo.
    """

    status: StatusCadastro
    mensagem: str
    produto_site: Optional[ProdutoSite] = None

    @property
    def sucesso(self) -> bool:
        """``True`` apenas quando o cadastro foi persistido."""
        return self.status is StatusCadastro.SUCESSO


def _rotear_url(
    url: str, adapters: Sequence[type[BaseAdapter]]
) -> Optional[ItemRef]:
    """Tenta ``parse_url`` de cada adapter e retorna o primeiro ``ItemRef``.

    Retorna ``None`` quando nenhum adapter reconhece a URL (site nao suportado
    ou identificador do item ausente/invalido — R1.3/R1.4).
    """
    for adapter in adapters:
        ref = adapter.parse_url(url)
        if ref is not None:
            return ref
    return None


def cadastrar_produto(
    conn: sqlite3.Connection,
    url: str,
    adapters: Sequence[type[BaseAdapter]] = _ADAPTERS,
) -> ResultadoCadastro:
    """Cadastra um produto a partir da URL de um anuncio (R1).

    Roteia a URL para o ``parse_url`` do adapter correto, valida e persiste
    exatamente uma entrada :class:`ProdutoSite` em caso de sucesso. Nunca lanca
    por entrada invalida/duplicada: retorna um :class:`ResultadoCadastro`.

    Args:
        conn: Conexao SQLite aberta (schema ja criado).
        url: URL do anuncio informada pelo usuario.
        adapters: Adapters candidatos para o roteamento; por padrao Mercado
            Livre e Amazon. Injetavel para testes.

    Returns:
        :class:`ResultadoCadastro` com ``status`` ``SUCESSO`` (e a
        ``ProdutoSite`` persistida), ``URL_INVALIDA`` (R1.3/R1.4) ou
        ``DUPLICADO`` (R1.5). Em rejeicao nada e persistido.
    """
    ref = _rotear_url(url, adapters)
    if ref is None:
        return ResultadoCadastro(
            status=StatusCadastro.URL_INVALIDA,
            mensagem=(
                "URL invalida: o site nao e suportado ou o identificador do "
                "item nao pode ser extraido da URL."
            ),
        )

    novo = ProdutoSite(
        site=ref.site.value,
        item_id=ref.item_id,
        url_original=ref.url_original,
        criado_em=datetime.now(timezone.utc).isoformat(),
    )
    persistido = database.inserir_produto_site(conn, novo)
    if persistido is None:
        # UNIQUE(site, item_id) violado: ja cadastrado (R1.5). Nada persistido.
        return ResultadoCadastro(
            status=StatusCadastro.DUPLICADO,
            mensagem=(
                f"Produto ja cadastrado: {ref.site.value}/{ref.item_id} "
                "ja possui uma entrada."
            ),
        )

    return ResultadoCadastro(
        status=StatusCadastro.SUCESSO,
        mensagem=f"Produto cadastrado: {ref.site.value}/{ref.item_id}.",
        produto_site=persistido,
    )
