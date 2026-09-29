"""Camada de consulta para a interface web: paginação, ordenação e facetas.

POR QUE UM MÓDULO SEPARADO
--------------------------
`export.py` faz consulta simples (lista e exporta). Uma interface web precisa
de três coisas que ele não dá:

  1. **Paginação com total** — saber quantos registros existem, não só a página.
  2. **Facetas** — contagem por UF, setor, modalidade… para os filtros mostrarem
     quantos resultados cada opção traz ANTES de clicar. Sem isso o usuário
     escolhe um filtro às cegas e cai em "nenhum resultado".
  3. **Facetas que respeitam os filtros ativos** — se filtrou por setor
     tecnologia, a lista de UFs deve mostrar só as UFs com tecnologia. É o que
     diferencia um filtro útil de um inútil.

REGRA DE SEGURANÇA
------------------
Nenhum valor vindo do cliente entra em SQL por interpolação. Toda coluna
passa por whitelist e todo valor por placeholder `?`.
"""
from __future__ import annotations

from .export import agora_brt

# --------------------------------------------------------------------------
# Filtros de licitações. Cada entrada: (coluna, operador, tipo).
#   tipo: "eq" igualdade | "like" contém | "min" >= | "max" <=
# --------------------------------------------------------------------------
FILTROS_OPORTUNIDADE: dict[str, tuple[str, str, str]] = {
    "uf":                 ("uf", "eq", "str"),
    "municipio":          ("municipio", "like", "str"),
    "orgao":              ("orgao", "like", "str"),
    "esfera":             ("esfera", "eq", "str"),
    "setor":              ("setor", "eq", "str"),
    "confianca":          ("setor_confianca", "eq", "str"),
    "modalidade":         ("modalidade", "like", "str"),
    "modo_disputa":       ("modo_disputa", "like", "str"),
    "instrumento":        ("instrumento", "like", "str"),
    "amparo_legal":       ("amparo_legal", "like", "str"),
    "tipo":               ("material_ou_servico", "eq", "str"),
    "beneficio":          ("tipo_beneficio", "like", "str"),
    "processo":           ("processo", "like", "str"),
    "texto":              ("__texto__", "texto", "str"),
    "valor_min":          ("valor_estimado", "min", "num"),
    "valor_max":          ("valor_estimado", "max", "num"),
    "prazo_de":           ("data_encerramento", "min", "str"),
    "prazo_ate":          ("data_encerramento", "max", "str"),
    "publicado_de":       ("data_publicacao", "min", "str"),
    "ano":                ("ano", "eq", "int"),
    "srp":                ("srp", "eqbool", "bool"),
    "conteudo_nacional":  ("conteudo_nacional", "eqbool", "bool"),
    "sem_resultado":      ("existe_resultado", "falsy", "bool"),
    "com_resultado":      ("existe_resultado", "eqbool", "bool"),
}

ORDENACOES_OPORTUNIDADE = {
    "prazo":        "data_encerramento ASC",
    "prazo_desc":   "data_encerramento DESC",
    "publicacao":   "data_publicacao DESC",
    "valor":        "valor_estimado DESC",
    "valor_asc":    "valor_estimado ASC",
    "orgao":        "orgao ASC",
    "uf":           "uf ASC",
}

FACETAS_OPORTUNIDADE = [
    ("uf", "UF"),
    ("setor", "Setor"),
    ("modalidade", "Modalidade"),
    ("esfera", "Esfera"),
    ("modo_disputa", "Modo de disputa"),
    ("material_ou_servico", "Tipo"),
    ("setor_confianca", "Confiança"),
    ("ano", "Ano"),
]

ORDENACOES_CONTRATO = {
    "valor":      "valor_global DESC",
    "valor_asc":  "valor_global ASC",
    "publicacao": "data_publicacao DESC",
    "fornecedor": "fornecedor ASC",
}

FACETAS_CONTRATO = [
    ("uf", "UF"),
    ("categoria", "Categoria"),
    ("orgao", "Órgão"),
]

# --------------------------------------------------------------------------
# Tabelas permitidas. Impede que um parâmetro aponte para tabela arbitrária.
# --------------------------------------------------------------------------
TABELAS = {
    "oportunidades": "oportunidades",
    "contratos": "contratos",
}


def _coerce(valor, tipo):
    """Converte o valor recebido da query string para o tipo da coluna."""
    if valor is None or valor == "":
        return None
    if tipo == "num":
        try:
            return float(valor)
        except (TypeError, ValueError):
            return None
    if tipo == "int":
        try:
            return int(valor)
        except (TypeError, ValueError):
            return None
    if tipo == "bool":
        return str(valor).lower() in ("1", "true", "sim", "yes")
    return str(valor)


def _montar_where(filtros: dict, mapa: dict) -> tuple[list[str], list]:
    """Traduz filtros em cláusulas WHERE parametrizadas."""
    where: list[str] = []
    args: list = []

    for chave, bruto in (filtros or {}).items():
        regra = mapa.get(chave)
        if not regra:
            continue                      # chave desconhecida: ignora com segurança
        if isinstance(bruto, (list, tuple)):
            bruto = bruto[0] if bruto else None
        coluna, op, tipo = regra
        valor = _coerce(bruto, tipo)
        if valor is None:
            continue

        if op == "texto":
            # procura no objeto, itens, processo e informação complementar.
            # `itens_json` entra porque muita licitação tem objeto genérico
            # ("aquisição de bens de TI") e só os itens dizem o que é.
            where.append("(objeto LIKE ? OR itens_json LIKE ? OR "
                         "processo LIKE ? OR informacao_complementar LIKE ? OR "
                         "orgao LIKE ?)")
            args += [f"%{valor}%"] * 5
        elif op == "eq":
            where.append(f"{coluna} = ?"); args.append(valor)
        elif op == "like":
            where.append(f"{coluna} LIKE ?"); args.append(f"%{valor}%")
        elif op == "min":
            where.append(f"{coluna} >= ?"); args.append(valor)
        elif op == "max":
            where.append(f"{coluna} <= ?"); args.append(valor)
        elif op == "eqbool":
            where.append(f"{coluna} = ?"); args.append(1 if valor else 0)
        elif op == "falsy":
            where.append(f"({coluna} = 0 OR {coluna} IS NULL)")

    return where, args


def _somente_abertos(filtros: dict) -> bool:
    v = (filtros or {}).get("abertos")
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return str(v).lower() in ("1", "true", "sim", "yes")


def consultar_paginado(
    conn,
    tabela: str = "oportunidades",
    filtros: dict | None = None,
    *,
    pagina: int = 1,
    por_pagina: int = 25,
    ordem: str | None = None,
) -> dict:
    """Consulta com paginação, total e itens. Devolve dict pronto para JSON."""
    if tabela not in TABELAS:                # whitelist de tabela
        tabela = "oportunidades"

    if tabela == "oportunidades":
        mapa, ordens, base = FILTROS_OPORTUNIDADE, ORDENACOES_OPORTUNIDADE, "oportunidades"
    else:
        mapa = {
            "uf":           ("uf", "eq", "str"),
            "municipio":    ("municipio", "like", "str"),
            "orgao":        ("orgao", "like", "str"),
            "fornecedor":   ("fornecedor", "like", "str"),
            "ni":           ("ni_fornecedor", "eq", "str"),
            "categoria":    ("categoria", "like", "str"),
            "edital":       ("edital_id", "eq", "str"),
            "texto":        ("__texto__", "texto", "str"),
            "valor_min":    ("valor_global", "min", "num"),
            "valor_max":    ("valor_global", "max", "num"),
            "publicado_de": ("data_publicacao", "min", "str"),
        }
        ordens, base = ORDENACOES_CONTRATO, "contratos"

    where, args = _montar_where(filtros, mapa)

    # "abertos" só existe para oportunidades (prazo no futuro, em horário de
    # Brasília — comparar com UTC escondia as que venciam no próprio dia)
    if tabela == "oportunidades" and _somente_abertos(filtros or {}):
        where.append("data_encerramento >= ?")
        args.append(agora_brt())

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(f"SELECT COUNT(*) FROM {base}{where_sql}", args).fetchone()[0]

    ordenacao = ordens.get(ordem or "", next(iter(ordens.values())))
    pagina = max(1, int(pagina or 1))
    por_pagina = min(max(1, int(por_pagina or 25)), 200)
    offset = (pagina - 1) * por_pagina

    linhas = conn.execute(
        f"SELECT * FROM {base}{where_sql} ORDER BY {ordenacao} LIMIT ? OFFSET ?",
        args + [por_pagina, offset],
    ).fetchall()

    itens = []
    for r in linhas:
        d = dict(r)
        for pesado in ("raw_json", "detalhe_json", "itens_json"):
            d.pop(pesado, None)          # não trafega payload bruto para a UI
        itens.append(d)

    return {
        "itens": itens,
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "paginas": max(1, (total + por_pagina - 1) // por_pagina),
        "tabela": tabela,
    }


def facetas(conn, tabela: str = "oportunidades", filtros: dict | None = None) -> dict:
    """Contagem por valor de cada campo facetável, respeitando os filtros ativos.

    Respeitar os filtros é o ponto: ao escolher "setor=tecnologia", a faceta de
    UF mostra quantas de tecnologia existem por estado — e não o total geral.
    Assim o usuário não clica num filtro que resulta em zero.
    """
    if tabela == "oportunidades":
        mapa, defs, base = FILTROS_OPORTUNIDADE, FACETAS_OPORTUNIDADE, "oportunidades"
    else:
        mapa = {
            "uf":         ("uf", "eq", "str"),
            "municipio":  ("municipio", "like", "str"),
            "orgao":      ("orgao", "like", "str"),
            "fornecedor": ("fornecedor", "like", "str"),
            "categoria":  ("categoria", "like", "str"),
            "texto":      ("__texto__", "texto", "str"),
            "valor_min":  ("valor_global", "min", "num"),
            "valor_max":  ("valor_global", "max", "num"),
        }
        defs, base = FACETAS_CONTRATO, "contratos"

    where, args = _montar_where(filtros, mapa)
    if tabela == "oportunidades" and _somente_abertos(filtros or {}):
        where.append("data_encerramento >= ?")
        args.append(agora_brt())
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    saida = {}
    for campo, rotulo in defs:
        # o campo da faceta é sempre da whitelist acima, nunca do cliente
        if not any(v[0] == campo for v in mapa.values()):
            continue
        linhas = conn.execute(
            f"""SELECT {campo} AS valor, COUNT(*) AS n
                FROM {base}{where_sql}
                GROUP BY {campo}
                HAVING {campo} IS NOT NULL AND {campo} != ''
                ORDER BY n DESC LIMIT 40""",
            args,
        ).fetchall()
        saida[campo] = {
            "rotulo": rotulo,
            "valores": [{"valor": r["valor"], "n": r["n"]} for r in linhas],
        }

    # dados gerais que a UI mostra no topo
    saida["_total"] = conn.execute(
        f"SELECT COUNT(*) FROM {base}{where_sql}", args).fetchone()[0]
    if tabela == "oportunidades":
        saida["_abertos"] = conn.execute(
            f"SELECT COUNT(*) FROM {base}{where_sql}"
            + (" AND " if where_sql else " WHERE ") + "data_encerramento >= ?",
            args + [agora_brt()]).fetchone()[0]
    return saida


def estatisticas_gerais(conn) -> dict:
    """Números do topo da interface."""
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    agora = agora_brt()
    return {
        "oportunidades": q("SELECT COUNT(*) FROM oportunidades"),
        "abertas": q("SELECT COUNT(*) FROM oportunidades WHERE data_encerramento >= ?", agora),
        "vencendo_7d": q(
            """SELECT COUNT(*) FROM oportunidades
               WHERE data_encerramento >= ? AND data_encerramento <= datetime(?, '+7 days')""",
            agora, agora),
        "com_valor": q(
            "SELECT COUNT(*) FROM oportunidades WHERE valor_estimado > 0"),
        "valor_total_aberto": q(
            """SELECT COALESCE(SUM(valor_estimado),0) FROM oportunidades
               WHERE data_encerramento >= ?""", agora),
        "contratos": q("SELECT COUNT(*) FROM contratos"),
        "fornecedores": q("SELECT COUNT(DISTINCT ni_fornecedor) FROM contratos"),
        "valor_contratado": q("SELECT COALESCE(SUM(valor_global),0) FROM contratos"),
        "tecnologia_abertas": q(
            """SELECT COUNT(*) FROM oportunidades
               WHERE setor = 'tecnologia' AND data_encerramento >= ?""", agora),
    }
