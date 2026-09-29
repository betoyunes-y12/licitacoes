"""Download de documentos dos editais.

Descobri testando ao vivo que o PNCP **hospeda os arquivos**, não só linka:

    GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/arquivos
    → [{"titulo": "Anexo I - Termo de Referencia.pdf", "tipo": "Termo de Referência",
        "url": "https://pncp.gov.br/pncp-api/v1/orgaos/.../arquivos/1"}]

E o download devolve o binário de verdade — verificado: PDF 1.7, 13 páginas,
469 KB. Ou seja: dá para baixar o edital inteiro, não só apontar o link.

Duas fontes de arquivo, em ordem de preferência:

  1. **PNCP** (`/arquivos`) — padronizado, com metadados (título e tipo).
     É o caminho principal. Nem toda contratação tem arquivo anexado: órgãos
     que publicam só o resumo no PNCP e mantêm o edital no portal próprio
     (BEC/SP, Licitanet, Portal de Compras Públicas) aparecem sem anexos.
  2. **`linkSistemaOrigem`** — o portal onde o edital realmente está. Presente
     em ~50% dos registros (Compras.gov.br, portais municipais, Licitanet…).
     Não é baixável de forma automática: cada portal tem seu próprio fluxo.
     Guardamos o link para conferência humana.

Organização em disco:
    dados/editais/{uf}/{ano}/{id_limpo}/{seq}-{titulo}.pdf
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .collectors.pncp import PNCPCollector
from .store import connect, now_iso


def _slug(texto: str, limite: int = 60) -> str:
    """Nome de arquivo seguro: sem acento, sem caractere especial."""
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9._-]+", "_", t).strip("_")
    return t[:limite] or "documento"


EXTENSOES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".odt", ".ods")


def _nome_arquivo(seq: int, titulo: str | None) -> str:
    """Monta o nome final preservando SEMPRE a extensão.

    Detalhe que já causou bug: truncar o nome DEPOIS de acrescentar o sufixo
    corta a extensão e o arquivo fica sem tipo reconhecido (o sistema
    operacional não abre com duplo clique). Por isso: separa o sufixo,
    trunca só o miolo, e reconcatena.
    """
    titulo = str(titulo or "documento").strip()
    ext = ""
    baixo = titulo.lower()
    for e in EXTENSOES:
        if baixo.endswith(e):
            ext, titulo = titulo[-len(e):], titulo[: -len(e)]
            break
    if not ext:
        # O PNCP costuma informar só o nome lógico, sem sufixo. Assume PDF,
        # que é o formato da quase totalidade dos documentos de licitação.
        ext = ".pdf"
    nome = _slug(titulo, limite=70)
    return f"{int(seq):02d}-{nome}{ext.lower()}"


def _pasta_destino(raiz: Path, op: dict) -> Path:
    uf = op.get("uf") or "XX"
    ano = op.get("ano") or "0000"
    ident = re.sub(r"[^A-Za-z0-9-]", "_", str(op.get("id") or "sem-id"))
    return raiz / uf / str(ano) / ident


def listar_documentos_oportunidade(col: PNCPCollector, op: dict) -> list[dict]:
    cnpj, ano, seq = op.get("cnpj_orgao"), op.get("ano"), op.get("sequencial")
    if not (cnpj and ano and seq):
        return []
    try:
        return col.listar_documentos(cnpj, ano, seq)
    except Exception:
        return []


def baixar_oportunidade(
    col: PNCPCollector,
    op: dict,
    *,
    raiz: Path,
    tipos: list[str] | None = None,
    max_arquivos: int = 20,
    verbose=None,
) -> dict:
    """Baixa os documentos de UMA oportunidade. Devolve balanço."""
    docs = listar_documentos_oportunidade(col, op)
    if tipos:
        # filtro por tipo de documento (ex.: só "Edital")
        alvo_tipos = [t.lower() for t in tipos]
        docs = [d for d in docs
                if any(t in str(d.get("tipo") or "").lower() or
                       t in str(d.get("titulo") or "").lower() for t in alvo_tipos)]

    if not docs:
        return {"id": op.get("id"), "documentos": 0, "baixados": 0, "pasta": None}

    pasta = _pasta_destino(raiz, op)
    baixados = 0
    for i, d in enumerate(docs[:max_arquivos], 1):
        url = d.get("url")
        if not url:
            continue
        seq = d.get("sequencial") or i
        destino = pasta / _nome_arquivo(seq, d.get("titulo"))
        if col.baixar_documento(url, destino):
            baixados += 1
            if verbose:
                kb = destino.stat().st_size / 1024
                verbose("baixado", f"{destino.name} ({kb:,.0f} KB)")

    # registra no banco quais arquivos existem
    try:
        (pasta / "_documentos.json").write_text(
            json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass

    return {"id": op.get("id"), "documentos": len(docs), "baixados": baixados,
            "pasta": str(pasta)}


def marcar_contagem_documentos(conn, resultados: list[dict]) -> int:
    """Grava qtd_documentos na tabela de oportunidades."""
    n = 0
    for r in resultados:
        if r.get("documentos") is not None:
            conn.execute("UPDATE oportunidades SET qtd_documentos = ? WHERE id = ?",
                         (r["documentos"], r["id"]))
            n += 1
    conn.commit()
    return n


def baixar_lote(
    *,
    db: str | Path | None = None,
    raiz: str | Path | None = None,
    limite: int = 20,
    uf: str | None = None,
    texto: str | None = None,
    apenas_abertos: bool = True,
    somente_com_documento: bool = True,
    tipos: list[str] | None = None,
    delay: float = 0.6,
    verbose=None,
) -> dict:
    """Baixa documentos de um lote de oportunidades — o job em lote.

    Ordena por prazo mais próximo: o edital da licitação que vence antes é o
    que você precisa ler primeiro.
    """
    from .export import agora_brt

    raiz = Path(raiz or "dados/editais")
    conn = connect(db)
    col = PNCPCollector(delay=delay, use_cache=True)

    where, args = ["cnpj_orgao IS NOT NULL", "ano IS NOT NULL", "sequencial IS NOT NULL"], []
    if apenas_abertos:
        where.append("data_encerramento >= ?"); args.append(agora_brt())
    if uf:
        where.append("uf = ?"); args.append(uf.upper())
    if texto:
        where.append("objeto LIKE ?"); args.append(f"%{texto}%")
    if somente_com_documento:
        # já sabemos que tem documento (qtd_documentos > 0) ou ainda não verificamos
        where.append("(qtd_documentos IS NULL OR qtd_documentos > 0)")

    sql = (f"SELECT * FROM oportunidades WHERE {' AND '.join(where)} "
           f"ORDER BY data_encerramento ASC LIMIT ?")
    ops = [dict(r) for r in conn.execute(sql, args + [limite])]

    total_docs = total_baixados = com_doc = sem_doc = 0
    resultados = []
    for i, op in enumerate(ops, 1):
        r = baixar_oportunidade(col, op, raiz=raiz, tipos=tipos, verbose=verbose)
        resultados.append(r)
        total_docs += r["documentos"]
        total_baixados += r["baixados"]
        if r["documentos"]:
            com_doc += 1
        else:
            sem_doc += 1
        if verbose and i % 10 == 0:
            verbose("progresso", f"{i}/{len(ops)} | {total_baixados} arquivos baixados")

    marcar_contagem_documentos(conn, resultados)
    return {
        "oportunidades": len(ops), "com_documento": com_doc, "sem_documento": sem_doc,
        "documentos_encontrados": total_docs, "arquivos_baixados": total_baixados,
        "pasta_raiz": str(raiz), "stats_http": col.client.stats,
    }
