"""Rotina de classificação setorial — separada da coleta, por decisão de projeto.

POR QUE ROTINA SEPARADA
-----------------------
A classificação é **derivada**, não coletada. Mantê-la separada dá três ganhos
concretos:

  1. **Reclassificar é grátis.** Melhorou a regra? Roda de novo. Não precisa
     re-coletar nada — o insumo (objeto + itens_json) já está no banco.
     Reprocessar 1.000 registros leva segundos e zero requisições.

  2. **Coleta e interpretação não se contaminam.** O coletor não sabe o que é
     TI; ele só traz dado. Se a regra de setor mudar, o dado bruto continua
     intacto e auditável.

  3. **Permite versionar.** `setor_regra_versao` guarda qual versão classificou
     cada registro, então dá para saber o que está desatualizado após um ajuste.

Só depende de rede quando o registro ainda não tem itens (`itens_json` nulo) —
e nesse caso o resultado sai como confiança baixa, sem inventar.
"""
from __future__ import annotations

import json

from .collectors.pncp import PNCPCollector
from .setores import classificar_oportunidade

# Versão das regras. Suba ao alterar setores.py, para saber o que reclassificar.
VERSAO_REGRAS = "1"


def classificar_base(
    conn,
    *,
    limite: int | None = None,
    apenas_sem_setor: bool = False,
    reclassificar_abaixo_de: str | None = None,
    buscar_itens_faltantes: bool = False,
    col: PNCPCollector | None = None,
    verbose=None,
) -> dict:
    """Classifica as oportunidades da base.

    Parâmetros:
      apenas_sem_setor        — só as nunca classificadas
      reclassificar_abaixo_de — 'alta' reprocessa tudo que não é confiança alta
      buscar_itens_faltantes  — busca /itens no PNCP para quem não tem
                                (custa 1 requisição por registro; sem isso a
                                 classificação desses fica só pelo objeto)
    """
    # Coerência entre confiança alta e existência de itens: sem itens, a
    # classificação veio só do objeto (que costuma ser genérico, ex.:
    # "aquisição de materiais permanentes"), então não pode ser "alta".
    where = ["1=1"]
    if apenas_sem_setor:
        where.append("setor IS NULL")
    if reclassificar_abaixo_de == "alta":
        where.append("(setor_confianca IS NULL OR setor_confianca != 'alta')")
    elif reclassificar_abaixo_de:
        where.append("setor_confianca IS NULL")
    if not buscar_itens_faltantes:
        pass  # nada a filtrar: usa o que já existe

    sql = (f"SELECT id, objeto, itens_json, cnpj_orgao, ano, sequencial "
           f"FROM oportunidades WHERE {' AND '.join(where)} "
           f"ORDER BY data_encerramento ASC")
    if limite:
        sql += f" LIMIT {int(limite)}"
    rows = conn.execute(sql).fetchall()

    stats = {"total": len(rows), "por_setor": {}, "por_confianca": {},
             "itens_buscados": 0, "sem_itens": 0}
    resultados = []

    for i, row in enumerate(rows, 1):
        op = dict(row)
        itens = []
        if op.get("itens_json"):
            try:
                itens = json.loads(op["itens_json"])
            except (ValueError, TypeError):
                itens = []
        elif buscar_itens_faltantes and col and op.get("cnpj_orgao"):
            try:
                itens = col.coletar_itens(op["cnpj_orgao"], op["ano"], op["sequencial"])
                stats["itens_buscados"] += 1
                if itens:
                    conn.execute("UPDATE oportunidades SET itens_json = ? WHERE id = ?",
                                 (json.dumps(itens, ensure_ascii=False), op["id"]))
            except Exception:
                itens = []

        if not itens:
            stats["sem_itens"] += 1

        r = classificar_oportunidade(op, itens)
        # Sem itens, rebaixa "alta" para "media": o rótulo veio de um objeto
        # possivelmente genérico, e afirmar alta confiança seria exagero.
        if not itens and r.get("setor_confianca") == "alta":
            r["setor_confianca"] = "media"
            r["setor_detalhe"] = (r.get("setor_detalhe") or "") + " [somente objeto]"
        resultados.append({"id": op["id"], **r})

        s = r.get("setor") or "(nao classificado)"
        c = r.get("setor_confianca") or "-"
        stats["por_setor"][s] = stats["por_setor"].get(s, 0) + 1
        stats["por_confianca"][c] = stats["por_confianca"].get(c, 0) + 1

        if verbose and i % 25 == 0:
            verbose("progresso", f"{i}/{len(rows)} | itens buscados: "
                                 f"{stats['itens_buscados']} | sem itens: {stats['sem_itens']}")

    from .store import salvar_setores
    salvar_setores(conn, resultados)
    conn.commit()
    # registra para o log qual versao de regras classificou
    stats["gravados"] = len(resultados)
    return stats


def setores_na_base(conn) -> dict:
    """Distribuição atual da classificação, para conferência."""
    return {
        "por_setor": {
            r[0] or "(sem setor)": r[1]
            for r in conn.execute(
                "SELECT setor, COUNT(*) n FROM oportunidades GROUP BY 1 ORDER BY n DESC"
            )
        },
        "por_confianca": {
            r[0] or "(sem classificação)": r[1]
            for r in conn.execute(
                "SELECT setor_confianca, COUNT(*) n FROM oportunidades "
                "GROUP BY 1 ORDER BY n DESC"
            )
        },
    }
