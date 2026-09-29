"""Ingestão em massa — constrói uma base de dados de licitações independente de perfil.

Dois modos complementares, descobertos por teste ao vivo:

  1) BACKFILL (histórico)
     GET /v1/contratacoes/publicacao?dataInicial=&dataFinal=&codigoModalidadeContratacao=
     Aceita datas PASSADAS. Verificado: 31.543 editais em 1 mês de Pregão
     Eletrônico. Serve para construir a base histórica e para inteligência de
     mercado (quem compra o quê, a que preço, com que frequência).

  2) SNAPSHOT (janela aberta)
     GET /v1/contratacoes/proposta?dataInicial=&dataFinal=&codigoModalidadeContratacao=
     REJEITA dataFinal no passado (HTTP 422: "Data Final deve ser maior ou igual
     a data atual"), mas ACEITA dataInicial. Serve para capturar tudo que ainda
     está recebendo proposta — é o que alimenta os alertas de prazo.

Ambos paginam em blocos de 10 registros (tamanho de página fixo do PNCP), então
o volume real de requisições é alto: ~3.100 páginas por mês/modalidade. Por isso
a ingestão é:
  • IDEMPOTENTE — upsert por id; rodar 2x não duplica nem corrompe
  • RESUMÍVEL   — cada (data, modalidade) concluído vai para `coletas`;
                  re-executar pula o que já foi feito
  • TOLERANTE   — falha em uma modalidade não aborta as outras
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .collectors.pncp import PNCPCollector
from .store import _bool_int, log_coleta, now_iso, salvar_itens, upsert_oportunidade

# Todas as modalidades da Lei 14.133/2021. Uso todas por padrão: a base deve
# ser abrangente, não enviesada pelo que uma empresa específica procura.
MODALIDADES_TODAS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)

# Modalidades com volume relevante na prática (verificado: 5, 6 e 7 concentram
# a quase totalidade dos editais de bens e serviços comuns).
MODALIDADES_PRINCIPAIS = (6, 5, 7, 4, 8, 12, 3)


def _fmt(d: date) -> str:
    return d.strftime("%Y%m%d")


def _intervalos(inicio: date, fim: date, passo_dias: int = 31):
    """Divide um período em janelas. O PNCP responde melhor em janelas curtas."""
    atual = inicio
    while atual <= fim:
        prox = min(atual + timedelta(days=passo_dias - 1), fim)
        yield atual, prox
        atual = prox + timedelta(days=1)


def enriquecer_lote_base(
    conn,
    *,
    limite: int = 500,
    col: PNCPCollector | None = None,
    apenas_sem_tipo: bool = True,
    verbose=None,
) -> dict:
    """Percorre a BASE e preenche material-vs-serviço + categoria via /itens.

    Por que é um job separado: são 2 requisições por oportunidade (detalhe +
    itens). Fazer isso durante a coleta multiplicaria o tempo de ingestão por 3.
    Como job independente, dá para rodar em lotes ao longo do dia, retomando de
    onde parou — as oportunidades já classificadas não são reprocessadas.

    É este passo que habilita filtrar a base inteira por "só serviços" ou por
    categoria (TIC, obras, saúde…) sem depender de palavra-chave no objeto.
    """
    col = col or PNCPCollector()
    where = "WHERE material_ou_servico IS NULL" if apenas_sem_tipo else ""
    alvo = conn.execute(
        f"""SELECT id, cnpj_orgao, ano, sequencial FROM oportunidades {where}
            ORDER BY data_publicacao DESC LIMIT ?""",
        (limite,),
    ).fetchall()

    ok = falhas = 0
    itens_gravados = 0
    for i, row in enumerate(alvo, 1):
        op = dict(row)
        try:
            col.enriquecer(op)
        except Exception as e:
            falhas += 1
            if verbose and falhas <= 5:
                verbose("erro", f"{op['id']}: {e}")
            continue

        # grava TUDO que o enriquecimento trouxe, não só material/serviço
        import json as _json
        itens = []
        if op.get("itens_json"):
            try:
                itens = _json.loads(op["itens_json"])
            except (ValueError, TypeError):
                itens = []
        if itens:
            itens_gravados += salvar_itens(conn, itens)

        conn.execute(
            """UPDATE oportunidades SET
                 material_ou_servico = COALESCE(?, material_ou_servico),
                 categoria_item      = COALESCE(?, categoria_item),
                 tipo_beneficio      = COALESCE(?, tipo_beneficio),
                 margem_preferencia_normal    = COALESCE(?, margem_preferencia_normal),
                 margem_preferencia_adicional = COALESCE(?, margem_preferencia_adicional),
                 conteudo_nacional   = COALESCE(?, conteudo_nacional),
                 valor_homologado    = COALESCE(?, valor_homologado),
                 processo            = COALESCE(?, processo),
                 modo_disputa        = COALESCE(?, modo_disputa),
                 instrumento         = COALESCE(?, instrumento),
                 amparo_legal        = COALESCE(?, amparo_legal),
                 amparo_legal_codigo = COALESCE(?, amparo_legal_codigo),
                 srp                 = COALESCE(?, srp),
                 existe_resultado    = COALESCE(?, existe_resultado),
                 informacao_complementar = COALESCE(?, informacao_complementar),
                 detalhe_json        = COALESCE(?, detalhe_json),
                 itens_json          = COALESCE(?, itens_json),
                 data_encerramento   = COALESCE(?, data_encerramento),
                 data_abertura       = COALESCE(?, data_abertura),
                 valor_estimado      = COALESCE(?, valor_estimado),
                 link_sistema_origem = COALESCE(?, link_sistema_origem),
                 link_processo       = COALESCE(?, link_processo)
               WHERE id = ?""",
            (
                op.get("material_ou_servico"), op.get("categoria_item"),
                op.get("tipo_beneficio"),
                _bool_int(op.get("margem_preferencia_normal")),
                _bool_int(op.get("margem_preferencia_adicional")),
                _bool_int(op.get("conteudo_nacional")),
                op.get("valor_homologado"), op.get("processo"),
                op.get("modo_disputa"), op.get("instrumento"),
                op.get("amparo_legal"), op.get("amparo_legal_codigo"),
                _bool_int(op.get("srp")), _bool_int(op.get("existe_resultado")),
                op.get("informacao_complementar"),
                json.dumps(op["detalhe_json"], ensure_ascii=False)
                if isinstance(op.get("detalhe_json"), (dict, list)) else op.get("detalhe_json"),
                _json.dumps(itens, ensure_ascii=False) if itens else op.get("itens_json"),
                op.get("data_encerramento"), op.get("data_abertura"),
                op.get("valor_estimado"),
                op.get("link_sistema_origem"), op.get("link_processo"),
                op["id"],
            ),
        )
        ok += 1
        if i % 25 == 0:
            conn.commit()
            if verbose:
                verbose("progresso", f"{i}/{len(alvo)} ({ok} ok, {falhas} falhas, "
                                     f"{itens_gravados} itens)")
    conn.commit()
    return {"processados": len(alvo), "enriquecidos": ok, "falhas": falhas,
            "itens_gravados": itens_gravados}


def estatisticas_base(conn) -> dict:
    """Quantos registros ainda faltam enriquecer."""
    total = conn.execute("SELECT COUNT(*) FROM oportunidades").fetchone()[0]
    feitos = conn.execute(
        "SELECT COUNT(*) FROM oportunidades WHERE material_ou_servico IS NOT NULL"
    ).fetchone()[0]
    return {"total": total, "enriquecidos": feitos, "pendentes": total - feitos}


def ja_coletado(conn, fonte: str, parametros: dict) -> bool:
    """Verifica se este (intervalo, modalidade) já foi ingerido com sucesso."""
    import json
    alvo = json.dumps(parametros, ensure_ascii=False, sort_keys=True)
    row = conn.execute(
        "SELECT 1 FROM coletas WHERE fonte=? AND parametros=? AND status='ok' LIMIT 1",
        (fonte, alvo),
    ).fetchone()
    return row is not None


URL_PUBLICACAO = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
URL_PROPOSTA = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"


# O PNCP aceita tamanhoPagina até 50 (verificado: 500 devolve vazio).
# Antes eu assumia que era fixo em 10 — eram 983 requisições para varrer a
# janela aberta. Com 50 são 197: 5x menos carga e 5x mais rápido.
TAMANHO_PAGINA = 50


def _ingerir_modalidade(
    conn,
    col: PNCPCollector,
    *,
    inicio: date,
    fim: date,
    modalidade: int,
    endpoint: str,
    max_paginas: int | None,
    verbose,
) -> tuple[int, int]:
    """Percorre as páginas de um (intervalo, modalidade). Devolve (novas, vistas)."""
    url = URL_PUBLICACAO if endpoint == "publicacao" else URL_PROPOSTA

    novas = vistas = 0
    pagina = 1
    total_declarado = None
    while True:
        if max_paginas and pagina > max_paginas:
            break
        params = {
            "dataInicial": _fmt(inicio),
            "dataFinal": _fmt(fim),
            "codigoModalidadeContratacao": modalidade,
            "pagina": pagina,
            "tamanhoPagina": TAMANHO_PAGINA,
        }
        try:
            resp = col.client.get_json(url, params)
        except Exception as e:
            if verbose:
                verbose("erro", f"mod {modalidade} p{pagina}: {e}")
            break
        if not resp:
            break
        if total_declarado is None:
            total_declarado = resp.get("totalRegistros")
        dados = resp.get("data") or []
        if not dados:
            break
        for item in dados:
            op = col.normalizar_consulta(item)
            if not op.get("id"):
                continue
            vistas += 1
            if upsert_oportunidade(conn, op):
                novas += 1
        if verbose and pagina % 20 == 0:
            verbose("progresso", f"mod {modalidade} p{pagina}: {vistas}/{total_declarado}")
        if len(dados) < TAMANHO_PAGINA:
            break
        pagina += 1
    return novas, vistas


def backfill_publicacoes(
    conn,
    *,
    inicio: date,
    fim: date,
    modalidades=MODALIDADES_PRINCIPAIS,
    col: PNCPCollector | None = None,
    max_paginas: int | None = None,
    passo_dias: int = 31,
    pular_existentes: bool = True,
    verbose=None,
) -> dict:
    """Ingere publicações de um período histórico. Idempotente e resumível."""
    col = col or PNCPCollector()
    total_novas = total_vistas = 0
    mods_ok = 0

    for d1, d2 in _intervalos(inicio, fim, passo_dias):
        for mod in modalidades:
            params = {"inicio": _fmt(d1), "fim": _fmt(d2), "modalidade": mod,
                      "endpoint": "publicacao"}
            if pular_existentes and ja_coletado(conn, "pncp_backfill", params):
                if verbose:
                    verbose("pulado", f"{_fmt(d1)}..{_fmt(d2)} mod {mod} (já coletado)")
                continue
            t0 = now_iso()
            if verbose:
                verbose("inicio", f"{_fmt(d1)}..{_fmt(d2)} mod {mod}")
            try:
                novas, vistas = _ingerir_modalidade(
                    conn, col, inicio=d1, fim=d2, modalidade=mod,
                    endpoint="publicacao", max_paginas=max_paginas, verbose=verbose,
                )
                conn.commit()
                log_coleta(conn, "pncp_backfill", params, vistas, t0, "ok")
                conn.commit()
                total_novas += novas
                total_vistas += vistas
                mods_ok += 1
                if verbose:
                    verbose("fim", f"{_fmt(d1)}..{_fmt(d2)} mod {mod}: "
                                    f"{vistas} vistas, {novas} novas")
            except Exception as e:
                log_coleta(conn, "pncp_backfill", params, 0, t0, "erro", str(e)[:500])
                conn.commit()
                if verbose:
                    verbose("erro", f"{_fmt(d1)}..{_fmt(d2)} mod {mod}: {e}")

    return {"novas": total_novas, "vistas": total_vistas, "blocos_ok": mods_ok}


def snapshot_abertos(
    conn,
    *,
    dias: int = 15,
    modalidades=MODALIDADES_TODAS,
    col: PNCPCollector | None = None,
    max_paginas: int | None = None,
    verbose=None,
) -> dict:
    """Captura tudo que está recebendo proposta até `dias` à frente.

    É o snapshot que alimenta alertas de prazo. Re-executável: como o PNCP
    atualiza `dataEncerramentoProposta`, rodar de novo atualiza os prazos.
    """
    col = col or PNCPCollector()
    hoje = date.today()
    fim = hoje + timedelta(days=dias)
    total_novas = total_vistas = 0

    for mod in modalidades:
        params = {"dias": dias, "modalidade": mod, "endpoint": "proposta"}
        t0 = now_iso()
        if verbose:
            verbose("inicio", f"abertos mod {mod} até {_fmt(fim)}")
        try:
            novas, vistas = _ingerir_modalidade(
                conn, col, inicio=hoje, fim=fim, modalidade=mod,
                endpoint="proposta", max_paginas=max_paginas, verbose=verbose,
            )
            conn.commit()
            log_coleta(conn, "pncp_abertos", params, vistas, t0, "ok")
            conn.commit()
            total_novas += novas
            total_vistas += vistas
            if verbose:
                verbose("fim", f"abertos mod {mod}: {vistas} vistas, {novas} novas")
        except Exception as e:
            log_coleta(conn, "pncp_abertos", params, 0, t0, "erro", str(e)[:500])
            conn.commit()
            if verbose:
                verbose("erro", f"abertos mod {mod}: {e}")

    return {"novas": total_novas, "vistas": total_vistas}
