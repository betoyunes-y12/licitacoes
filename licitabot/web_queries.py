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
    "texto":              ("__texto__", "texto", "str"),   # colunas em TEXTO_OPORTUNIDADE
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

# Colunas que a busca livre varre. Ficam em constante PRÓPRIA porque cada
# tabela tem as suas: a de contratos não tem `itens_json`, e usar a lista de
# oportunidades contra ela dá "no such column". Foi um bug real na interface
# de resultados -- buscar por fornecedor retornava erro interno.
TEXTO_OPORTUNIDADE = ("objeto", "itens_json", "processo",
                      "informacao_complementar", "orgao")
TEXTO_CONTRATO = ("objeto", "fornecedor", "ni_fornecedor", "orgao",
                  "processo", "categoria")

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


def normalizar_filtros(filtros: dict) -> dict:
    """Une `uf` e `uf[]` numa chave só, sempre como lista.

    O frontend envia checkboxes como `uf[]=SP` (sintaxe de array). O backend
    esperava `uf`. A chave `uf[]` não casava com a whitelist e era ignorada em
    silêncio — o filtro de UF simplesmente não funcionava, e a resposta vinha
    com a base inteira. Aceitar as duas formas elimina a classe de erro.
    """
    saida: dict = {}
    for chave, valor in (filtros or {}).items():
        base = chave[:-2] if chave.endswith("[]") else chave
        vals = valor if isinstance(valor, (list, tuple)) else [valor]
        vals = [v for v in vals if v not in (None, "")]
        if not vals:
            continue
        if base in saida:
            atual = saida[base] if isinstance(saida[base], list) else [saida[base]]
            saida[base] = atual + vals
        else:
            saida[base] = vals
    return saida


def _montar_where(filtros: dict, mapa: dict,
                  colunas_texto: tuple[str, ...] = TEXTO_OPORTUNIDADE
                  ) -> tuple[list[str], list]:
    """Traduz filtros em cláusulas WHERE parametrizadas.

    Múltiplos valores na MESMA faceta viram OR — escolher "SP" e "MG" significa
    "SP ou MG". Entre facetas diferentes é AND, que é o comportamento esperado
    de um painel de filtros.
    """
    where: list[str] = []
    args: list = []

    for chave, bruto in normalizar_filtros(filtros).items():
        regra = mapa.get(chave)
        if not regra:
            continue                      # chave desconhecida: ignora com segurança
        coluna, op, tipo = regra
        valores = [_coerce(v, tipo) for v in bruto]
        valores = [v for v in valores if v is not None]
        if not valores:
            continue
        valor = valores[0]

        if op == "texto":
            # As colunas vêm do parâmetro, não do nome da tabela: cada tabela
            # tem o seu conjunto. `itens_json` entra nas oportunidades porque
            # muita licitação tem objeto genérico ("aquisição de bens de TI") e
            # só os itens dizem o que é -- mas a tabela de contratos não tem
            # essa coluna, e referenciá-la ali quebrava a busca.
            cond = " OR ".join(f"{col} LIKE ?" for col in colunas_texto)
            where.append(f"({cond})")
            args += [f"%{valor}%"] * len(colunas_texto)
        elif op == "eq":
            # OR entre valores da mesma faceta (SP ou MG), AND entre facetas
            marcadores = ", ".join("?" for _ in valores)
            where.append(f"{coluna} IN ({marcadores})"); args += valores
        elif op == "like":
            if len(valores) == 1:
                where.append(f"{coluna} LIKE ?"); args.append(f"%{valor}%")
            else:
                cond = " OR ".join(f"{coluna} LIKE ?" for _ in valores)
                where.append(f"({cond})")
                args += [f"%{v}%" for v in valores]
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
    v = normalizar_filtros(filtros).get("abertos")
    if isinstance(v, list):
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
            "texto":        ("__texto__", "texto", "str"),   # TEXTO_CONTRATO
            "valor_min":    ("valor_global", "min", "num"),
            "valor_max":    ("valor_global", "max", "num"),
            "publicado_de": ("data_publicacao", "min", "str"),
        }
        ordens, base = ORDENACOES_CONTRATO, "contratos"

    colunas_texto = (TEXTO_OPORTUNIDADE if tabela == "oportunidades"
                     else TEXTO_CONTRATO)
    where, args = _montar_where(filtros, mapa, colunas_texto)

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

    colunas_texto = (TEXTO_OPORTUNIDADE if tabela == "oportunidades"
                     else TEXTO_CONTRATO)
    where, args = _montar_where(filtros, mapa, colunas_texto)
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


# Campos que a interface consulta mas que podem estar sem dado na base.
# A UI usa isto para DESABILITAR o filtro e explicar o motivo, em vez de deixar
# o usuário clicar e receber zero resultados sem entender por quê.
CAMPOS_OPCIONAIS = [
    ("tipo_beneficio", "Benefício (ME/EPP)", "enriquecer"),
    ("material_ou_servico", "Tipo (serviço/material)", "enriquecer"),
    ("categoria_item", "Categoria do item", "enriquecer"),
]


def cobertura_campos(conn) -> dict:
    """Diz quais campos opcionais têm dado, e quanto.

    Serve para a interface avisar em vez de enganar. Um filtro que sempre
    devolve zero é pior que um filtro ausente: o usuário conclui que a
    ferramenta está vazia.
    """
    total = conn.execute("SELECT COUNT(*) FROM oportunidades").fetchone()[0] or 1
    saida = {}
    for campo, rotulo, como_preencher in CAMPOS_OPCIONAIS:
        n = conn.execute(
            f"SELECT COUNT(*) FROM oportunidades WHERE {campo} IS NOT NULL"
        ).fetchone()[0]
        saida[campo] = {
            "rotulo": rotulo,
            "preenchidos": n,
            "total": total,
            "percentual": round(n * 100 / total),
            "disponivel": n > 0,
            "como_preencher": como_preencher,
        }
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
        "cobertura": cobertura_campos(conn),
    }
