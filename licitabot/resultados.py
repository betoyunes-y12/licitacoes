"""Inteligência de resultados: quem ganhou, por quanto, e a que preço se registrou.

POR QUE ESTE MÓDULO É DIFERENTE DOS OUTROS
------------------------------------------
Os coletores existentes respondem "o que está sendo licitado". Este responde
"o que foi contratado, por quem, e a que preço" — que é a informação que
permite decidir se vale disputar.

Descoberto ao ler o swagger completo do PNCP (`/api/consulta/v3/api-docs`),
que tem 12 endpoints — eu conhecia 5. Os quatro novos:

    /v1/contratacoes/atualizacao  Contratações por data de ATUALIZAÇÃO GLOBAL
    /v1/contratos                 Contratos por data de publicação
    /v1/atas                      Atas de registro de preço por vigência
    /v1/pca                       Plano de contratações anual

O CAMPO QUE LIGA TUDO
---------------------
`/v1/contratos` traz 41 campos, e entre eles:

    numeroControlePncpCompra        -> liga DIRETO ao edital da nossa base
    niFornecedor / nomeRazaoSocial  -> quem ganhou
    valorGlobal / valorParcela      -> por quanto, e em quantas parcelas
    dataVigenciaInicio/Fim          -> por quanto tempo
    categoriaProcesso               -> setor oficial (Compras/Serviços/Obras)
    orgaoEntidade / unidadeOrgao    -> quem comprou

Com isso dá para responder, para qualquer licitação encerrada:
    "quem ganhou, por quanto, e qual a vigência do contrato"

E o inverso, que é o mais valioso comercialmente:
    "este fornecedor já ganhou o quê, de quem, e por quanto"

As atas (`/v1/atas`, 509 mil registros numa semana) trazem preços registrados,
que servem de referência para precificar proposta.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .collectors.pncp import PNCPCollector
from .store import connect, now_iso

BASE = "https://pncp.gov.br/api/consulta/v1"
TAMANHO_PAGINA = 50


def _fmt(d: date) -> str:
    return d.strftime("%Y%m%d")


def coletar_contratos(
    conn,
    *,
    inicio: date,
    fim: date,
    passo_dias: int = 7,
    col: PNCPCollector | None = None,
    apenas_da_base: bool = True,
    max_paginas: int | None = None,
    verbose=None,
) -> dict:
    """Coleta contratos publicados no período.

    `apenas_da_base=True` grava só os contratos cujo edital está na nossa base —
    evita encher o banco com dezenas de milhares de contratos de licitações que
    não acompanhamos. Use False para construir base de concorrência ampla.
    """
    col = col or PNCPCollector(delay=0.4, use_cache=True)
    conn.execute("""CREATE TABLE IF NOT EXISTS contratos (
        numero_controle     TEXT PRIMARY KEY,
        edital_id           TEXT,
        orgao               TEXT,
        cnpj_orgao          TEXT,
        uf                  TEXT,
        municipio           TEXT,
        fornecedor          TEXT,
        ni_fornecedor       TEXT,
        tipo_pessoa         TEXT,
        categoria           TEXT,
        objeto              TEXT,
        processo            TEXT,
        valor_inicial       REAL,
        valor_global        REAL,
        valor_parcela       REAL,
        numero_parcelas     INTEGER,
        data_assinatura     TEXT,
        vigencia_inicio     TEXT,
        vigencia_fim        TEXT,
        data_publicacao     TEXT,
        raw_json            TEXT,
        coletado_em         TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ct_forn ON contratos(ni_fornecedor)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ct_edital ON contratos(edital_id)")

    total = gravados = 0
    atual = inicio
    while atual <= fim:
        prox = min(atual + timedelta(days=passo_dias - 1), fim)
        pagina = 1
        while True:
            if max_paginas and pagina > max_paginas:
                break
            try:
                resp = col.client.get_json(f"{BASE}/contratos", {
                    "dataInicial": _fmt(atual), "dataFinal": _fmt(prox),
                    "pagina": pagina, "tamanhoPagina": TAMANHO_PAGINA,
                })
            except Exception as e:
                if verbose:
                    verbose("erro", f"contratos {_fmt(atual)}..{_fmt(prox)} p{pagina}: {e}")
                break
            if not resp:
                break
            dados = resp.get("data") or []
            if not dados:
                break
            for c in dados:
                total += 1
                if _gravar_contrato(conn, c, apenas_da_base=apenas_da_base):
                    gravados += 1
            if verbose:
                verbose("progresso", f"contratos {_fmt(atual)}..{_fmt(prox)} "
                                     f"p{pagina}: {total} vistos, {gravados} gravados")
            if len(dados) < TAMANHO_PAGINA:
                break
            pagina += 1
        conn.commit()
        atual = prox + timedelta(days=1)

    if verbose:
        verbose("fim", f"contratos: {total} vistos, {gravados} gravados")
    return {"vistos": total, "gravados": gravados}


def _gravar_contrato(conn, c: dict, *, apenas_da_base: bool) -> bool:
    edital = c.get("numeroControlePncpCompra")
    if apenas_da_base and edital:
        if not conn.execute("SELECT 1 FROM oportunidades WHERE id = ?",
                            (edital,)).fetchone():
            return False
    elif apenas_da_base and not edital:
        return False

    org = c.get("orgaoEntidade") or {}
    uni = c.get("unidadeOrgao") or {}
    cat = c.get("categoriaProcesso") or {}
    conn.execute(
        """INSERT INTO contratos (numero_controle, edital_id, orgao, cnpj_orgao,
               uf, municipio, fornecedor, ni_fornecedor, tipo_pessoa, categoria,
               objeto, processo, valor_inicial, valor_global, valor_parcela,
               numero_parcelas, data_assinatura, vigencia_inicio, vigencia_fim,
               data_publicacao, raw_json, coletado_em)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(numero_controle) DO UPDATE SET
             fornecedor = COALESCE(excluded.fornecedor, contratos.fornecedor),
             valor_global = COALESCE(excluded.valor_global, contratos.valor_global),
             vigencia_fim = COALESCE(excluded.vigencia_fim, contratos.vigencia_fim)""",
        (c.get("numeroControlePNCP"), edital, org.get("razaoSocial"),
         org.get("cnpj"), uni.get("ufSigla"), uni.get("municipioNome"),
         c.get("nomeRazaoSocialFornecedor"), c.get("niFornecedor"),
         c.get("tipoPessoa"), cat.get("nome"), c.get("objetoContrato"),
         c.get("processo"), c.get("valorInicial"), c.get("valorGlobal"),
         c.get("valorParcela"), c.get("numeroParcelas"), c.get("dataAssinatura"),
         c.get("dataVigenciaInicio"), c.get("dataVigenciaFim"),
         c.get("dataPublicacaoPncp"), json.dumps(c, ensure_ascii=False), now_iso()),
    )
    return True


def vencedores_de(conn, edital_id: str) -> list[dict]:
    """Quem ganhou uma licitação específica."""
    return [dict(r) for r in conn.execute(
        """SELECT fornecedor, ni_fornecedor, valor_global, numero_parcelas,
                  vigencia_inicio, vigencia_fim, objeto
           FROM contratos WHERE edital_id = ? ORDER BY valor_global DESC""",
        (edital_id,))]


def historico_fornecedor(conn, ni: str, limite: int = 50) -> dict:
    """O que este fornecedor já ganhou — base para análise de concorrência."""
    rows = [dict(r) for r in conn.execute(
        """SELECT orgao, uf, objeto, valor_global, vigencia_inicio
           FROM contratos WHERE ni_fornecedor = ? ORDER BY valor_global DESC
           LIMIT ?""", (ni, limite))]
    total = sum(r["valor_global"] or 0 for r in rows)
    return {"contratos": rows, "quantidade": len(rows), "valor_total": total}


def ingerir_contratos_da_base(
    conn,
    *,
    inicio: date,
    fim: date,
    limite_orgaos: int | None = None,
    col: PNCPCollector | None = None,
    verbose=None,
) -> dict:
    """Busca contratos por ÓRGÃO da nossa base — eficiente e direcionado.

    Por que assim, e não varrendo por data: uma semana de contratos no PNCP tem
    31 mil registros, e a taxa de casamento com a nossa base é de ~0,2%. Buscar
    por `cnpjOrgao` inverte isso: em vez de ler 31 mil para achar 60, lemos
    apenas os contratos dos órgãos que já acompanhamos.

    Verificado: `cnpjOrgao` funciona (862 contratos para um único órgão).
    """
    col = col or PNCPCollector(delay=0.4, use_cache=True)
    # cria a tabela via coletar_contratos (idempotente)
    coletar_contratos(conn, inicio=inicio, fim=inicio, col=col, max_paginas=0)

    orgaos = [r[0] for r in conn.execute(
        """SELECT DISTINCT cnpj_orgao FROM oportunidades
           WHERE cnpj_orgao IS NOT NULL"""
        + (f" LIMIT {int(limite_orgaos)}" if limite_orgaos else ""))]

    total = gravados = 0
    LIMITE_DIAS = 365   # o PNCP rejeita período maior: "Período maior que 365 dias"
    for i, cnpj in enumerate(orgaos, 1):
        atual = inicio
        while atual <= fim:
            prox = min(atual + timedelta(days=LIMITE_DIAS - 1), fim)
            pagina = 1
            while True:
                try:
                    resp = col.client.get_json(f"{BASE}/contratos", {
                        "dataInicial": _fmt(atual), "dataFinal": _fmt(prox),
                        "cnpjOrgao": cnpj, "pagina": pagina,
                        "tamanhoPagina": TAMANHO_PAGINA,
                    })
                except Exception as e:
                    if verbose:
                        verbose("erro", f"{cnpj} {_fmt(atual)}..{_fmt(prox)}: {e}")
                    break
                if not resp:
                    break
                dados = resp.get("data") or []
                if not dados:
                    break
                for c in dados:
                    total += 1
                    # aqui gravamos TODOS: o filtro é o próprio órgão
                    if _gravar_contrato(conn, c, apenas_da_base=False):
                        gravados += 1
                if len(dados) < TAMANHO_PAGINA:
                    break
                pagina += 1
            atual = prox + timedelta(days=1)
        conn.commit()
        if verbose and i % 20 == 0:
            verbose("progresso", f"{i}/{len(orgaos)} órgãos | {gravados} contratos")

    if verbose:
        verbose("fim", f"{len(orgaos)} órgãos | {total} vistos, {gravados} gravados")
    return {"orgaos": len(orgaos), "vistos": total, "gravados": gravados}


def estatisticas(conn) -> dict:
    q = lambda s, *a: conn.execute(s, *([a] if a else [])).fetchone()[0]
    return {
        "contratos": q("SELECT COUNT(*) FROM contratos"),
        "com_edital_na_base": q(
            "SELECT COUNT(*) FROM contratos WHERE edital_id IN "
            "(SELECT id FROM oportunidades)"),
        "fornecedores_distintos": q(
            "SELECT COUNT(DISTINCT ni_fornecedor) FROM contratos"),
        "valor_total": q("SELECT SUM(valor_global) FROM contratos") or 0,
        "top_fornecedores": [
            {"fornecedor": r[0], "ni": r[1], "contratos": r[2], "valor": r[3]}
            for r in conn.execute(
                """SELECT fornecedor, ni_fornecedor, COUNT(*) n, SUM(valor_global) v
                   FROM contratos WHERE fornecedor IS NOT NULL
                   GROUP BY ni_fornecedor ORDER BY v DESC LIMIT 15""")
        ],
    }
