"""Interface comum dos adapters de site (MVP).

Este modulo define o contrato compartilhado por todos os adapters de site,
conforme a secao "Components and Interfaces" -> "Interface comum de adapter"
do design. Cada site (Mercado Livre, Amazon e, no futuro, marketplaces de
usados) e acessado por um adapter independente que implementa esta interface.

Principios (ver design.md e steering docs):

- **Isolamento por site.** Nenhum adapter importa outro adapter. Os adapters
  apenas *buscam dados*; a decisao de notificar vive em ``jobs/monitor.py``.
- **Nunca lancam por falha externa.** ``buscar_preco`` e
  ``buscar_reputacao_vendedor`` NUNCA propagam excecao por falha de
  rede/parsing: retornam um resultado com ``sucesso=False`` e ``erro``
  preenchido, garantindo o isolamento exigido em R3.3 e R6.6.
- **``parse_url`` e ``@staticmethod``** para poder ser usada no cadastro (R1)
  sem instanciar credenciais.

Os campos das dataclasses de resultado carregam exatamente os sinais
necessarios para popular ``HistoricoPreco`` e ``HistoricoReputacao``
(ver ``src/db/models.py``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


class Site(str, Enum):
    """Sites suportados pelo sistema.

    Os valores string casam com o campo ``ProdutoSite.site`` usado na camada de
    dados. ``site`` no banco e uma string aberta (ver ``db/models.py``), entao
    novos marketplaces (ex.: ``"olx"``) podem ser adicionados como novos membros
    deste enum sem migracao de schema.

    Herda de ``str`` para que o membro seja diretamente comparavel/serializavel
    como a string do banco (ex.: ``Site.MERCADO_LIVRE == "mercado_livre"``).
    """

    MERCADO_LIVRE = "mercado_livre"
    AMAZON = "amazon"
    # Futuro: OLX = "olx", FACEBOOK_MARKETPLACE = "facebook_marketplace"


@dataclass(frozen=True)
class ItemRef:
    """Resultado do parsing de uma URL de anuncio.

    Produzido por :meth:`BaseAdapter.parse_url`. Identifica o site e o
    identificador do item (``"MLB..."`` no Mercado Livre; ASIN de 10 caracteres
    na Amazon) e preserva a URL original informada no cadastro.
    """

    site: Site
    #: Identificador do item: ``"MLB..."`` no ML, ASIN de 10 chars na Amazon.
    item_id: str
    #: URL original do anuncio, como informada pelo usuario.
    url_original: str


@dataclass(frozen=True)
class PrecoResult:
    """Resultado tipado de uma consulta de preco.

    Em caso de falha (rede/parsing/timeout), ``sucesso`` e ``False`` e ``erro``
    descreve o motivo; ``preco`` fica ``None``. Nunca ha excecao propagada para
    fora do adapter (R3.3).
    """

    sucesso: bool
    #: Preco exato; ``None`` quando indisponivel ou em caso de erro.
    preco: Optional[Decimal] = None
    moeda: str = "BRL"
    #: Motivo da falha quando ``sucesso`` e ``False``.
    erro: Optional[str] = None
    #: Titulo do anuncio, quando obtido.
    nome_produto: Optional[str] = None


@dataclass(frozen=True)
class ReputacaoResult:
    """Resultado tipado de uma consulta de reputacao do vendedor.

    Cada adapter preenche apenas o subconjunto de campos do seu site; os campos
    do outro site permanecem ``None``. Os campos casam com as colunas de
    ``HistoricoReputacao`` (ver ``db/models.py``).

    Em caso de falha total, ``sucesso`` e ``False`` e ``erro`` descreve o
    motivo. Quando a consulta ocorre mas algum sinal falta, ``sucesso`` pode ser
    ``True`` com ``sinais_indisponiveis=True`` (R6.5, R6.6).
    """

    sucesso: bool

    # --- Sinais do Mercado Livre ---
    #: Nivel de reputacao, ex.: ``"5_green"``.
    ml_level_id: Optional[str] = None
    #: Selo MercadoLider: ``"ausente"``|``"mercadolider"``|``"gold"``|``"platinum"``.
    ml_selo_mercadolider: Optional[str] = None
    #: Percentual de reclamacoes no intervalo ``0..100``.
    ml_percentual_reclamacoes: Optional[float] = None

    # --- Sinais da Amazon ---
    #: Nota media no intervalo ``0.0..5.0``.
    amz_nota_media: Optional[float] = None
    #: Numero de avaliacoes (``>= 0``).
    amz_num_avaliacoes: Optional[int] = None
    #: ``True`` quando vendido e entregue pela Amazon.
    amz_vendido_por_amazon: Optional[bool] = None

    # --- Comuns ---
    #: ``True`` se algum sinal esperado faltou.
    sinais_indisponiveis: bool = False
    #: Motivo da falha quando ``sucesso`` e ``False``.
    erro: Optional[str] = None


class BaseAdapter(ABC):
    """Interface comum de adapter de site.

    Nenhum adapter importa outro adapter. Subclasses devem definir o atributo de
    classe :attr:`site` e implementar :meth:`parse_url`, :meth:`buscar_preco` e
    :meth:`buscar_reputacao_vendedor`.

    Convencao de erro: :meth:`buscar_preco` e :meth:`buscar_reputacao_vendedor`
    NUNCA lancam por falha de rede/parsing; retornam um resultado com
    ``sucesso=False`` e ``erro`` preenchido (R3.3, R6.6).
    """

    #: Site atendido por este adapter. Definido pela subclasse.
    site: Site

    @staticmethod
    @abstractmethod
    def parse_url(url: str) -> Optional[ItemRef]:
        """Extrai ``(site, item_id)`` de uma URL de anuncio.

        Retorna ``None`` quando a URL nao pertence a este site ou quando o
        identificador do item nao pode ser extraido. E ``@staticmethod`` para
        uso no cadastro (R1) sem instanciar credenciais.
        """
        ...

    @abstractmethod
    def buscar_preco(self, produto_site_id: str) -> PrecoResult:
        """Consulta o preco atual do item.

        Nunca lanca por falha externa: em erro, retorna ``PrecoResult`` com
        ``sucesso=False`` e ``erro`` preenchido.
        """
        ...

    @abstractmethod
    def buscar_reputacao_vendedor(self, produto_site_id: str) -> ReputacaoResult:
        """Consulta os sinais de reputacao do vendedor.

        Nunca lanca por falha externa: em erro, retorna ``ReputacaoResult`` com
        ``sucesso=False`` e ``erro`` preenchido.
        """
        ...
