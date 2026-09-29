"""Ingestão de itens em MASSA, com classificação estrutural por código oficial.

POR QUE ESTE MÓDULO EXISTE
--------------------------
O endpoint de itens do PNCP (`/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens`)
está levando **~23 segundos por licitação**, medido. Para 995 registros isso é
mais de 6 horas — inviável. E a busca por CNPJ no Compras.gov.br chegou a
estourar 90s de timeout.

O Compras.gov.br tem uma porta muito melhor:

    GET /modulo-contratacoes/2_consultarItensContratacoes_PNCP_14133
        ?dataInclusaoPncpInicial=&dataInclusaoPncpFinal=&pagina=&tamanhoPagina=500

Medido: **500 itens em 0,42 s**. Um mês inteiro (~30 mil itens) sai em ~57
requisições, menos de 30 segundos.

E o ganho não é só de velocidade. Os itens nesse formato trazem:

    codigoClasse   -> classe de MATERIAL (6640 microscópio, 6530 cardioversor)
    codigoGrupo    -> grupo de SERVIÇO (131 nuvem, 151 outsourcing impressão)

São **códigos oficiais da taxonomia do governo** — classificação estrutural,
não adivinhação por palavra no texto. É exatamente o que faltava: o PNCP não
fornece código algum (verificado), e por isso a classificação setorial teve de
ser por texto, com as limitações que isso implica.

ESTRATÉGIA
----------
Baixar os itens do Compras.gov.br por janela de data e indexar por
`idContratacaoPNCP` (que é o mesmo `numeroControlePNCP` da nossa base). Assim,
uma passada de 30 segundos cobre milhares de licitações de uma vez, em vez de
uma requisição lenta por licitação.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .collectors.pncp import PNCPCollector
from .store import connect, now_iso

URL_ITENS_LOTE = (
    "https://dadosabertos.compras.gov.br/modulo-contratacoes/"
    "2_consultarItensContratacoes_PNCP_14133"
)
URL_GRUPO_SERVICO = (
    "https://dadosabertos.compras.gov.br/modulo-servico/3_consultarGrupoServico"
)
URL_CLASSE_MATERIAL = (
    "https://dadosabertos.compras.gov.br/modulo-material/2_consultarClasseMaterial"
)
URL_GRUPO_MATERIAL = (
    "https://dadosabertos.compras.gov.br/modulo-material/1_consultarGrupoMaterial"
)


def _fmt(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _ingerir_lote(conn, col, d1: date, d2: date, *, verbose=None,
                  max_paginas: int | None = None) -> dict:
    """Baixa os itens de um intervalo e grava por idContratacaoPNCP."""
    novas = vistas = 0
    pagina = 1
    while True:
        if max_paginas and pagina > max_paginas:
            break
        try:
            resp = col.client.get_json(URL_ITENS_LOTE, {
                "dataInclusaoPncpInicial": _fmt(d1),
                "dataInclusaoPncpFinal": _fmt(d2),
                "pagina": pagina,
                "tamanhoPagina": 500,
            })
        except Exception as e:
            if verbose:
                verbose("erro", f"{_fmt(d1)}..{_fmt(d2)} p{pagina}: {e}")
            break
        if not resp:
            break
        dados = resp.get("resultado") or []
        if not dados:
            break
        for it in dados:
            novas += _gravar_item(conn, it)
            vistas += 1
        if verbose and pagina % 10 == 0:
            verbose("progresso", f"{_fmt(d1)}..{_fmt(d2)} p{pagina}: "
                                 f"{vistas}/{resp.get('totalRegistros')}")
        if len(dados) < 500:
            break
        pagina += 1
    return {"vistas": vistas, "gravadas": novas}


def _gravar_item(conn, it: dict) -> int:
    """Grava/atualiza um item, preservando campos que só o PNCP tem."""
    id_compra = it.get("idContratacaoPNCP")
    id_item = it.get("idCompraItem") or (
        f"{id_compra}-{it.get('numeroItemPncp')}" if id_compra else None
    )
    if not (id_compra and id_item):
        return 0
    desc = it.get("descricaoResumida") or it.get("descricaodetalhada")
    # Só grava se a contratação existe na base: sem isso encheríamos as tabelas
    # com dezenas de milhares de itens de licitações que não acompanhamos.
    if not conn.execute("SELECT 1 FROM oportunidades WHERE id = ?",
                        (id_compra,)).fetchone():
        return 0
    conn.execute(
        """INSERT INTO itens (id_item, id_compra, numero_item, descricao,
                              descricao_detalhada, material_ou_servico,
                              material_ou_servico_nome, quantidade,
                              unidade_medida, valor_unitario, valor_total,
                              codigo_classe, codigo_grupo, numero_grupo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id_item) DO UPDATE SET
             descricao = COALESCE(excluded.descricao, itens.descricao),
             material_ou_servico = COALESCE(excluded.material_ou_servico, itens.material_ou_servico),
             quantidade = COALESCE(excluded.quantidade, itens.quantidade),
             valor_unitario = COALESCE(excluded.valor_unitario, itens.valor_unitario),
             valor_total = COALESCE(excluded.valor_total, itens.valor_total),
             codigo_classe = COALESCE(excluded.codigo_classe, itens.codigo_classe),
             codigo_grupo = COALESCE(excluded.codigo_grupo, itens.codigo_grupo),
             numero_grupo = COALESCE(excluded.numero_grupo, itens.numero_grupo)""",
        (id_item, id_compra, it.get("numeroItemPncp"), desc, desc,
         it.get("materialOuServico"), it.get("materialOuServicoNome"),
         it.get("quantidade"), it.get("unidadeMedida"),
         it.get("valorUnitarioEstimado"), it.get("valorTotal"),
         it.get("codigoClasse"), it.get("codigoGrupo"), it.get("numeroGrupo")),
    )
    return 1


def ingerir_itens_lote(
    conn,
    *,
    inicio: date,
    fim: date,
    passo_dias: int = 31,
    col: PNCPCollector | None = None,
    max_paginas: int | None = None,
    verbose=None,
) -> dict:
    """Baixa itens do Compras.gov.br em janelas e grava o que casa com a base."""
    col = col or PNCPCollector(delay=0.3, use_cache=True)
    total_v = total_g = 0
    atual = inicio
    while atual <= fim:
        prox = min(atual + timedelta(days=passo_dias - 1), fim)
        r = _ingerir_lote(conn, col, atual, prox, verbose=verbose,
                          max_paginas=max_paginas)
        conn.commit()
        total_v += r["vistas"]
        total_g += r["gravadas"]
        if verbose:
            verbose("bloco", f"{_fmt(atual)}..{_fmt(prox)}: "
                             f"{r['vistas']} vistos, {r['gravadas']} gravados")
        atual = prox + timedelta(days=1)
    return {"vistos": total_v, "gravados": total_g}
