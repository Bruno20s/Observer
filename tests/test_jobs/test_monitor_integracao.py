"""Testes de exemplo/integracao do Job_Monitor (`src/jobs/monitor.py`).

Estes sao testes de EXEMPLO/integracao (nao property-based): exercitam
``executar_ciclo`` contra um banco SQLite em memoria com adapters, relogio,
executor de timeout e remetente de notificacao TODOS injetados -- nunca ha
rede nem tempo real.

Cobrem tres cenarios do fluxo do Job_Monitor:

1. Roteamento de adapter por site (R3.1): cada ProdutoSite e consultado pelo
   adapter do seu proprio site (ML via adapter ML, Amazon via adapter Amazon),
   e nunca pelo adapter do outro site.
2. Timeout de consulta (R3.6): quando a consulta de preco estoura o timeout, o
   item e pulado (nenhum HistoricoPreco novo e persistido) e o processamento
   segue para o proximo item.
3. Transicoes single-site <-> multi-site no agrupamento (R2.1, R2.2, R2.4,
   R2.6): a secao de comparacao entre sites so aparece na notificacao quando o
   Produto agrupa >= 2 ProdutoSite; associar faz a secao aparecer e
   desassociar (de volta a single-site) faz a secao desaparecer.

**Validates: Requirements 3.1, 3.6, 2.1, 2.2, 2.4, 2.6**
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from src.adapters.base import BaseAdapter, PrecoResult, ReputacaoResult, Site
from src.config import Config, Credenciais, ParametrosOperacionais
from src.db import database
from src.db.models import HistoricoPreco
from src.jobs.monitor import executar_ciclo
from src.notificacao.email import CABECALHO_COMPARACAO, ResultadoEnvio

# Relogio fixo (UTC-aware) injetado no ciclo -- deterministico, sem tempo real.
_AGORA_FIXO = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

# Preco anterior semeado e preco retornado pelo adapter. Sao DIFERENTES para que
# ``detectar_mudanca`` dispare (houve mudanca) e o remetente seja chamado.
_PRECO_ANTERIOR = Decimal("100.00")
_PRECO_NOVO = Decimal("199.90")

# Instante do preco anterior: bem no passado (> 1h) para que o item esteja "due"
# mesmo respeitando o piso de 1h do Intervalo_Minimo (R3.4).
_T_ANTERIOR = _AGORA_FIXO - timedelta(hours=2)


def _config_ficticia() -> Config:
    """Config com credenciais ficticias (e-mail SMTP) e Intervalo_Minimo no piso.

    As credenciais NAO tocam a rede: o remetente e um fake. O Intervalo_Minimo
    e fixado no piso permitido (1h); como o preco anterior e semeado 2h no
    passado, o item esta "due".
    """
    credenciais = Credenciais(
        ml_client_id="cid",
        ml_client_secret="secret",
        ml_redirect_uri="https://exemplo/callback",
        ml_access_token="access",
        ml_refresh_token="refresh",
        email_remetente="remetente@gmail.com",
        email_senha_app="senha-de-app-ficticia",
        email_destinatario="destino@example.com",
    )
    return Config(
        credenciais=credenciais,
        parametros=ParametrosOperacionais(intervalo_minimo_segundos=3600),
    )


def _reputacao_ok() -> ReputacaoResult:
    """Reputacao valida e completa (nao e o foco destes testes)."""
    return ReputacaoResult(
        sucesso=True,
        ml_level_id="5_green",
        ml_selo_mercadolider="platinum",
        ml_percentual_reclamacoes=1.0,
        amz_nota_media=4.5,
        amz_num_avaliacoes=100,
        amz_vendido_por_amazon=True,
    )


class _AdapterFake(BaseAdapter):
    """Adapter fake que registra os ``item_id`` consultados (preco/reputacao).

    Retorna sempre um preco valido (``_PRECO_NOVO``) para que, quando "due", o
    ciclo persista um novo ``HistoricoPreco`` e -- havendo preco anterior
    diferente -- dispare a notificacao.
    """

    def __init__(self, site: Site) -> None:
        self.site = site
        self.itens_preco: list[str] = []
        self.itens_reputacao: list[str] = []

    @staticmethod
    def parse_url(url: str):  # nao usado nestes testes
        return None

    def buscar_preco(self, produto_site_id: str) -> PrecoResult:
        self.itens_preco.append(produto_site_id)
        return PrecoResult(
            sucesso=True,
            preco=_PRECO_NOVO,
            moeda="BRL",
            nome_produto=f"Produto {produto_site_id}",
        )

    def buscar_reputacao_vendedor(self, produto_site_id: str) -> ReputacaoResult:
        self.itens_reputacao.append(produto_site_id)
        return _reputacao_ok()


class _RemetenteFake:
    """Remetente de notificacao fake: captura as mensagens (sem rede)."""

    def __init__(self) -> None:
        self.mensagens: list[str] = []

    def __call__(
        self, mensagem, remetente, senha_app, destinatario, *, produto_site_id=None
    ) -> ResultadoEnvio:
        self.mensagens.append(mensagem)
        return ResultadoEnvio(sucesso=True, tentativas=1)


def _executor_inline(func: Callable[[], PrecoResult], timeout: float) -> PrecoResult:
    """Executor de timeout inline: chama ``func()`` direto (sem threads)."""
    return func()


def _inserir_produto_site(
    conn: sqlite3.Connection,
    site: Site,
    item_id: str,
    *,
    produto_id: Optional[int] = None,
) -> int:
    """Insere uma linha ``produto_site`` e retorna o id gerado."""
    cur = conn.execute(
        """
        INSERT INTO produto_site (produto_id, site, item_id, url_original, criado_em)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            produto_id,
            site.value,
            item_id,
            f"https://exemplo/{item_id}",
            _AGORA_FIXO.isoformat(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _inserir_produto(conn: sqlite3.Connection, nome: str) -> int:
    """Insere uma linha ``produto`` e retorna o id gerado."""
    cur = conn.execute(
        "INSERT INTO produto (nome, criado_em) VALUES (?, ?)",
        (nome, _AGORA_FIXO.isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def _semear_preco_anterior(conn: sqlite3.Connection, ps_id: int) -> None:
    """Semeia um HistoricoPreco anterior (2h atras) com preco != adapter.

    Assim o item esta "due" (t_anterior > Intervalo_Minimo) e ``detectar_mudanca``
    dispara (preco anterior != preco do adapter), acionando a notificacao.
    """
    database.inserir_historico_preco(
        conn,
        HistoricoPreco(
            produto_site_id=ps_id,
            preco=_PRECO_ANTERIOR,
            moeda="BRL",
            mudanca_detectada=False,
            coletado_em=_T_ANTERIOR.isoformat(),
        ),
    )
    conn.commit()


def _contar_historico_preco(conn: sqlite3.Connection, ps_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM historico_preco WHERE produto_site_id = ?",
        (ps_id,),
    ).fetchone()[0]


def _resetar_e_semear_anterior(conn: sqlite3.Connection, ps_id: int) -> None:
    """Limpa o historico de preco do item e semeia apenas o preco anterior.

    Necessario entre ciclos no teste de transicao: cada ciclo grava um novo
    HistoricoPreco (``_PRECO_NOVO``) em ``_AGORA_FIXO``, que passaria a ser o
    "ultimo preco". Para reavaliar a mudanca (e o status "due") de forma limpa
    em um novo ciclo, removemos os registros e deixamos somente o anterior
    (2h no passado, diferente do preco do adapter). Este reset e um utilitario
    de teste; nao integra a camada de dados append-only de producao.
    """
    conn.execute("DELETE FROM historico_preco WHERE produto_site_id = ?", (ps_id,))
    conn.commit()
    _semear_preco_anterior(conn, ps_id)


# ===========================================================================
# 1. Roteamento de adapter por site (R3.1)
# ===========================================================================


def test_roteamento_adapter_por_site() -> None:
    """Cada ProdutoSite e consultado pelo adapter do seu proprio site (R3.1).

    Injeta um adapter fake DISTINTO por Site e verifica que cada fake foi
    chamado para o item do seu site -- e NAO para o item do outro site.

    **Validates: Requirements 3.1**
    """
    conn = database.inicializar_banco(":memory:")
    try:
        ps_ml = _inserir_produto_site(conn, Site.MERCADO_LIVRE, "MLB111")
        ps_amz = _inserir_produto_site(conn, Site.AMAZON, "ASIN000001")

        adapter_ml = _AdapterFake(Site.MERCADO_LIVRE)
        adapter_amz = _AdapterFake(Site.AMAZON)

        executar_ciclo(
            conn,
            config=_config_ficticia(),
            adapters={
                Site.MERCADO_LIVRE: adapter_ml,
                Site.AMAZON: adapter_amz,
            },
            agora=lambda: _AGORA_FIXO,
            executor_timeout=_executor_inline,
            remetente=_RemetenteFake(),
        )

        # O adapter do ML viu apenas o item do ML; o da Amazon apenas o da Amazon.
        assert adapter_ml.itens_preco == ["MLB111"]
        assert adapter_amz.itens_preco == ["ASIN000001"]

        # Nenhum adapter viu o item do outro site (roteamento correto).
        assert "ASIN000001" not in adapter_ml.itens_preco
        assert "MLB111" not in adapter_amz.itens_preco

        # E a persistencia refletiu o roteamento: um registro por item.
        assert _contar_historico_preco(conn, ps_ml) == 1
        assert _contar_historico_preco(conn, ps_amz) == 1
    finally:
        conn.close()


# ===========================================================================
# 2. Timeout de consulta (R3.6)
# ===========================================================================


def test_timeout_consulta_pula_item_e_continua() -> None:
    """Timeout na consulta pula o item (sem persistir) e segue para o proximo (R3.6).

    Injeta um ``executor_timeout`` que levanta ``TimeoutError`` para o item alvo
    (identificado pelo ``nome_produto`` retornado) e verifica que nenhum novo
    HistoricoPreco e gravado para ele, enquanto um segundo item (rapido) e
    processado normalmente.

    **Validates: Requirements 3.6**
    """
    conn = database.inicializar_banco(":memory:")
    try:
        ps_lento = _inserir_produto_site(conn, Site.MERCADO_LIVRE, "MLB_LENTO")
        ps_rapido = _inserir_produto_site(conn, Site.AMAZON, "ASIN_RAPID0")

        adapter_ml = _AdapterFake(Site.MERCADO_LIVRE)
        adapter_amz = _AdapterFake(Site.AMAZON)

        def executor_timeout(
            func: Callable[[], PrecoResult], timeout: float
        ) -> PrecoResult:
            # Roda a func; se corresponder ao item lento, converte em TimeoutError
            # (simulando o estouro do limite ANTES de qualquer persistencia).
            resultado = func()
            if resultado.nome_produto and "MLB_LENTO" in resultado.nome_produto:
                raise TimeoutError("timeout simulado no item lento")
            return resultado

        executar_ciclo(
            conn,
            config=_config_ficticia(),
            adapters={
                Site.MERCADO_LIVRE: adapter_ml,
                Site.AMAZON: adapter_amz,
            },
            agora=lambda: _AGORA_FIXO,
            executor_timeout=executor_timeout,
            remetente=_RemetenteFake(),
        )

        # Item lento: consulta tentada, mas timeout -> nenhum HistoricoPreco.
        assert adapter_ml.itens_preco == ["MLB_LENTO"]
        assert _contar_historico_preco(conn, ps_lento) == 0

        # Item rapido: processado normalmente apesar do timeout do anterior.
        assert adapter_amz.itens_preco == ["ASIN_RAPID0"]
        assert _contar_historico_preco(conn, ps_rapido) == 1
        ultimo = database.obter_ultimo_preco(conn, ps_rapido)
        assert ultimo is not None and ultimo.preco == _PRECO_NOVO
    finally:
        conn.close()


# ===========================================================================
# 3. Transicoes single-site <-> multi-site no agrupamento (R2.1, R2.2, R2.4, R2.6)
# ===========================================================================


def _rodar_ciclo_e_capturar(conn: sqlite3.Connection) -> list[str]:
    """Roda um ciclo com adapters/remetente fake e retorna as mensagens enviadas.

    Reseta o historico entre chamadas nao e necessario: cada ciclo apenas
    acrescenta um novo HistoricoPreco. Para reavaliar a mudanca em novo ciclo, o
    chamador semeia novamente o preco anterior antes de chamar.
    """
    remetente = _RemetenteFake()
    executar_ciclo(
        conn,
        config=_config_ficticia(),
        adapters={
            Site.MERCADO_LIVRE: _AdapterFake(Site.MERCADO_LIVRE),
            Site.AMAZON: _AdapterFake(Site.AMAZON),
        },
        agora=lambda: _AGORA_FIXO,
        executor_timeout=_executor_inline,
        remetente=remetente,
    )
    return remetente.mensagens


def test_single_site_sem_comparacao_entre_sites() -> None:
    """Single-site (produto_id NULL): notificacao NAO inclui comparacao (R2.1/R2.2).

    **Validates: Requirements 2.1, 2.2**
    """
    conn = database.inicializar_banco(":memory:")
    try:
        ps = _inserir_produto_site(conn, Site.MERCADO_LIVRE, "MLB_SOLO")
        _semear_preco_anterior(conn, ps)

        mensagens = _rodar_ciclo_e_capturar(conn)

        # Houve mudanca (100 -> 199.90) => exatamente uma notificacao.
        assert len(mensagens) == 1
        # Single-site: sem secao de comparacao entre sites.
        assert CABECALHO_COMPARACAO not in mensagens[0]
    finally:
        conn.close()


def test_multi_site_inclui_comparacao_entre_sites() -> None:
    """Multi-site (>=2 ProdutoSite no mesmo Produto): notificacao INCLUI comparacao (R2.4).

    **Validates: Requirements 2.4**
    """
    conn = database.inicializar_banco(":memory:")
    try:
        produto_id = _inserir_produto(conn, "Produto Agrupado")
        ps_ml = _inserir_produto_site(
            conn, Site.MERCADO_LIVRE, "MLB_GRP", produto_id=produto_id
        )
        ps_amz = _inserir_produto_site(
            conn, Site.AMAZON, "ASIN_GRP001", produto_id=produto_id
        )
        # Ambos os sites com preco anterior (para haver "ultimo preco" na
        # comparacao) e para disparar a mudanca em cada consulta.
        _semear_preco_anterior(conn, ps_ml)
        _semear_preco_anterior(conn, ps_amz)

        mensagens = _rodar_ciclo_e_capturar(conn)

        # Ambos mudaram => duas notificacoes, ambas com a comparacao entre sites.
        assert len(mensagens) == 2
        for msg in mensagens:
            assert CABECALHO_COMPARACAO in msg
    finally:
        conn.close()


def test_transicao_associar_e_desassociar_alterna_comparacao() -> None:
    """associar -> comparacao aparece; desassociar -> comparacao some (R2.4/R2.6).

    Comeca com dois ProdutoSite single-site (sem comparacao). Ao associa-los ao
    mesmo Produto, a comparacao passa a aparecer. Ao desassociar um deles (de
    volta a single-site), a comparacao desaparece novamente.

    **Validates: Requirements 2.4, 2.6**
    """
    conn = database.inicializar_banco(":memory:")
    try:
        ps_ml = _inserir_produto_site(conn, Site.MERCADO_LIVRE, "MLB_TRANS")
        ps_amz = _inserir_produto_site(conn, Site.AMAZON, "ASIN_TRANS1")

        # --- Fase 1: single-site (sem associacao) -> sem comparacao ---------
        _semear_preco_anterior(conn, ps_ml)
        _semear_preco_anterior(conn, ps_amz)
        mensagens_single = _rodar_ciclo_e_capturar(conn)
        assert len(mensagens_single) == 2
        for msg in mensagens_single:
            assert CABECALHO_COMPARACAO not in msg

        # --- Fase 2: associar ambos ao mesmo Produto -> comparacao aparece --
        produto_id = _inserir_produto(conn, "Produto Transicao")
        database.associar_produto(conn, ps_ml, produto_id)
        database.associar_produto(conn, ps_amz, produto_id)
        # Reset + re-semeia o preco anterior para reavaliar a mudanca neste ciclo.
        _resetar_e_semear_anterior(conn, ps_ml)
        _resetar_e_semear_anterior(conn, ps_amz)
        mensagens_multi = _rodar_ciclo_e_capturar(conn)
        assert len(mensagens_multi) == 2
        for msg in mensagens_multi:
            assert CABECALHO_COMPARACAO in msg

        # --- Fase 3: desassociar um deles -> volta a single-site (sem comparacao)
        database.desassociar_produto(conn, ps_amz)
        _resetar_e_semear_anterior(conn, ps_ml)
        _resetar_e_semear_anterior(conn, ps_amz)
        mensagens_volta = _rodar_ciclo_e_capturar(conn)
        assert len(mensagens_volta) == 2
        for msg in mensagens_volta:
            # Ambos agora tem < 2 irmaos agrupados => sem comparacao.
            assert CABECALHO_COMPARACAO not in msg
    finally:
        conn.close()
