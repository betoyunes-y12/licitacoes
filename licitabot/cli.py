#!/usr/bin/env python3
"""licitabot — CLI do agente de varredura de licitações.

Uso:
    python -m licitabot.cli varredura --perfil config/empresa.json --dias 7
    python -m licitabot.cli buscar --perfil config/empresa.json --termo "software"
    python -m licitabot.cli fontes
    python -m licitabot.cli status
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .collectors import PNCPCollector
from .collectors.base import CATALOGO
from .export import consultar, exportar_csv, exportar_jsonl, resumo_base
from .matching import Perfil, deduplicar_similares, ranquear
from .store import (
    connect,
    log_coleta,
    now_iso,
    save_matches,
    stats,
    upsert_oportunidade,
)

RAIZ = Path(__file__).resolve().parent


def _log(tipo: str, msg: str) -> None:
    if tipo == "erro":
        print(f"  ! {msg}", file=sys.stderr)
    else:
        print(f"  · {msg}", file=sys.stderr)


def cmd_varredura(args) -> int:
    perfil = Perfil.carregar(args.perfil)
    conn = connect(args.db)
    col = PNCPCollector(delay=args.delay, use_cache=not args.no_cache)

    inicio = now_iso()
    coletadas: list[dict] = []

    # Estratégia A: busca textual por palavras-chave fortes (preciso)
    termos = perfil.termos_busca(limite=args.max_termos)
    print(f"[1/2] Busca textual por {len(termos)} termos "
          f"em {len(perfil.ufs) or 'todas as'} UF(s)...", file=sys.stderr)
    coletadas += col.coletar_por_termos(
        termos,
        ufs=sorted(perfil.ufs) or None,
        max_paginas=args.paginas,
        somente_abertos=not args.incluir_fechados,
        callback=_log,
    )
    print(f"      -> {len(coletadas)} oportunidades (busca textual)", file=sys.stderr)

    # Estratégia B: varredura por prazo (abrangente, pega o que o termo não pegou)
    if args.dias > 0:
        print(f"[2/2] Varredura por prazo (encerrando em {args.dias} dias)...", file=sys.stderr)
        antes = len(coletadas)
        por_prazo = col.coletar_por_prazo(
            dias=args.dias, max_paginas=args.max_paginas_prazo, callback=_log
        )
        ids = {c["id"] for c in coletadas}
        coletadas += [o for o in por_prazo if o["id"] not in ids]
        print(f"      -> +{len(coletadas) - antes} oportunidades (por prazo)", file=sys.stderr)

    if not coletadas:
        print("Nenhuma oportunidade coletada.", file=sys.stderr)
        return 1

    # Enriquecimento ANTES do ranqueamento final: o detalhe + itens definem
    # material-vs-serviço e o prazo, e ambos mudam o score. Enriquecer depois
    # ranquear seria ranquear com dado incompleto.
    if not args.sem_enriquecer:
        pre = deduplicar_similares(ranquear(coletadas, perfil), limiar=0.95)
        alvo = pre[: args.max_enriquecer]
        print(f"[+] Enriquecendo {len(alvo)} pré-selecionadas (prazo/valor/itens)...",
              file=sys.stderr)
        col.enriquecer_lote(alvo, callback=_log)
        por_id = {o["id"]: o for o in alvo}
        coletadas = [por_id.get(o["id"], o) for o in coletadas]

    # Persistência
    novas = 0
    for op in coletadas:
        if upsert_oportunidade(conn, op):
            novas += 1
    log_coleta(conn, "pncp", {"termos": termos, "dias": args.dias}, len(coletadas), inicio)
    conn.commit()

    # Ranqueamento final com dados completos
    ranqueadas = deduplicar_similares(ranquear(coletadas, perfil), limiar=0.95)
    save_matches(conn, perfil.nome, ranqueadas)
    conn.commit()

    _imprimir(ranqueadas, perfil, args.limite)
    print(f"\nResumo: {len(coletadas)} coletadas | {novas} novas no banco | "
          f"{len(ranqueadas)} aderentes (score >= {perfil.score_minimo})", file=sys.stderr)
    print(f"Requisições: {col.client.stats}", file=sys.stderr)
    return 0


def cmd_buscar(args) -> int:
    perfil = Perfil.carregar(args.perfil)
    col = PNCPCollector(delay=args.delay, use_cache=not args.no_cache)
    resp = col.buscar_editais(
        args.termo,
        uf=args.uf,
        todos_status=not args.somente_abertos,
        tamanho_pagina=args.limite,
    )
    ops = [col.normalizar_busca(i) for i in (resp.get("items") or [])]
    ranqueadas = ranquear(ops, perfil)
    _imprimir(ranqueadas, perfil, args.limite)
    print(f"\nTotal no portal para '{args.termo}': {resp.get('total')}", file=sys.stderr)
    return 0


def _dias_restantes(data_iso: str | None):
    """Dias restantes até o prazo, em HORAS DECIMAIS.

    O PNCP devolve datas sem fuso, no horário de Brasília. Tratar como UTC
    errava por 3 horas e mostrava "-1 dia" para editais que ainda estavam
    abertos no mesmo dia. Aqui interpretamos as datas como horário de Brasília.
    """
    if not data_iso:
        return None
    try:
        from datetime import datetime, timedelta, timezone
        fuso_br = timezone(timedelta(hours=-3))
        dt = datetime.fromisoformat(str(data_iso).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=fuso_br)
        delta = dt - datetime.now(fuso_br)
        return delta.total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def _imprimir(ops: list[dict], perfil: Perfil, limite: int) -> None:
    if not ops:
        print("Nenhuma oportunidade aderente encontrada.")
        return
    for i, op in enumerate(ops[:limite], 1):
        dias = _dias_restantes(op.get("data_encerramento"))
        urgente = dias is not None and dias <= 3
        marca = "URGENTE" if urgente else ("ALERTA" if op.get("alerta") else "      ")
        print(f"\n[{i}] {marca} score={op['score']}")
        print(f"    Órgão : {op.get('orgao')}  ({op.get('uf')} / {op.get('municipio')})")
        print(f"    Objeto: {(op.get('objeto') or '')[:220]}")
        if dias is not None:
            if dias < 0:
                prazo = f"ENCERRADO há {abs(dias):.1f} dia(s)"
            elif dias < 1:
                prazo = f"HOJE — faltam {dias*24:.1f} hora(s)"
            else:
                prazo = f"{dias:.1f} dia(s)"
            print(f"    Prazo : {op['data_encerramento']}  ({prazo})")
        elif op.get("data_encerramento"):
            print(f"    Prazo : {op['data_encerramento']}")
        if op.get("valor_estimado"):
            print(f"    Valor : R$ {op['valor_estimado']:,.2f}")
        if op.get("modalidade"):
            print(f"    Modal.: {op['modalidade']}")
        if op.get("termos"):
            print(f"    Termos: {', '.join(op['termos'][:8])}")
        print(f"    Link  : {op.get('link')}")


def cmd_enriquecer(args) -> int:
    """Preenche material-vs-serviço e categoria na base (job em lotes)."""
    from .ingest import enriquecer_lote_base, estatisticas_base

    conn = connect(args.db)
    col = PNCPCollector(delay=args.delay, use_cache=not args.no_cache)

    antes = estatisticas_base(conn)
    print(f"Base: {antes['total']:,} registros | {antes['enriquecidos']:,} já enriquecidos "
          f"| {antes['pendentes']:,} pendentes")
    if antes["pendentes"] == 0:
        print("Nada a fazer — base totalmente enriquecida.")
        return 0

    r = enriquecer_lote_base(conn, limite=args.limite, col=col, verbose=_log)
    depois = estatisticas_base(conn)
    print(f"\nProcessados {r['processados']} | enriquecidos {r['enriquecidos']} "
          f"| falhas {r['falhas']}")
    print(f"Pendentes agora: {depois['pendentes']:,}")
    if depois["pendentes"]:
        est = max(1, int(depois["pendentes"] * args.delay * 2 / 60))
        print(f"Estimativa para concluir com --delay {args.delay}: ~{est} min "
              f"(rode novamente para continuar — é retomável)")
    return 0


def cmd_baixar(args) -> int:
    """Baixa os documentos (edital, TR, minuta) das licitações."""
    from .documentos import baixar_lote

    tipos = [t.strip() for t in args.tipos.split(",")] if args.tipos else None
    print(f"Baixando documentos de até {args.limite} licitações "
          f"(prazo mais próximo primeiro)...")
    r = baixar_lote(
        db=args.db, raiz=args.pasta, limite=args.limite, uf=args.uf,
        texto=args.texto, apenas_abertos=not args.todas,
        tipos=tipos, delay=args.delay, verbose=_log,
    )
    print(f"\n{'='*62}")
    print(f"  Licitações processadas : {r['oportunidades']}")
    print(f"  Com documento anexado  : {r['com_documento']}")
    print(f"  Sem documento no PNCP  : {r['sem_documento']}")
    print(f"  Arquivos encontrados   : {r['documentos_encontrados']}")
    print(f"  Arquivos baixados      : {r['arquivos_baixados']}")
    print(f"  Pasta                  : {r['pasta_raiz']}")
    print(f"{'='*62}")
    if r["sem_documento"]:
        print("\nObs.: contratações sem anexo no PNCP normalmente têm o edital no")
        print("portal do órgão. Use `links` para obter essas URLs.")
    return 0


def cmd_renormalizar(args) -> int:
    """Re-extrai colunas a partir do raw_json guardado — sem tocar na rede."""
    from .store import renormalizar_base

    conn = connect(args.db)
    antes = {
        c: conn.execute(
            f"SELECT COUNT(*) FROM oportunidades WHERE {c} IS NOT NULL"
        ).fetchone()[0]
        for c in ("unidade_nome", "codigo_ibge", "poder", "processo", "esfera")
    }
    r = renormalizar_base(conn, limite=args.limite)
    depois = {
        c: conn.execute(
            f"SELECT COUNT(*) FROM oportunidades WHERE {c} IS NOT NULL"
        ).fetchone()[0]
        for c in antes
    }
    print(f"Processados {r['processados']} registros ({r['falhas']} falhas) — "
          f"0 requisições de rede")
    print(f"\n{'campo':18} {'antes':>7} {'depois':>7}")
    for c in antes:
        marca = "  ←" if depois[c] > antes[c] else ""
        print(f"{c:18} {antes[c]:>7} {depois[c]:>7}{marca}")
    print("\nObs.: campos que só existem no /detalhe e nos /itens (tipo_beneficio,")
    print("valor_homologado, amparo_legal…) exigem rede — use `atualizar-dados`.")
    return 0


def cmd_atualizar_dados(args) -> int:
    """Rebaixa os payloads completos e re-extrai TODOS os campos informativos.

    Serve para duas situações:
      1. A base foi coletada por uma versão anterior que não extraía todos os
         campos (ex.: sem processo, amparo legal, tipo de benefício).
      2. O PNCP atualizou o edital — prazo prorrogado, valor alterado,
         resultado publicado. Aqui a atualização é o objetivo, não um efeito
         colateral.
    """
    from .collectors.pncp import PNCPCollector
    from .store import salvar_itens, upsert_oportunidade

    conn = connect(args.db)
    col = PNCPCollector(delay=args.delay, use_cache=not args.no_cache)

    where, params = ["cnpj_orgao IS NOT NULL", "ano IS NOT NULL", "sequencial IS NOT NULL"], []
    if args.uf:
        where.append("uf = ?"); params.append(args.uf.upper())
    if args.abertos:
        from .export import agora_brt
        where.append("data_encerramento >= ?"); params.append(agora_brt())
    if args.texto:
        where.append("objeto LIKE ?"); params.append(f"%{args.texto}%")

    alvo = conn.execute(
        f"SELECT * FROM oportunidades WHERE {' AND '.join(where)} "
        f"ORDER BY data_encerramento ASC LIMIT ?", params + [args.limite]
    ).fetchall()

    print(f"Re-extraindo dados de {len(alvo)} licitações "
          f"(detalhe + itens: 2 requisições cada)...")
    ok = falhas = itens_total = atualizadas = 0
    for i, row in enumerate(alvo, 1):
        op = dict(row)
        try:
            col.enriquecer(op)
            itens = json.loads(op.get("itens_json") or "[]")
        except Exception as e:
            falhas += 1
            continue

        antes = row["data_encerramento"]
        if upsert_oportunidade(conn, op):
            pass  # não deveria acontecer: já existe
        if itens:
            itens_total += salvar_itens(conn, itens)
        if antes != op.get("data_encerramento"):
            atualizadas += 1
        ok += 1
        if i % 20 == 0:
            conn.commit()
            print(f"  {i}/{len(alvo)} | itens: {itens_total} | prazos alterados: {atualizadas}")
    conn.commit()
    print(f"\nProcessadas {ok} | falhas {falhas} | itens gravados {itens_total} "
          f"| prazos alterados {atualizadas}")
    return 0


def cmd_classificar(args) -> int:
    """Classifica setorialmente a base — rotina separada da coleta."""
    from .setores import setores_disponiveis
    from .setores_job import VERSAO_REGRAS, classificar_base, setores_na_base

    if args.listar_setores:
        print(f"Setores disponíveis (regras v{VERSAO_REGRAS}):\n")
        for k, nome in setores_disponiveis():
            print(f"  {k:18} {nome}")
        print(f"  {'misto':18} Licitação com itens de vários setores (baixa confiança)")
        print(f"  {'indefinido':18} Nenhum item com evidência sólida")
        return 0

    conn = connect(args.db)
    col = PNCPCollector(delay=args.delay, use_cache=not args.no_cache) \
        if args.buscar_itens else None

    r = classificar_base(
        conn, limite=args.limite,
        apenas_sem_setor=args.apenas_sem_setor,
        reclassificar_abaixo_de=args.reclassificar_abaixo_de,
        buscar_itens_faltantes=args.buscar_itens, col=col, verbose=_log,
    )

    print(f"\nClassificados: {r['gravados']} de {r['total']} "
          f"(regras v{VERSAO_REGRAS})")
    if r["itens_buscados"]:
        print(f"  itens buscados no PNCP: {r['itens_buscados']}")
    print(f"  sem itens publicados  : {r['sem_itens']} "
          f"(classificados só pelo objeto)")
    print(f"\nPor setor:")
    for k, v in sorted(r["por_setor"].items(), key=lambda x: -x[1]):
        print(f"  {k:20} {v:>6}")
    print(f"\nPor confiança:")
    for k, v in sorted(r["por_confianca"].items(), key=lambda x: -x[1]):
        print(f"  {k:20} {v:>6}")
    return 0


def cmd_setores(args) -> int:
    """Panorama da classificação setorial da base."""
    from .setores_job import setores_na_base

    conn = connect(args.db)
    d = setores_na_base(conn)
    print(f"\n{'='*58}\n  CLASSIFICAÇÃO SETORIAL DA BASE\n{'='*58}")
    print("\n  Por setor:")
    for k, v in d["por_setor"].items():
        barra = "#" * min(40, v // 5)
        print(f"    {k:20} {v:>6}  {barra}")
    print("\n  Por confiança:")
    for k, v in d["por_confianca"].items():
        print(f"    {k:20} {v:>6}")
    print(f"{'='*58}\n")
    return 0


def cmd_contratos(args) -> int:
    """Inteligência de resultados: quem ganhou, por quanto."""
    from .resultados import estatisticas, historico_fornecedor, vencedores_de

    conn = connect(args.db)
    if args.fornecedor:
        h = historico_fornecedor(conn, args.fornecedor)
        print(f"\nFornecedor {args.fornecedor}: {h['quantidade']} contratos, "
              f"R$ {h['valor_total']:,.2f}")
        for c in h["contratos"][:30]:
            print(f"  R$ {c['valor_global'] or 0:>16,.2f} | {c['uf']} | "
                  f"{str(c['orgao'])[:32]}")
            print(f"      {' '.join(str(c['objeto'] or '').split())[:100]}")
        return 0

    if args.edital:
        v = vencedores_de(conn, args.edital)
        if not v:
            print(f"Nenhum contrato registrado para o edital {args.edital}.")
            print("(Comum: a base acompanha licitações abertas, que ainda não têm contrato.)")
            return 0
        for x in v:
            print(f"  {x['fornecedor']} (CNPJ {x['ni_fornecedor']})")
            print(f"    R$ {x['valor_global'] or 0:,.2f} em {x['numero_parcelas']} parcela(s)")
            print(f"    vigência {x['vigencia_inicio']} a {x['vigencia_fim']}")
        return 0

    e = estatisticas(conn)
    print(f"\n{'='*70}\n  INTELIGÊNCIA DE RESULTADOS\n{'='*70}")
    print(f"  Contratos            : {e['contratos']:,}")
    print(f"  Fornecedores únicos  : {e['fornecedores_distintos']:,}")
    print(f"  Valor total          : R$ {e['valor_total']:,.2f}")
    print(f"  Ligados à nossa base : {e['com_edital_na_base']:,}")
    print("\n  Por categoria oficial (setor do PNCP):")
    for r in conn.execute(
        """SELECT categoria, COUNT(*) n, SUM(valor_global) v FROM contratos
           WHERE categoria IS NOT NULL GROUP BY 1 ORDER BY v DESC"""):
        print(f"    {str(r[0])[:26]:28} {r[1]:>6,}x  R$ {r[2] or 0:>18,.2f}")
    print("\n  Top fornecedores:")
    for f in e["top_fornecedores"][:10]:
        print(f"    R$ {f['valor'] or 0:>16,.2f} | {f['contratos']:>3}x | "
              f"{str(f['fornecedor'])[:44]}")
    print(f"{'='*70}\n")
    return 0


def cmd_web(args) -> int:
    """Sobe a interface web (licitações + resultados)."""
    from .web_app import rodar
    try:
        rodar(host=args.host, porta=args.porta, db_path=args.db,
              permitir_todas=args.permitir_todas)
    except FileNotFoundError as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"\nNão consegui abrir {args.host}:{args.porta} — {e}", file=sys.stderr)
        print("Tente outra porta: --porta 8081\n", file=sys.stderr)
        return 1
    return 0


def cmd_links(args) -> int:
    """Lista licitações com todos os links: PNCP, portal de origem e documentos."""
    from .documentos import listar_documentos_oportunidade

    conn = connect(args.db)
    ops = consultar(
        conn, uf=args.uf, texto=args.texto, apenas_abertos=not args.todas,
        ordem="data_encerramento ASC", limite=args.limite,
    )
    if not ops:
        print("Nenhum registro.")
        return 0

    col = PNCPCollector(delay=args.delay) if args.com_documentos else None
    for i, o in enumerate(ops, 1):
        d = _dias_restantes(o.get("data_encerramento"))
        tag = "HOJE" if d is not None and 0 <= d < 1 else (f"{d:.0f}d" if d is not None else "?")
        print(f"\n[{i}] {str(o.get('data_encerramento'))[:16]} ({tag}) | "
              f"{o.get('uf')} | {str(o.get('orgao'))[:45]}")
        print(f"    {' '.join(str(o.get('objeto') or '').split())[:150]}")
        print(f"    PNCP     : {o.get('link')}")
        if o.get("link_sistema_origem"):
            print(f"    Portal   : {o.get('link_sistema_origem')}")
        if o.get("link_processo"):
            print(f"    Processo : {o.get('link_processo')}")
        if col:
            docs = listar_documentos_oportunidade(col, o)
            if docs:
                print(f"    Documentos ({len(docs)}):")
                for doc in docs[:6]:
                    print(f"      - {doc.get('tipo')}: {doc.get('titulo')}")
                    print(f"        {doc.get('url')}")
            else:
                print("    Documentos: nenhum anexado no PNCP")
    return 0


def cmd_fontes(args) -> int:
    print("\nCATÁLOGO DE FONTES\n" + "=" * 78)
    for f in CATALOGO:
        print(f"\n▸ {f.nome}")
        print(f"  URL      : {f.url}")
        print(f"  Cobertura: {f.cobertura}")
        print(f"  Método   : {f.metodo}   |   Auth: {f.auth}")
        print(f"  Status   : {f.status}")
        if f.observacao:
            print(f"  Nota     : {f.observacao}")
    print()
    return 0


# ------------------------------------------------------------ ingestão
def cmd_ingerir(args) -> int:
    """Constrói/atualiza a base de dados completa — sem perfil de empresa."""
    from datetime import date, timedelta

    from .ingest import MODALIDADES_PRINCIPAIS, MODALIDADES_TODAS, backfill_publicacoes, snapshot_abertos

    conn = connect(args.db)
    col = PNCPCollector(delay=args.delay, use_cache=not args.no_cache)

    if args.modalidades == "todas":
        mods = MODALIDADES_TODAS
    elif args.modalidades == "principais":
        mods = MODALIDADES_PRINCIPAIS
    else:
        mods = tuple(int(m) for m in args.modalidades.split(",") if m.strip())

    print(f"Base: {args.db}")
    print(f"Modalidades: {list(mods)}")
    if args.backfill:
        partes = args.backfill.split(":")
        d1 = datetime.strptime(partes[0], "%Y-%m-%d").date()
        d2 = datetime.strptime(partes[1], "%Y-%m-%d").date() if len(partes) > 1 else date.today()
        est = ((d2 - d1).days + 1)
        print(f"Backfill histórico: {d1} .. {d2} ({est} dias)")

    t_ini = time.time()

    if args.backfill:
        r = backfill_publicacoes(
            conn, inicio=d1, fim=d2, modalidades=mods, col=col,
            max_paginas=args.max_paginas, passo_dias=args.passo_dias,
            pular_existentes=not args.recoletar, verbose=_log,
        )
        print(f"\nBackfill: {r['vistas']} vistas, {r['novas']} novas "
              f"({r['blocos_ok']} blocos)", file=sys.stderr)

    if args.abertos:
        r2 = snapshot_abertos(
            conn, dias=args.janela_abertos, modalidades=mods,
            col=col, max_paginas=args.max_paginas, verbose=_log,
        )
        print(f"Snapshot aberto: {r2['vistas']} vistas, {r2['novas']} novas", file=sys.stderr)

    dur = time.time() - t_ini
    est = resumo_base(conn)
    print(f"\n{'='*60}\nBASE ATUALIZADA em {dur/60:.1f} min")
    print(f"  Total de oportunidades : {est['total_oportunidades']:,}")
    print(f"  Abertas agora          : {est['abertas_agora']:,}")
    print(f"  Requisições            : {col.client.stats}")
    print(f"{'='*60}")
    return 0


def cmd_sync(args) -> int:
    """Atualização incremental — para rodar de hora em hora via cron."""
    from .ingest import MODALIDADES_TODAS, snapshot_abertos

    conn = connect(args.db)
    col = PNCPCollector(delay=args.delay, use_cache=False)
    antes = conn.execute("SELECT COUNT(*) FROM oportunidades").fetchone()[0]

    r = snapshot_abertos(conn, dias=args.janela, modalidades=MODALIDADES_TODAS,
                         col=col, verbose=_log)
    depois = conn.execute("SELECT COUNT(*) FROM oportunidades").fetchone()[0]
    print(f"sync: {r['vistas']} vistas | +{depois-antes} novas | total {depois:,}")
    return 0


def cmd_listar(args) -> int:
    linhas = consultar(
        conn := connect(args.db),
        uf=args.uf, municipio=args.municipio, orgao=args.orgao,
        modalidade=args.modalidade, modo_disputa=args.modo_disputa,
        amparo_legal=args.amparo_legal, texto=args.texto,
        material_ou_servico=args.tipo, tipo_beneficio=args.beneficio,
        categoria=args.categoria, setor=args.setor,
        setor_confianca=args.confianca, esfera=args.esfera,
        apenas_srp=args.srp, apenas_conteudo_nacional=args.nacional,
        apenas_sem_resultado=args.sem_resultado,
        apenas_abertos=args.abertos,
        valor_min=args.valor_min, valor_max=args.valor_max,
        limite=args.limite, ordem=args.ordem,
    )
    if not linhas:
        print("Nenhum registro. Rode `ingerir` primeiro.")
        return 0
    print(f"{len(linhas)} registro(s):\n")
    for r in linhas:
        print(f"[{r.get('id')}]")
        print(f"  {r.get('orgao')} ({r.get('uf')}/{r.get('municipio')})")
        print(f"  {r.get('modalidade')} | {r.get('modo_disputa')} | {r.get('instrumento')}")
        if r.get("unidade_nome"):
            print(f"  Unidade: {r['unidade_nome']}")
        print(f"  {' '.join(str(r.get('objeto') or '').split())[:200]}")
        if r.get("processo"):
            print(f"  Processo: {r['processo']}", end="")
        if r.get("amparo_legal"):
            print(f"  |  Base legal: {r['amparo_legal']}", end="")
        print()
        extras = []
        if r.get("valor_estimado"):
            extras.append(f"Valor: R$ {r['valor_estimado']:,.2f}")
        if r.get("data_encerramento"):
            extras.append(f"Prazo: {str(r['data_encerramento'])[:16]}")
        if r.get("srp"):
            extras.append("SRP: sim")
        if r.get("tipo_beneficio"):
            extras.append(f"Benefício: {r['tipo_beneficio']}")
        if r.get("material_ou_servico"):
            extras.append("Serviço" if r["material_ou_servico"] == "S" else "Material")
        if extras:
            print("  " + "  |  ".join(extras))
        print(f"  {r.get('link')}")
        if r.get("link_sistema_origem"):
            print(f"  Portal: {r['link_sistema_origem']}")
        print()
    return 0


def cmd_exportar(args) -> int:
    conn = connect(args.db)
    filtros = dict(
        uf=args.uf, modalidade=args.modalidade, texto=args.texto,
        material_ou_servico=args.tipo, apenas_abertos=args.abertos,
        valor_min=args.valor_min, valor_max=args.valor_max,
    )
    if args.formato == "csv":
        n = exportar_csv(conn, args.destino, **filtros)
    else:
        n = exportar_jsonl(conn, args.destino, com_raw=args.com_raw, **filtros)
    tam = Path(args.destino).stat().st_size / 1024
    print(f"Exportados {n:,} registros para {args.destino} ({tam:,.0f} KB)")
    return 0


def cmd_base(args) -> int:
    print(json.dumps(resumo_base(connect(args.db)), ensure_ascii=False, indent=2))
    return 0


def cmd_resumo(args) -> int:
    """Resumo legível da base — o que existe, sem precisar de perfil."""
    from .store import stats as st
    conn = connect(args.db)
    e = resumo_base(conn)
    s = st(conn)
    print(f"\n{'='*66}\n  BASE DE LICITAÇÕES — {args.db}\n{'='*66}")
    print(f"  Oportunidades      : {e['total_oportunidades']:,}")
    print(f"  Abertas agora      : {e['abertas_agora']:,}")
    print(f"  Com prazo          : {e['com_prazo']:,}")
    print(f"  Com valor estimado : {e['com_valor']:,}")
    print(f"  Itens detalhados   : {s['itens']:,}")
    p = e["periodo"]
    print(f"  Período            : {str(p['primeira_publicacao'])[:10]} .. {str(p['ultima_publicacao'])[:10]}")
    print(f"\n  Por esfera   : {e['por_esfera']}")
    print(f"  Tipo         : {e['material_ou_servico']}")
    print(f"\n  Top UFs      : {dict(list(e['por_uf'].items())[:12])}")
    print(f"\n  Top modalidades:")
    for k, v in list(e["por_modalidade"].items())[:10]:
        print(f"     {str(k):38} {v:>8,}")
    print(f"{'='*66}\n")
    return 0


def cmd_status(args) -> int:
    conn = connect(args.db)
    print(json.dumps(stats(conn), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="licitabot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(RAIZ / "data" / "licitabot.db"))
    ap.add_argument("--delay", type=float, default=0.6, help="pausa entre requisições (s)")
    ap.add_argument("--no-cache", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("varredura", help="varredura completa + ranqueamento")
    p.add_argument("--perfil", default=str(RAIZ / "config" / "empresa.example.json"))
    p.add_argument("--dias", type=int, default=7, help="janela de prazo (0 desliga)")
    p.add_argument("--paginas", type=int, default=2, help="páginas por termo")
    p.add_argument("--max-paginas-prazo", type=int, default=3)
    p.add_argument("--max-termos", type=int, default=8)
    p.add_argument("--incluir-fechados", action="store_true")
    p.add_argument("--sem-enriquecer", action="store_true",
                   help="não buscar prazo/valor no detalhe de cada edital")
    p.add_argument("--max-enriquecer", type=int, default=40,
                   help="limite de requisições de enriquecimento por execução")
    p.add_argument("--limite", type=int, default=20, help="quantos exibir")
    p.set_defaults(func=cmd_varredura)

    p = sub.add_parser("buscar", help="busca um termo específico")
    p.add_argument("--perfil", default=str(RAIZ / "config" / "empresa.example.json"))
    p.add_argument("--termo", required=True)
    p.add_argument("--uf", default=None)
    p.add_argument("--somente-abertos", action="store_true")
    p.add_argument("--limite", type=int, default=20)
    p.set_defaults(func=cmd_buscar)

    p = sub.add_parser("fontes", help="lista o catálogo de fontes")
    p.set_defaults(func=cmd_fontes)

    p = sub.add_parser("status", help="estatísticas do banco")
    p.set_defaults(func=cmd_status)

    # ---------------------------------------------------------- base de dados
    p = sub.add_parser("ingerir", help="ingestão em massa: backfill histórico + snapshot aberto")
    p.add_argument("--backfill", default=None,
                   help="período histórico AAAA-MM-DD:AAAA-MM-DD (ex.: 2026-08-01:2026-09-30)")
    p.add_argument("--abertos", action="store_true", help="capturar também janela aberta")
    p.add_argument("--janela-abertos", type=int, default=15, help="dias à frente (padrão 15)")
    p.add_argument("--modalidades", default="principais",
                   help="'principais' | 'todas' | lista '5,6,7,8'")
    p.add_argument("--passo-dias", type=int, default=31, help="tamanho da janela de backfill")
    p.add_argument("--max-paginas", type=int, default=None, help="limite de páginas por bloco (teste)")
    p.add_argument("--recoletar", action="store_true", help="não pular blocos já coletados")
    p.set_defaults(func=cmd_ingerir)

    p = sub.add_parser("sync", help="atualização incremental (para cron)")
    p.add_argument("--janela", type=int, default=15, help="dias à frente (padrão 15)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("enriquecer",
                       help="preenche material/serviço + categoria na base (em lotes)")
    p.add_argument("--limite", type=int, default=500,
                   help="quantos registros processar nesta execução")
    p.set_defaults(func=cmd_enriquecer)

    p = sub.add_parser("resumo", help="panorama da base de dados")
    p.set_defaults(func=cmd_resumo)

    p = sub.add_parser("base", help="panorama da base em JSON")
    p.set_defaults(func=cmd_base)

    p = sub.add_parser("listar", help="consultar a base (sem perfil de empresa)")
    p.add_argument("--uf")
    p.add_argument("--municipio")
    p.add_argument("--orgao")
    p.add_argument("--modalidade")
    p.add_argument("--modo-disputa", help="Aberto | Aberto-Fechado | Dispensa Com Disputa")
    p.add_argument("--amparo-legal", help="ex.: 'Art. 75' (dispensa), 'Art. 28' (pregão)")
    p.add_argument("--esfera", choices=["F", "E", "M"], help="F=Federal E=Estadual M=Municipal")
    p.add_argument("--texto", help="busca em objeto, itens, processo e informação complementar")
    p.add_argument("--tipo", choices=["S", "M"], help="S=serviço, M=material")
    p.add_argument("--beneficio", help="ex.: 'ME/EPP' (participação exclusiva)")
    p.add_argument("--categoria", help="ex.: 'Informática'")
    p.add_argument("--setor", help="tecnologia|saude|obras|educacao|transporte|alimentacao|limpeza|seguranca|cultura_esporte|meio_ambiente|servicos_gerais|misto|indefinido")
    p.add_argument("--confianca", choices=["alta","media","baixa"], help="confiança da classificação setorial")
    p.add_argument("--srp", action="store_true", help="só registro de preços")
    p.add_argument("--nacional", action="store_true", help="só exigência de conteúdo nacional")
    p.add_argument("--sem-resultado", action="store_true", help="só as ainda sem resultado")
    p.add_argument("--abertos", action="store_true")
    p.add_argument("--valor-min", type=float)
    p.add_argument("--valor-max", type=float)
    p.add_argument("--ordem", default="data_publicacao DESC")
    p.add_argument("--limite", type=int, default=20)
    p.set_defaults(func=cmd_listar)

    p = sub.add_parser("exportar", help="exportar a base para CSV/JSONL")
    p.add_argument("--formato", choices=["csv", "jsonl"], default="csv")
    p.add_argument("--destino", required=True)
    p.add_argument("--uf")
    p.add_argument("--modalidade")
    p.add_argument("--texto")
    p.add_argument("--tipo", choices=["S", "M"])
    p.add_argument("--abertos", action="store_true")
    p.add_argument("--valor-min", type=float)
    p.add_argument("--valor-max", type=float)
    p.add_argument("--com-raw", action="store_true", help="incluir payload original (JSONL)")
    p.set_defaults(func=cmd_exportar)

    # ------------------------------------------------------------- documentos
    p = sub.add_parser("renormalizar",
                       help="re-extrai colunas do raw_json guardado (sem rede)")
    p.add_argument("--limite", type=int, default=None)
    p.set_defaults(func=cmd_renormalizar)

    p = sub.add_parser("classificar",
                       help="classificação setorial da base (rotina separada)")
    p.add_argument("--limite", type=int, default=None)
    p.add_argument("--apenas-sem-setor", action="store_true")
    p.add_argument("--reclassificar-abaixo-de", choices=["alta"], default=None,
                   help="reprocessa tudo que não tem confiança alta")
    p.add_argument("--buscar-itens", action="store_true",
                   help="buscar /itens no PNCP para quem não tem (custa 1 req/registro)")
    p.add_argument("--listar-setores", action="store_true")
    p.set_defaults(func=cmd_classificar)

    p = sub.add_parser("setores", help="panorama da classificação setorial")
    p.set_defaults(func=cmd_setores)

    p = sub.add_parser("atualizar-dados",
                       help="re-extrai todos os campos informativos (detalhe + itens)")
    p.add_argument("--uf")
    p.add_argument("--texto")
    p.add_argument("--abertos", action="store_true", help="só as com prazo em aberto")
    p.add_argument("--limite", type=int, default=100)
    p.set_defaults(func=cmd_atualizar_dados)

    p = sub.add_parser("contratos", help="inteligência de resultados: quem ganhou, por quanto")
    p.add_argument("--edital", help="ver vencedores de um edital específico")
    p.add_argument("--fornecedor", help="histórico de um fornecedor (CNPJ)")
    p.set_defaults(func=cmd_contratos)

    p = sub.add_parser("web", help="sobe a interface web (licitações + resultados)")
    p.add_argument("--host", default="127.0.0.1",
                   help="127.0.0.1 (padrão, local) ou 0.0.0.0 para expor na rede")
    p.add_argument("--porta", type=int, default=8080)
    p.add_argument("--permitir-todas", action="store_true",
                   help="libera qualquer origem (padrão: só localhost e tailnet)")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("links", help="lista licitações com links e documentos")
    p.add_argument("--uf")
    p.add_argument("--texto")
    p.add_argument("--todas", action="store_true", help="incluir encerradas")
    p.add_argument("--com-documentos", action="store_true",
                   help="consultar o PNCP e listar os arquivos de cada uma")
    p.add_argument("--limite", type=int, default=20)
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("baixar", help="baixar editais e anexos (PDF)")
    p.add_argument("--pasta", default=None, help="pasta de destino (padrão dados/editais)")
    p.add_argument("--uf")
    p.add_argument("--texto")
    p.add_argument("--tipos", default=None,
                   help="filtrar por tipo: 'Edital', 'Termo de Referência'…")
    p.add_argument("--todas", action="store_true", help="incluir encerradas")
    p.add_argument("--limite", type=int, default=20)
    p.set_defaults(func=cmd_baixar)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
