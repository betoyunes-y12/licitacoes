"""Exportação — a base de dados só tem valor se for fácil tirar dado dela.

Formatos:
  • CSV    — abre no Excel/Sheets, para uso comercial e conferência manual
  • JSONL  — uma oportunidade por linha (com raw_json), para pipelines e BI
  • SQLite — o próprio arquivo do banco, para consulta direta
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# O PNCP devolve datas SEM fuso (ex.: "2026-09-29T15:00:00"), no horário de
# Brasília. Comparar isso com `datetime('now')` do SQLite (que é UTC) erra por
# 3 horas e faz o sistema tratar como fechada uma licitação que ainda está
# aberta. Em um produto cujo propósito é avisar ANTES do prazo, esse erro é
# inaceitável: foram 8 oportunidades encerrando no mesmo dia que ficaram
# invisíveis nos testes.
FUSO_BR = timezone(timedelta(hours=-3))


def agora_brt(fmt: str = "%Y-%m-%dT%H:%M:%S") -> str:
    """Agora no horário de Brasília, no MESMO formato das datas do PNCP.

    Assim a comparação de string no SQLite funciona de forma correta e
    consistente (mesmo separador 'T', mesmo fuso).
    """
    return datetime.now(FUSO_BR).strftime(fmt)


COLUNAS_CSV = [
    "id", "fonte",
    "orgao", "cnpj_orgao", "uf", "municipio", "codigo_ibge", "esfera", "poder",
    "unidade_nome", "orgao_subrogado",
    "modalidade", "modo_disputa", "instrumento", "numero_compra", "processo",
    "ano", "sequencial",
    "objeto", "informacao_complementar",
    "valor_estimado", "valor_homologado", "srp", "orcamento_sigiloso",
    "emenda_parlamentar", "amparo_legal",
    "material_ou_servico", "categoria_item", "tipo_beneficio",
    "conteudo_nacional", "margem_preferencia_normal",
    "data_publicacao", "data_abertura", "data_encerramento",
    "data_atualizacao", "situacao", "existe_resultado",
    "qtd_documentos", "sistema_origem",
    "link",                  # página no PNCP
    "link_sistema_origem",   # portal do órgão (onde o edital costuma estar)
    "link_processo",         # processo eletrônico (SEI etc.)
]


def consultar(
    conn,
    *,
    uf: str | None = None,
    municipio: str | None = None,
    orgao: str | None = None,
    modalidade: str | None = None,
    modo_disputa: str | None = None,
    amparo_legal: str | None = None,
    material_ou_servico: str | None = None,
    tipo_beneficio: str | None = None,
    categoria: str | None = None,
    setor: str | None = None,
    setor_confianca: str | None = None,
    texto: str | None = None,
    apenas_srp: bool = False,
    apenas_conteudo_nacional: bool = False,
    apenas_com_resultado: bool = False,
    apenas_sem_resultado: bool = False,
    esfera: str | None = None,
    data_encerramento_de: str | None = None,
    data_encerramento_ate: str | None = None,
    data_publicacao_de: str | None = None,
    valor_min: float | None = None,
    valor_max: float | None = None,
    apenas_abertos: bool = False,
    ordem: str = "data_publicacao DESC",
    limite: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Consulta genérica à base — sem qualquer noção de perfil de empresa.

    `texto` procura no objeto, na informação complementar E nas descrições dos
    itens (via itens_json), porque muita licitação tem objeto genérico
    ("aquisição de bens de TI") e só os itens revelam o que é de fato.
    """
    where, args = [], []

    if uf:
        where.append("uf = ?"); args.append(uf.upper())
    if municipio:
        where.append("municipio LIKE ?"); args.append(f"%{municipio}%")
    if orgao:
        where.append("orgao LIKE ?"); args.append(f"%{orgao}%")
    if modalidade:
        where.append("modalidade LIKE ?"); args.append(f"%{modalidade}%")
    if modo_disputa:
        where.append("modo_disputa LIKE ?"); args.append(f"%{modo_disputa}%")
    if amparo_legal:
        where.append("amparo_legal LIKE ?"); args.append(f"%{amparo_legal}%")
    if esfera:
        where.append("esfera = ?"); args.append(esfera.upper()[:1])
    if material_ou_servico:
        where.append("material_ou_servico = ?"); args.append(material_ou_servico.upper())
    if tipo_beneficio:
        where.append("tipo_beneficio LIKE ?"); args.append(f"%{tipo_beneficio}%")
    if categoria:
        where.append("categoria_item LIKE ?"); args.append(f"%{categoria}%")
    if setor:
        where.append("setor = ?"); args.append(setor)
    if setor_confianca:
        where.append("setor_confianca = ?"); args.append(setor_confianca)
    if texto:
        where.append("(objeto LIKE ? OR informacao_complementar LIKE ? "
                     "OR itens_json LIKE ? OR orgao LIKE ? OR processo LIKE ?)")
        args += [f"%{texto}%"] * 5
    if apenas_srp:
        where.append("srp = 1")
    if apenas_conteudo_nacional:
        where.append("conteudo_nacional = 1")
    if apenas_com_resultado:
        where.append("existe_resultado = 1")
    if apenas_sem_resultado:
        where.append("(existe_resultado = 0 OR existe_resultado IS NULL)")
    if data_encerramento_de:
        where.append("data_encerramento >= ?"); args.append(data_encerramento_de)
    if data_encerramento_ate:
        where.append("data_encerramento <= ?"); args.append(data_encerramento_ate)
    if data_publicacao_de:
        where.append("data_publicacao >= ?"); args.append(data_publicacao_de)
    if valor_min is not None:
        where.append("valor_estimado >= ?"); args.append(valor_min)
    if valor_max is not None:
        where.append("valor_estimado <= ?"); args.append(valor_max)
    if apenas_abertos:
        # Compara no fuso de Brasília — ver comentário em `agora_brt`.
        where.append("data_encerramento >= ?")
        args.append(agora_brt())

    # whitelist de ordenação — nunca interpolar string vinda do usuário em SQL
    ordens_ok = {
        "data_publicacao DESC", "data_publicacao ASC",
        "data_encerramento ASC", "data_encerramento DESC",
        "valor_estimado DESC", "valor_estimado ASC",
        "uf ASC", "orgao ASC", "data_atualizacao DESC",
    }
    if ordem not in ordens_ok:
        ordem = "data_publicacao DESC"

    sql = "SELECT * FROM oportunidades"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {ordem} LIMIT ? OFFSET ?"
    args += [limite, offset]

    return [dict(r) for r in conn.execute(sql, args)]


def exportar_csv(conn, destino: str | Path, **filtros) -> int:
    filtros.setdefault("limite", 10_000_000)
    linhas = consultar(conn, **filtros)
    p = Path(destino)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS_CSV, extrasaction="ignore",
                           delimiter=";")  # ; porque Excel pt-BR usa vírgula decimal
        w.writeheader()
        w.writerows(linhas)
    return len(linhas)


def exportar_jsonl(conn, destino: str | Path, *, com_raw: bool = False, **filtros) -> int:
    filtros.setdefault("limite", 10_000_000)
    linhas = consultar(conn, **filtros)
    p = Path(destino)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8") as f:
        for r in linhas:
            if not com_raw:
                r.pop("raw_json", None)
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def resumo_base(conn) -> dict:
    """Panorama da base: volume, distribuição, cobertura temporal."""
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    return {
        "total_oportunidades": q("SELECT COUNT(*) FROM oportunidades"),
        "com_prazo": q("SELECT COUNT(*) FROM oportunidades WHERE data_encerramento IS NOT NULL"),
        "com_valor": q("SELECT COUNT(*) FROM oportunidades WHERE valor_estimado IS NOT NULL"),
        "abertas_agora": conn.execute(
            "SELECT COUNT(*) FROM oportunidades WHERE data_encerramento >= ?",
            (agora_brt(),),
        ).fetchone()[0],
        "periodo": {
            "primeira_publicacao": q("SELECT MIN(data_publicacao) FROM oportunidades"),
            "ultima_publicacao": q("SELECT MAX(data_publicacao) FROM oportunidades"),
        },
        "por_modalidade": {
            r[0] or "(desconhecida)": r[1]
            for r in conn.execute(
                "SELECT modalidade, COUNT(*) n FROM oportunidades "
                "GROUP BY modalidade ORDER BY n DESC LIMIT 12"
            )
        },
        "por_uf": {
            r[0] or "(sem UF)": r[1]
            for r in conn.execute(
                "SELECT uf, COUNT(*) n FROM oportunidades WHERE uf IS NOT NULL "
                "GROUP BY uf ORDER BY n DESC LIMIT 27"
            )
        },
        "por_esfera": {
            {"F": "Federal", "E": "Estadual", "M": "Municipal"}.get(r[0], r[0] or "(?") : r[1]
            for r in conn.execute(
                "SELECT esfera, COUNT(*) n FROM oportunidades GROUP BY esfera ORDER BY n DESC"
            )
        },
        "material_ou_servico": {
            {"S": "Serviço", "M": "Material"}.get(r[0], r[0] or "(não enriquecido)"): r[1]
            for r in conn.execute(
                "SELECT material_ou_servico, COUNT(*) n FROM oportunidades GROUP BY 1 ORDER BY n DESC"
            )
        },
    }
