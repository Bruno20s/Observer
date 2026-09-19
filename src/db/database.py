"""Camada de acesso ao SQLite: conexao e schema/migracao inicial (MVP).

Este modulo cobre a *criacao* do schema conforme o design (design.md, secao
"Schema SQLite"). Os acessores append-only (``inserir_historico_preco``,
``obter_ultimo_preco``, ``associar_produto`` etc.) sao implementados
separadamente (tarefa 3.2) e nao fazem parte deste modulo ainda.

Caracteristicas do schema (todas derivadas do design):

- **``UNIQUE (site, item_id)``** em ``produto_site`` impede cadastro duplicado
  do mesmo anuncio (R1.5).
- **Preco como ``TEXT``** (``Decimal`` serializado) em ``historico_preco``,
  nunca ``REAL``, para preservar o valor monetario exato (R4.2).
- **Timestamps ISO-8601 UTC** como ``TEXT`` (``criado_em``/``coletado_em``),
  portaveis para PostgreSQL.
- **Indices** por ``(produto_site_id, coletado_em)`` nas tabelas de historico,
  acelerando a busca do registro mais recente por ProdutoSite (R4.1).
- **Append-only por convencao:** as tabelas de historico nao sao alteradas por
  ``UPDATE``/``DELETE`` em uso normal; a camada de acesso (tarefa 3.2) expoe
  apenas insercoes e leituras.

Usa a stdlib ``sqlite3`` (sem ORM). ``FOREIGN KEY`` e habilitado por conexao
via ``PRAGMA foreign_keys = ON`` (o SQLite nao o habilita por padrao).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

from src.db.models import HistoricoPreco, HistoricoReputacao, ProdutoSite

#: Caminho especial aceito por ``sqlite3.connect`` para um banco em memoria.
IN_MEMORY = ":memory:"

# ---------------------------------------------------------------------------
# Definicao do schema (design.md -> "Schema SQLite")
# ---------------------------------------------------------------------------

#: Instrucoes DDL executadas em ordem para criar o schema inicial. Todas usam
#: ``IF NOT EXISTS`` para tornar ``criar_schema`` idempotente (uma migracao
#: inicial pode ser aplicada com seguranca a um banco novo ou ja existente).
SCHEMA_STATEMENTS: tuple[str, ...] = (
    # --- Produto -----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS produto (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        nome       TEXT NOT NULL,
        criado_em  TEXT NOT NULL              -- ISO-8601 UTC
    )
    """,
    # --- ProdutoSite -------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS produto_site (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        produto_id    INTEGER REFERENCES produto(id),  -- NULL = single-site
        site          TEXT NOT NULL,                   -- string aberta p/ extensao
        item_id       TEXT NOT NULL,                   -- MLB... ou ASIN
        url_original  TEXT NOT NULL,
        criado_em     TEXT NOT NULL,                   -- ISO-8601 UTC
        UNIQUE (site, item_id)                         -- impede duplicata (R1.5)
    )
    """,
    # --- HistoricoPreco (append-only) --------------------------------------
    """
    CREATE TABLE IF NOT EXISTS historico_preco (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        produto_site_id   INTEGER NOT NULL REFERENCES produto_site(id),
        preco             TEXT,                         -- Decimal serializado; NULL = indisponivel
        moeda             TEXT NOT NULL DEFAULT 'BRL',
        mudanca_detectada INTEGER NOT NULL DEFAULT 0,   -- 0/1
        coletado_em       TEXT NOT NULL                 -- ISO-8601 UTC
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hist_preco_ps_ts
        ON historico_preco(produto_site_id, coletado_em)
    """,
    # --- HistoricoReputacao (append-only) ----------------------------------
    """
    CREATE TABLE IF NOT EXISTS historico_reputacao (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        produto_site_id           INTEGER NOT NULL REFERENCES produto_site(id),
        ml_level_id               TEXT,
        ml_selo                   TEXT,
        ml_percentual_reclamacoes REAL,
        amz_nota_media            REAL,
        amz_num_avaliacoes        INTEGER,
        amz_vendido_por_amazon    INTEGER,               -- 0/1/NULL
        score_confianca           TEXT,                  -- "0".."100" ou "indisponivel"
        sinais_indisponiveis      INTEGER NOT NULL DEFAULT 0,
        coletado_em               TEXT NOT NULL          -- ISO-8601 UTC
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hist_rep_ps_ts
        ON historico_reputacao(produto_site_id, coletado_em)
    """,
)


def conectar(caminho: Union[str, Path] = IN_MEMORY) -> sqlite3.Connection:
    """Abre uma conexao SQLite pronta para uso pelo sistema.

    Configura a conexao com convencoes uteis para o dominio:

    - ``PRAGMA foreign_keys = ON`` para que as ``FOREIGN KEY`` sejam de fato
      aplicadas (o SQLite as ignora por padrao).
    - ``row_factory = sqlite3.Row`` para acesso a colunas por nome.

    Args:
        caminho: Caminho do arquivo do banco, ou ``":memory:"`` (default) para
            um banco em memoria — util em testes.

    Returns:
        Uma :class:`sqlite3.Connection` configurada. O chamador e responsavel
        por fecha-la (ou usa-la como context manager para transacoes).
    """
    conn = sqlite3.connect(str(caminho))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def criar_schema(conn: sqlite3.Connection) -> None:
    """Cria (ou garante a existencia de) todas as tabelas e indices do MVP.

    Executa a migracao inicial descrita no design. E idempotente: todas as
    instrucoes usam ``IF NOT EXISTS``, entao pode ser chamada com seguranca na
    inicializacao mesmo com um banco ja migrado.

    Args:
        conn: Conexao SQLite aberta (ver :func:`conectar`).
    """
    with conn:  # transaciona: commit ao final se nao houver excecao
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def inicializar_banco(caminho: Union[str, Path] = IN_MEMORY) -> sqlite3.Connection:
    """Abre uma conexao e garante que o schema esteja criado.

    Conveniencia que combina :func:`conectar` e :func:`criar_schema`.

    Args:
        caminho: Caminho do arquivo do banco, ou ``":memory:"`` (default).

    Returns:
        Conexao pronta para uso, com o schema ja aplicado.
    """
    conn = conectar(caminho)
    criar_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Acessores append-only (design.md -> "Data Models" / tarefa 3.2)
# ---------------------------------------------------------------------------
#
# Estas funcoes formam a *unica* superficie de acesso as tabelas de historico.
# Por design, expomos apenas insercoes (``inserir_*``) e leituras
# (``obter_ultimo_preco``, ``listar_produto_site``); nao ha ``update``/``delete``
# de historico. Isso impoe o invariante append-only por convencao de acesso
# (R3.2, R6.3): nenhum registro previamente gravado e alterado ou removido.
#
# ``associar_produto``/``desassociar_produto`` alteram apenas o vinculo
# (``produto_site.produto_id``); nunca tocam nas tabelas de historico (R2.5).
#
# Convencao de serializacao do preco (design.md): ``Decimal`` <-> ``TEXT`` via
# ``str(Decimal)`` / ``Decimal(str)``, garantindo round-trip sem perda de
# precisao (R4.2). ``None`` representa preco indisponivel.


def _serializar_preco(preco: Optional[Decimal]) -> Optional[str]:
    """Serializa um ``Decimal`` para ``TEXT`` (ou ``None``) para o banco."""
    return None if preco is None else str(preco)


def _desserializar_preco(valor: Optional[str]) -> Optional[Decimal]:
    """Reconstroi um ``Decimal`` a partir do ``TEXT`` do banco (ou ``None``)."""
    return None if valor is None else Decimal(valor)


def _linha_para_historico_preco(row: sqlite3.Row) -> HistoricoPreco:
    """Converte uma linha de ``historico_preco`` em :class:`HistoricoPreco`."""
    return HistoricoPreco(
        id=row["id"],
        produto_site_id=row["produto_site_id"],
        preco=_desserializar_preco(row["preco"]),
        moeda=row["moeda"],
        mudanca_detectada=bool(row["mudanca_detectada"]),
        coletado_em=row["coletado_em"],
    )


def _linha_para_produto_site(row: sqlite3.Row) -> ProdutoSite:
    """Converte uma linha de ``produto_site`` em :class:`ProdutoSite`."""
    return ProdutoSite(
        id=row["id"],
        produto_id=row["produto_id"],
        site=row["site"],
        item_id=row["item_id"],
        url_original=row["url_original"],
        criado_em=row["criado_em"],
    )


def inserir_historico_preco(
    conn: sqlite3.Connection, historico: HistoricoPreco
) -> HistoricoPreco:
    """Insere um novo :class:`HistoricoPreco` (append-only).

    Sempre cria um novo registro; nunca atualiza/sobrescreve registros
    anteriores do mesmo ProdutoSite (R3.2). O preco (``Decimal`` ou ``None``) e
    serializado como ``TEXT`` para preservar o valor monetario exato (R4.2).

    Args:
        conn: Conexao SQLite aberta.
        historico: Dados a inserir. O campo ``id`` e ignorado (o banco atribui
            via AUTOINCREMENT).

    Returns:
        Uma nova :class:`HistoricoPreco` identica a entrada, com ``id``
        preenchido pelo banco.
    """
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO historico_preco
                (produto_site_id, preco, moeda, mudanca_detectada, coletado_em)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                historico.produto_site_id,
                _serializar_preco(historico.preco),
                historico.moeda,
                1 if historico.mudanca_detectada else 0,
                historico.coletado_em,
            ),
        )
    return HistoricoPreco(
        id=cursor.lastrowid,
        produto_site_id=historico.produto_site_id,
        preco=historico.preco,
        moeda=historico.moeda,
        mudanca_detectada=historico.mudanca_detectada,
        coletado_em=historico.coletado_em,
    )


def inserir_historico_reputacao(
    conn: sqlite3.Connection, historico: HistoricoReputacao
) -> HistoricoReputacao:
    """Insere um novo :class:`HistoricoReputacao` (append-only).

    Sempre cria um novo registro; nunca atualiza/sobrescreve registros
    anteriores do mesmo ProdutoSite (R6.3). Cada adapter preenche o subconjunto
    de campos do seu site; os demais permanecem ``None``.

    Args:
        conn: Conexao SQLite aberta.
        historico: Dados a inserir. O campo ``id`` e ignorado (o banco atribui
            via AUTOINCREMENT).

    Returns:
        Uma nova :class:`HistoricoReputacao` identica a entrada, com ``id``
        preenchido pelo banco.
    """
    amz_vendido = historico.amz_vendido_por_amazon
    amz_vendido_int = None if amz_vendido is None else (1 if amz_vendido else 0)
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO historico_reputacao
                (produto_site_id, ml_level_id, ml_selo,
                 ml_percentual_reclamacoes, amz_nota_media, amz_num_avaliacoes,
                 amz_vendido_por_amazon, score_confianca, sinais_indisponiveis,
                 coletado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historico.produto_site_id,
                historico.ml_level_id,
                historico.ml_selo,
                historico.ml_percentual_reclamacoes,
                historico.amz_nota_media,
                historico.amz_num_avaliacoes,
                amz_vendido_int,
                historico.score_confianca,
                1 if historico.sinais_indisponiveis else 0,
                historico.coletado_em,
            ),
        )
    return HistoricoReputacao(
        id=cursor.lastrowid,
        produto_site_id=historico.produto_site_id,
        coletado_em=historico.coletado_em,
        ml_level_id=historico.ml_level_id,
        ml_selo=historico.ml_selo,
        ml_percentual_reclamacoes=historico.ml_percentual_reclamacoes,
        amz_nota_media=historico.amz_nota_media,
        amz_num_avaliacoes=historico.amz_num_avaliacoes,
        amz_vendido_por_amazon=historico.amz_vendido_por_amazon,
        score_confianca=historico.score_confianca,
        sinais_indisponiveis=historico.sinais_indisponiveis,
    )


def inserir_produto_site(
    conn: sqlite3.Connection, produto_site: ProdutoSite
) -> Optional[ProdutoSite]:
    """Insere uma nova entrada :class:`ProdutoSite`, respeitando ``UNIQUE(site, item_id)``.

    Usado pelo cadastro (R1.6). O par (``site``, ``item_id``) e unico no banco;
    quando ja existir uma entrada com o mesmo par, a insercao viola a restricao
    ``UNIQUE(site, item_id)`` e esta funcao retorna ``None`` sem persistir uma
    segunda linha (dedup — R1.5). Isso permite ao chamador tratar a duplicata de
    forma graciosa sem propagar ``sqlite3.IntegrityError``.

    Args:
        conn: Conexao SQLite aberta.
        produto_site: Dados a inserir. Os campos ``id`` e ``criado_em`` de
            entrada sao respeitados; se ``id`` vier preenchido ele e ignorado
            (o banco atribui via AUTOINCREMENT).

    Returns:
        A nova :class:`ProdutoSite` com ``id`` preenchido em caso de sucesso, ou
        ``None`` quando ja existir uma entrada com o mesmo (``site``, ``item_id``).
    """
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO produto_site
                    (produto_id, site, item_id, url_original, criado_em)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    produto_site.produto_id,
                    produto_site.site,
                    produto_site.item_id,
                    produto_site.url_original,
                    produto_site.criado_em,
                ),
            )
    except sqlite3.IntegrityError:
        # Viola UNIQUE(site, item_id): ja cadastrado (R1.5). Nada e persistido.
        return None
    return ProdutoSite(
        id=cursor.lastrowid,
        produto_id=produto_site.produto_id,
        site=produto_site.site,
        item_id=produto_site.item_id,
        url_original=produto_site.url_original,
        criado_em=produto_site.criado_em,
    )


def obter_ultimo_preco(
    conn: sqlite3.Connection, produto_site_id: int
) -> Optional[HistoricoPreco]:
    """Retorna o :class:`HistoricoPreco` mais recente de um ProdutoSite.

    "Mais recente" e o registro de maior ``coletado_em``; em empate de
    timestamp, o de maior ``id`` (ordem de insercao) — o mesmo criterio de
    "preco atual" usado na detecao de mudanca (R4.1) e na comparacao entre
    sites (R2.3).

    Args:
        conn: Conexao SQLite aberta.
        produto_site_id: Identificador do ProdutoSite.

    Returns:
        O :class:`HistoricoPreco` mais recente, ou ``None`` se o ProdutoSite
        nao tiver nenhum registro de preco.
    """
    row = conn.execute(
        """
        SELECT id, produto_site_id, preco, moeda, mudanca_detectada, coletado_em
        FROM historico_preco
        WHERE produto_site_id = ?
        ORDER BY coletado_em DESC, id DESC
        LIMIT 1
        """,
        (produto_site_id,),
    ).fetchone()
    return None if row is None else _linha_para_historico_preco(row)


def listar_produto_site(conn: sqlite3.Connection) -> list[ProdutoSite]:
    """Lista todas as entradas :class:`ProdutoSite` cadastradas.

    Usado pelo Job_Monitor para iterar sobre os itens monitorados (R3.1).
    Ordena por ``id`` para um resultado deterministico.

    Args:
        conn: Conexao SQLite aberta.

    Returns:
        Lista de :class:`ProdutoSite` (possivelmente vazia).
    """
    rows = conn.execute(
        """
        SELECT id, produto_id, site, item_id, url_original, criado_em
        FROM produto_site
        ORDER BY id
        """
    ).fetchall()
    return [_linha_para_produto_site(row) for row in rows]


def associar_produto(
    conn: sqlite3.Connection, produto_site_id: int, produto_id: int
) -> None:
    """Associa um :class:`ProdutoSite` a um :class:`Produto` (define ``produto_id``).

    Apenas o vinculo (``produto_site.produto_id``) e alterado; nenhum registro
    de historico e tocado, preservando o invariante append-only (R2.5).

    Args:
        conn: Conexao SQLite aberta.
        produto_site_id: Identificador do ProdutoSite a associar.
        produto_id: Identificador do Produto de destino.
    """
    with conn:
        conn.execute(
            "UPDATE produto_site SET produto_id = ? WHERE id = ?",
            (produto_id, produto_site_id),
        )


def desassociar_produto(conn: sqlite3.Connection, produto_site_id: int) -> None:
    """Remove a associacao de um :class:`ProdutoSite` (``produto_id = NULL``).

    Retorna a entrada ao modo single-site (R2.5/R2.6). Apenas o vinculo e
    removido; o historico de preco/reputacao daquela entrada permanece
    inalterado (append-only).

    Args:
        conn: Conexao SQLite aberta.
        produto_site_id: Identificador do ProdutoSite a desassociar.
    """
    with conn:
        conn.execute(
            "UPDATE produto_site SET produto_id = NULL WHERE id = ?",
            (produto_site_id,),
        )
