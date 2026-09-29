"""Camada de persistência (SQLite) — armazena oportunidades e histórico de matches.

SQLite é suficiente e correto aqui: o volume é de dezenas de milhares de
registros, o acesso é local, e não exige servidor. Migrar para Postgres só
quando houver multiusuário concorrente (fase SaaS).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Ambos os artefatos gerados ficam em ./data, na raiz do repo:
# cache HTTP e banco. Antes o cache ia para ./data (raiz) e o banco para
# <pacote>/data — inconsistente, e foi essa ambiguidade que causou a
# perda do banco numa reorganização de diretórios.
DB_PATH = Path(__file__).resolve().parent / "data" / "licitabot.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS oportunidades (
    id                TEXT PRIMARY KEY,      -- numeroControlePNCP
    fonte             TEXT NOT NULL,         -- pncp | compras_gov | bec_sp | ...

    -- órgão e localização
    orgao             TEXT,
    cnpj_orgao        TEXT,
    uf                TEXT,
    municipio         TEXT,
    codigo_ibge       TEXT,
    esfera            TEXT,                  -- F | E | M
    poder             TEXT,                  -- L | E | J | N (Legislativo/Executivo/Judiciário)
    unidade_nome      TEXT,                  -- ex.: SECRETARIA DE EDUCAÇÃO
    unidade_codigo    TEXT,
    orgao_subrogado   TEXT,
    unidade_subrogada TEXT,

    -- identificação da contratação
    modalidade        TEXT,
    modalidade_id     INTEGER,
    modo_disputa      TEXT,                  -- Aberto | Aberto-Fechado | Dispensa Com Disputa
    instrumento       TEXT,                  -- Edital | Aviso de Contratação Direta
    numero_compra     TEXT,
    processo          TEXT,                  -- nº do processo administrativo
    ano               INTEGER,
    sequencial        INTEGER,

    -- objeto e valores
    objeto            TEXT,
    informacao_complementar TEXT,
    valor_estimado    REAL,
    valor_homologado  REAL,
    srp               INTEGER,               -- 1 = registro de preços
    orcamento_sigiloso INTEGER,
    emenda_parlamentar TEXT,

    -- base legal
    amparo_legal      TEXT,                  -- ex.: "Lei 14.133/2021, Art. 75, I"
    amparo_legal_codigo INTEGER,
    amparo_legal_descricao TEXT,

    -- classificação e benefícios
    material_ou_servico TEXT,                -- S = serviço | M = material
    categoria_item    TEXT,
    tipo_beneficio    TEXT,                  -- ME/EPP, Agricultura familiar…
    margem_preferencia_normal INTEGER,
    margem_preferencia_adicional INTEGER,
    conteudo_nacional INTEGER,

    -- datas
    data_publicacao   TEXT,
    data_abertura     TEXT,
    data_encerramento TEXT,                  -- prazo fatal para propor
    data_inclusao     TEXT,
    data_atualizacao  TEXT,

    -- situação e resultado
    situacao          TEXT,
    situacao_id       INTEGER,
    existe_resultado  INTEGER,

    -- links e documentos
    link              TEXT,                  -- página no PNCP
    link_sistema_origem TEXT,                -- portal onde o edital realmente está
    link_processo     TEXT,                  -- processo eletrônico (SEI etc.)
    qtd_documentos    INTEGER,

    -- controle
    sistema_origem    TEXT,                  -- ex.: "Compras.gov.br", "Licitanet"
    usuario_nome      TEXT,                  -- quem publicou (geralmente o fornecedor do sistema)
    raw_json          TEXT,                  -- payload COMPLETO da listagem
    detalhe_json      TEXT,                  -- payload COMPLETO do /detalhe (38 campos)
    itens_json        TEXT,                  -- payload COMPLETO dos itens (36 campos cada)
    coletado_em       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_op_uf        ON oportunidades(uf);
CREATE INDEX IF NOT EXISTS ix_op_encerr    ON oportunidades(data_encerramento);
CREATE INDEX IF NOT EXISTS ix_op_public    ON oportunidades(data_publicacao);
CREATE INDEX IF NOT EXISTS ix_op_fonte     ON oportunidades(fonte);

CREATE TABLE IF NOT EXISTS itens (
    id_item      TEXT PRIMARY KEY,
    id_compra    TEXT NOT NULL,
    numero_item  INTEGER,
    descricao    TEXT,
    descricao_detalhada TEXT,
    material_ou_servico TEXT,                -- S | M
    material_ou_servico_nome TEXT,
    categoria    TEXT,
    categoria_id INTEGER,
    quantidade   REAL,
    unidade_medida TEXT,
    valor_unitario REAL,
    valor_total  REAL,
    criterio_julgamento TEXT,
    criterio_julgamento_id INTEGER,
    situacao     TEXT,
    situacao_id  INTEGER,
    tipo_beneficio TEXT,                     -- ME/EPP, agricultura familiar…
    tipo_beneficio_id INTEGER,
    incentivo_produtivo_basico INTEGER,
    margem_normal INTEGER,
    margem_adicional INTEGER,
    percentual_margem_normal REAL,
    percentual_margem_adicional REAL,
    tipo_margem_preferencia TEXT,
    conteudo_nacional INTEGER,
    orcamento_sigiloso INTEGER,
    tem_resultado INTEGER,
    ncm_nbs_codigo TEXT,
    ncm_nbs_descricao TEXT,
    catalogo     TEXT,
    catalogo_codigo_item TEXT,
    categoria_catalogo TEXT,
    patrimonio   TEXT,
    codigo_registro_imobiliario TEXT,
    informacao_complementar TEXT,
    data_inclusao TEXT,
    data_atualizacao TEXT,
    FOREIGN KEY (id_compra) REFERENCES oportunidades(id)
);

CREATE INDEX IF NOT EXISTS ix_itens_compra ON itens(id_compra);

CREATE TABLE IF NOT EXISTS matches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    oportunidade_id TEXT NOT NULL,
    perfil         TEXT NOT NULL,
    score          REAL NOT NULL,
    termos         TEXT,                      -- JSON: termos que dispararam
    calculado_em   TEXT NOT NULL,
    UNIQUE(oportunidade_id, perfil)
);

CREATE INDEX IF NOT EXISTS ix_match_score ON matches(score DESC);

CREATE TABLE IF NOT EXISTS coletas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fonte       TEXT NOT NULL,
    parametros  TEXT,
    registros   INTEGER,
    inicio      TEXT,
    fim         TEXT,
    status      TEXT,
    erro        TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Migrações incrementais: (tabela, coluna, definição).
# `CREATE TABLE IF NOT EXISTS` não altera tabela já existente, então evoluir o
# schema exige ALTER TABLE. SQLite não tem "ADD COLUMN IF NOT EXISTS", então
# verificamos via PRAGMA e aplicamos só o que falta. Rodar sempre é seguro.
MIGRACOES = [
    ("oportunidades", "material_ou_servico", "TEXT"),
    ("oportunidades", "categoria_item", "TEXT"),
    ("oportunidades", "link_sistema_origem", "TEXT"),
    ("oportunidades", "link_processo", "TEXT"),
    ("oportunidades", "qtd_documentos", "INTEGER"),
    # ampliação para "todos os dados informativos" do PNCP
    ("oportunidades", "codigo_ibge", "TEXT"),
    ("oportunidades", "poder", "TEXT"),
    ("oportunidades", "unidade_nome", "TEXT"),
    ("oportunidades", "unidade_codigo", "TEXT"),
    ("oportunidades", "orgao_subrogado", "TEXT"),
    ("oportunidades", "unidade_subrogada", "TEXT"),
    ("oportunidades", "modalidade_id", "INTEGER"),
    ("oportunidades", "modo_disputa", "TEXT"),
    ("oportunidades", "instrumento", "TEXT"),
    ("oportunidades", "processo", "TEXT"),
    ("oportunidades", "informacao_complementar", "TEXT"),
    ("oportunidades", "valor_homologado", "REAL"),
    ("oportunidades", "srp", "INTEGER"),
    ("oportunidades", "orcamento_sigiloso", "INTEGER"),
    ("oportunidades", "emenda_parlamentar", "TEXT"),
    ("oportunidades", "amparo_legal", "TEXT"),
    ("oportunidades", "amparo_legal_codigo", "INTEGER"),
    ("oportunidades", "amparo_legal_descricao", "TEXT"),
    ("oportunidades", "tipo_beneficio", "TEXT"),
    ("oportunidades", "margem_preferencia_normal", "INTEGER"),
    ("oportunidades", "margem_preferencia_adicional", "INTEGER"),
    ("oportunidades", "conteudo_nacional", "INTEGER"),
    ("oportunidades", "data_inclusao", "TEXT"),
    ("oportunidades", "data_atualizacao", "TEXT"),
    ("oportunidades", "situacao_id", "INTEGER"),
    ("oportunidades", "existe_resultado", "INTEGER"),
    ("oportunidades", "sistema_origem", "TEXT"),
    ("oportunidades", "usuario_nome", "TEXT"),
    ("oportunidades", "detalhe_json", "TEXT"),
    ("oportunidades", "itens_json", "TEXT"),
    # classificação setorial (rotina separada — ver setores.py)
    ("itens", "codigo_classe", "INTEGER"),
    ("itens", "codigo_grupo", "INTEGER"),
    ("itens", "numero_grupo", "INTEGER"),
    ("oportunidades", "setor", "TEXT"),
    ("oportunidades", "setor_confianca", "TEXT"),
    ("oportunidades", "setor_detalhe", "TEXT"),
]


def _migrar(conn: sqlite3.Connection) -> list[str]:
    aplicadas = []
    for tabela, coluna, definicao in MIGRACOES:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        except sqlite3.Error:
            continue
        if not cols or coluna in cols:
            continue
        conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")
        aplicadas.append(f"{tabela}.{coluna}")
    aplicadas += _migrar_itens(conn)
    return aplicadas


def _migrar_itens(conn: sqlite3.Connection) -> list[str]:
    """Recria a tabela `itens` quando ela tem o schema antigo e está vazia.

    A tabela de itens nasceu com 9 colunas e passou a ter 38. SQLite não tem
    ADD COLUMN em lote nem "IF NOT EXISTS" para colunas, e o ALTER individual
    de 29 colunas seria pior. Como a tabela estava vazia na prática, recriar é
    seguro — mas só fazemos isso se estiver realmente vazia, para nunca
    destruir dado coletado.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(itens)")}
    except sqlite3.Error:
        return []
    if not cols or "tipo_beneficio" in cols:
        return []
    n = conn.execute("SELECT COUNT(*) FROM itens").fetchone()[0]
    if n:
        # Tem dado: não destrói. Adiciona só as colunas que faltam.
        adicionadas = []
        for c in COLS_ITEM:
            if c in cols:
                continue
            tipo = "REAL" if c.startswith(("valor_", "percentual_", "quantidade")) else (
                "INTEGER" if c.endswith(("_id", "_normal", "_adicional")) or c in
                ("incentivo_produtivo_basico", "conteudo_nacional",
                 "orcamento_sigiloso", "tem_resultado") else "TEXT")
            conn.execute(f"ALTER TABLE itens ADD COLUMN {c} {tipo}")
            adicionadas.append(c)
        return [f"itens.{c}" for c in adicionadas]
    conn.executescript(
        "DROP TABLE itens;"
        + SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS itens"):SCHEMA.index("CREATE INDEX IF NOT EXISTS ix_itens_compra")]
    )
    return ["itens (recriada com 38 colunas)"]


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    aplicadas = _migrar(conn)
    if aplicadas:
        conn.commit()
    return conn


COLS_OPORTUNIDADE = [
    "id", "fonte",
    "orgao", "cnpj_orgao", "uf", "municipio", "codigo_ibge", "esfera", "poder",
    "unidade_nome", "unidade_codigo", "orgao_subrogado", "unidade_subrogada",
    "modalidade", "modalidade_id", "modo_disputa", "instrumento",
    "numero_compra", "processo", "ano", "sequencial",
    "objeto", "informacao_complementar", "valor_estimado", "valor_homologado",
    "srp", "orcamento_sigiloso", "emenda_parlamentar",
    "amparo_legal", "amparo_legal_codigo", "amparo_legal_descricao",
    "material_ou_servico", "categoria_item", "tipo_beneficio",
    "margem_preferencia_normal", "margem_preferencia_adicional", "conteudo_nacional",
    "data_publicacao", "data_abertura", "data_encerramento",
    "data_inclusao", "data_atualizacao",
    "situacao", "situacao_id", "existe_resultado",
    "link", "link_sistema_origem", "link_processo", "qtd_documentos",
    "sistema_origem", "usuario_nome",
    # classificação setorial — NÃO entram no upsert de coleta de propósito:
    # são calculadas pela rotina separada `classificar`. Se entrassem, cada
    # sync apagaria a classificação (o coletor não tem esse dado).
    "raw_json", "detalhe_json", "itens_json", "coletado_em",
]


def salvar_setores(conn: sqlite3.Connection, resultados: list[dict]) -> int:
    """Grava a classificação setorial.

    Separado de `upsert_oportunidade` de propósito: a coleta não conhece setor,
    então se a classificação fosse escrita pelo upsert, cada sync a zeraria.
    """
    n = 0
    for r in resultados:
        conn.execute(
            """UPDATE oportunidades
               SET setor = ?, setor_confianca = ?, setor_detalhe = ?
               WHERE id = ?""",
            (r.get("setor"), r.get("setor_confianca"),
             r.get("setor_detalhe"), r.get("id")),
        )
        n += 1
    return n


def _bool_int(v):
    """Normaliza booleano para 0/1 (SQLite não tem BOOLEAN)."""
    if v is None:
        return None
    return 1 if v else 0


def upsert_oportunidade(conn: sqlite3.Connection, op: dict) -> bool:
    """Insere ou atualiza uma oportunidade. Devolve True se era nova.

    Usa INSERT ... ON CONFLICT DO UPDATE (não INSERT OR REPLACE) para
    PRESERVAR campos já enriquecidos que vierem vazios numa recoleta — por
    exemplo `detalhe_json` ou `tipo_beneficio` quando a listagem não os traz.
    REPLACE apagaria a linha e perderia o enriquecimento.
    """
    cols = COLS_OPORTUNIDADE
    row = {c: op.get(c) for c in cols}
    row["coletado_em"] = op.get("coletado_em") or now_iso()

    for c in ("srp", "orcamento_sigiloso", "existe_resultado",
              "margem_preferencia_normal", "margem_preferencia_adicional",
              "conteudo_nacional"):
        row[c] = _bool_int(row.get(c))

    for c in ("raw_json", "detalhe_json", "itens_json"):
        if isinstance(row.get(c), (dict, list)):
            row[c] = json.dumps(row[c], ensure_ascii=False)

    existing = conn.execute(
        "SELECT 1 FROM oportunidades WHERE id = ?", (row["id"],)
    ).fetchone()

    placeholders = ", ".join(f":{c}" for c in cols)
    # COALESCE(excluded.x, oportunidades.x): só sobrescreve se o novo valor
    # existir de fato. Evita que uma recoleta mais pobre apague dado bom.
    updates = ", ".join(
        f"{c} = COALESCE(excluded.{c}, oportunidades.{c})" for c in cols if c != "id"
    )
    conn.execute(
        f"""INSERT INTO oportunidades ({', '.join(cols)}) VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {updates}""",
        row,
    )
    return existing is None


def save_matches(conn: sqlite3.Connection, perfil: str, results: list[dict]) -> int:
    n = 0
    for r in results:
        conn.execute(
            """INSERT INTO matches (oportunidade_id, perfil, score, termos, calculado_em)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(oportunidade_id, perfil) DO UPDATE SET
                 score = excluded.score,
                 termos = excluded.termos,
                 calculado_em = excluded.calculado_em""",
            (
                r["id"], perfil, r["score"],
                json.dumps(r.get("termos", []), ensure_ascii=False), now_iso(),
            ),
        )
        n += 1
    return n


COLS_ITEM = [
    "id_item", "id_compra", "numero_item", "descricao", "descricao_detalhada",
    "material_ou_servico", "material_ou_servico_nome", "categoria", "categoria_id",
    "quantidade", "unidade_medida", "valor_unitario", "valor_total",
    "criterio_julgamento", "criterio_julgamento_id", "situacao", "situacao_id",
    "tipo_beneficio", "tipo_beneficio_id", "incentivo_produtivo_basico",
    "margem_normal", "margem_adicional", "percentual_margem_normal",
    "percentual_margem_adicional", "tipo_margem_preferencia", "conteudo_nacional",
    "orcamento_sigiloso", "tem_resultado", "ncm_nbs_codigo", "ncm_nbs_descricao",
    "catalogo", "catalogo_codigo_item", "categoria_catalogo", "patrimonio",
    "codigo_registro_imobiliario", "informacao_complementar",
    "data_inclusao", "data_atualizacao",
    "codigo_classe", "codigo_grupo", "numero_grupo",
]

COLS_BOOL_ITEM = (
    "incentivo_produtivo_basico", "margem_normal", "margem_adicional",
    "conteudo_nacional", "orcamento_sigiloso", "tem_resultado",
)


def salvar_itens(conn: sqlite3.Connection, itens: list[dict]) -> int:
    """Grava os itens detalhados. Idempotente por id_item."""
    n = 0
    for it in itens:
        row = {c: it.get(c) for c in COLS_ITEM}
        if not row.get("id_item") or not row.get("id_compra"):
            continue
        for c in COLS_BOOL_ITEM:
            row[c] = _bool_int(row.get(c))
        placeholders = ", ".join(f":{c}" for c in COLS_ITEM)
        updates = ", ".join(
            f"{c} = COALESCE(excluded.{c}, itens.{c})" for c in COLS_ITEM if c != "id_item"
        )
        conn.execute(
            f"""INSERT INTO itens ({', '.join(COLS_ITEM)}) VALUES ({placeholders})
                ON CONFLICT(id_item) DO UPDATE SET {updates}""",
            row,
        )
        n += 1
    return n


def renormalizar_base(conn: sqlite3.Connection, *, limite: int | None = None) -> dict:
    """Re-extrai todas as colunas a partir do `raw_json` já guardado.

    Por que isso existe: quando o extrator ganha campos novos, os registros
    antigos ficam com colunas vazias — mesmo tendo o dado disponível no
    payload que já está no disco. Reprocessar do `raw_json` preenche tudo
    SEM nenhuma requisição de rede. Foi assim que `unidade_nome`,
    `codigo_ibge` e `poder` voltaram para 995 registros em segundos.

    (Os campos que só existem no /detalhe e nos /itens precisam de rede —
    para esses use `atualizar-dados`.)
    """
    from .collectors.pncp import PNCPCollector

    col = PNCPCollector(delay=0)
    sql = "SELECT id, raw_json FROM oportunidades WHERE raw_json IS NOT NULL"
    if limite:
        sql += f" LIMIT {int(limite)}"
    rows = conn.execute(sql).fetchall()

    ok = falhas = novos = 0
    for r in rows:
        try:
            item = json.loads(r["raw_json"])
        except (ValueError, TypeError):
            falhas += 1
            continue
        op = col.normalizar_consulta(item)
        if not op.get("id"):
            falhas += 1
            continue
        # preserva o que foi enriquecido por rede (não sobrescreve com NULL)
        atual = conn.execute(
            """SELECT material_ou_servico, categoria_item, tipo_beneficio,
                      detalhe_json, itens_json, qtd_documentos,
                      informacao_complementar, valor_homologado, processo,
                      amparo_legal, srp, existe_resultado
               FROM oportunidades WHERE id = ?""", (r["id"],)
        ).fetchone()
        if atual:
            for campo in atual.keys():
                if op.get(campo) in (None, "") and atual[campo] is not None:
                    op[campo] = atual[campo]
        upsert_oportunidade(conn, op)
        ok += 1
    conn.commit()
    return {"processados": len(rows), "ok": ok, "falhas": falhas}


def log_coleta(conn, fonte, parametros, registros, inicio, status="ok", erro=None):
    conn.execute(
        """INSERT INTO coletas (fonte, parametros, registros, inicio, fim, status, erro)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        # sort_keys é OBRIGATÓRIO: a comparação de blocos já coletados é feita
        # por igualdade de string JSON. Sem ordenar as chaves, dois dicts
        # idênticos com ordem de inserção diferente não batem — e o controle de
        # idempotência falha silenciosamente, recoletando tudo.
        (fonte,
         json.dumps(parametros, ensure_ascii=False, sort_keys=True) if parametros else None,
         registros, inicio, now_iso(), status, erro),
    )


def stats(conn) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "oportunidades": q("SELECT COUNT(*) FROM oportunidades"),
        "itens": q("SELECT COUNT(*) FROM itens"),
        "matches": q("SELECT COUNT(*) FROM matches"),
        "por_fonte": {
            r["fonte"]: r["n"]
            for r in conn.execute(
                "SELECT fonte, COUNT(*) n FROM oportunidades GROUP BY fonte ORDER BY n DESC"
            )
        },
        "por_uf": {
            r["uf"]: r["n"]
            for r in conn.execute(
                "SELECT uf, COUNT(*) n FROM oportunidades WHERE uf IS NOT NULL "
                "GROUP BY uf ORDER BY n DESC LIMIT 10"
            )
        },
    }
