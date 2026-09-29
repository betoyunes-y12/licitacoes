"""Motor de aderência: casa o perfil da empresa com as oportunidades coletadas.

Estratégia em CAMADAS, do mais barato/rápido ao mais caro/preciso:

  Camada 1 (determinística, este módulo): normalização de texto + dicionário
    ponderado de termos (fortes/médios/negativos) + categoria de item.
    Custo ~0, roda em milissegundos, auditável (diz POR QUE pontuou).

  Camada 2 (opcional, plugável): similaridade vetorial (embeddings) para pegar
    paráfrases que o dicionário não cobre. Ver `score_semantico()` — deixado
    como hook, não como dependência obrigatória.

  Camada 3 (opcional): reranking por LLM sobre o top-N da camada 1, para julgar
    exigências habilitatórias (atestado técnico, capital social, registro no
    conselho). É aqui que se decide "posso ou não participar".

A pontuação é EXPLICÁVEL: cada match devolve os termos que o dispararam
(`termos`). Isso é essencial — o usuário precisa confiar e auditar o filtro.
"""
from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

# ------------------------------------------------------------- normalização
_ACENTOS = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüçñÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ",
    "aaaaaeeeeiiiiooooouuuucnAAAAAEEEEIIIIOOOOOUUUUCN",
)


def normalizar(texto: str | None) -> str:
    """Minúsculas, sem acento, sem pontuação, espaços colapsados.

    Sem acento importa: editais escrevem 'licitacao', 'licitação' e 'LICITACAO'
    de formas diferentes para a mesma palavra.
    """
    if not texto:
        return ""
    t = str(texto).translate(_ACENTOS).lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokenizar(texto: str | None) -> set[str]:
    return set(normalizar(texto).split())


def _contem_termo(texto_norm: str, termo_norm: str) -> bool:
    """Casamento por fronteira de palavra (evita 'ti' casar dentro de 'gestito')."""
    if not termo_norm:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(termo_norm)}(?![a-z0-9])", texto_norm) is not None


class Perfil:
    """Perfil de uma empresa, carregado de JSON."""

    def __init__(self, dados: dict, nome: str = "perfil"):
        self.raw = dados
        self.nome = nome
        at = dados.get("atuacao", {}) or {}
        self.ufs = {u.upper() for u in (at.get("ufs") or [])}
        self.esferas = set(at.get("esferas") or [])
        self.valor_min = at.get("valor_min")
        self.valor_max = at.get("valor_max")
        self.cnaes = {str(c) for c in (dados.get("cnaes") or [])}
        self.categorias = {normalizar(c) for c in (dados.get("categorias_pncp") or [])}

        pc = dados.get("palavras_chave", {}) or {}
        self.obrigatorias = [normalizar(x) for x in (pc.get("obrigatorias") or [])]
        self.fortes = [normalizar(x) for x in (pc.get("fortes") or [])]
        self.medias = [normalizar(x) for x in (pc.get("medias") or [])]
        self.negativas = [normalizar(x) for x in (pc.get("negativas") or [])]

        pesos = dados.get("pesos", {}) or {}
        self.p_forte = pesos.get("forte", 10)
        self.p_media = pesos.get("media", 4)
        self.p_categoria = pesos.get("categoria_match", 6)
        self.p_cnae = pesos.get("cnae_match", 8)
        self.p_negativa = pesos.get("negativa_penalidade", -50)

        lim = dados.get("limiares", {}) or {}
        self.score_minimo = lim.get("score_minimo", 10)
        self.score_alerta = lim.get("score_alerta", 25)

        to = dados.get("tipo_oferta", {}) or {}
        self.aceita_servico = to.get("aceita_servico", True)
        self.aceita_material = to.get("aceita_material", True)
        self.p_material = to.get("penalidade_material", 0)
        self.termos_produto = [normalizar(t) for t in (to.get("termos_produto") or [])]

        empresa = dados.get("empresa", {}) or {}
        self.me_epp = bool(empresa.get("me_epp"))

    @classmethod
    def carregar(cls, caminho: str | Path) -> "Perfil":
        p = Path(caminho)
        dados = json.loads(p.read_text(encoding="utf-8"))
        return cls(dados, nome=p.stem)

    def termos_busca(self, limite: int | None = None) -> list[str]:
        """Termos a enviar ao portal (usa os fortes; médios só p/ varredura ampla)."""
        base = list(self.raw.get("palavras_chave", {}).get("fortes", []))
        return base[:limite] if limite else base


def _beneficio_me_epp(texto_norm: str) -> bool:
    return any(
        _contem_termo(texto_norm, normalizar(t))
        for t in ("me epp", "microempresa", "empresa de pequeno porte", "exclusivo me", "lc 123")
    )


def pontuar(oportunidade: dict, perfil: Perfil) -> dict:
    """Devolve dict com score + termos disparados + motivos. Explicável por design."""
    objeto = oportunidade.get("objeto") or ""
    texto = normalizar(objeto)
    # itens (quando disponíveis) ampliam o texto pesquisável
    itens_txt = normalizar(oportunidade.get("itens_texto") or "")
    alvo = f"{texto} {itens_txt}".strip()

    score = 0.0
    termos: list[str] = []
    motivos: list[str] = []

    if not alvo:
        return {"score": 0.0, "termos": [], "motivos": ["sem objeto"]}

    # 1) termos obrigatórios: se exigidos e ausentes, descarta de imediato
    faltando = [t for t in perfil.obrigatorias if not _contem_termo(alvo, t)]
    if faltando:
        return {
            "score": 0.0,
            "termos": [],
            "motivos": [f"falta termo obrigatório: {t}" for t in faltando],
            "descartado": True,
        }

    # 2) termos fortes (multi-palavra contam integralmente)
    for t in perfil.fortes:
        if _contem_termo(alvo, t):
            score += perfil.p_forte
            termos.append(t)

    # 3) termos médios (só conta se não for subcaso de um forte já contado)
    for t in perfil.medias:
        if _contem_termo(alvo, t):
            score += perfil.p_media
            termos.append(t)

    # 4) categoria do item (ex.: "Informática (TIC)")
    cat = normalizar(oportunidade.get("categoria_item") or "")
    if cat and cat in perfil.categorias:
        score += perfil.p_categoria
        motivos.append(f"categoria: {oportunidade.get('categoria_item')}")

    # 5) CNAE do fornecedor, quando o portal informar
    cnae = str(oportunidade.get("cnae") or "")
    if cnae and cnae in perfil.cnaes:
        score += perfil.p_cnae
        motivos.append(f"cnae: {cnae}")

    # 6) negativos: penalizam fortemente (falso positivo clássico)
    for t in perfil.negativas:
        if _contem_termo(alvo, t):
            score += perfil.p_negativa
            motivos.append(f"termo negativo: {t}")

    # 7) benefício ME/EPP é um bônus real de competitividade
    if perfil.me_epp and _beneficio_me_epp(alvo):
        score += 3
        motivos.append("benefício ME/EPP")

    # 8) produto vs serviço — corrige o falso positivo clássico de TIC.
    #    Uma software house NÃO quer disputar "aquisição de 200 notebooks",
    #    mas o edital casa em "Informática (TIC)" e "tecnologia da informacao".
    eh_material = (oportunidade.get("material_ou_servico") or "").upper().startswith("M")
    marcas_produto = [t for t in perfil.termos_produto if _contem_termo(alvo, t)]
    if eh_material and not perfil.aceita_material:
        score += perfil.p_material
        motivos.append("item de MATERIAL (produto, não serviço)")
    elif marcas_produto and not perfil.aceita_material:
        score += perfil.p_material
        motivos.append(f"indício de produto: {marcas_produto[0]}")

    return {"score": round(score, 2), "termos": sorted(set(termos)), "motivos": motivos}


def dentro_do_perfil(oportunidade: dict, perfil: Perfil) -> tuple[bool, str | None]:
    """Filtros duros (geografia/valor/esfera). Devolve (passa, motivo_exclusao)."""
    uf = (oportunidade.get("uf") or "").upper()
    if perfil.ufs and uf and uf not in perfil.ufs:
        return False, f"uf {uf} fora da atuação"
    if perfil.esferas:
        esf = (oportunidade.get("esfera") or "").upper()[:1]
        if esf and esf not in perfil.esferas:
            return False, f"esfera {esf} fora do perfil"
    valor = oportunidade.get("valor_estimado")
    if valor:
        if perfil.valor_min and valor < perfil.valor_min:
            return False, f"valor {valor} abaixo do mínimo"
        if perfil.valor_max and valor > perfil.valor_max:
            return False, f"valor {valor} acima do máximo"
    return True, None


def ranquear(oportunidades: list[dict], perfil: Perfil, *, aplicar_filtros_duros=True):
    """Pontua e ordena. Descarta abaixo do limiar e os que falham filtros duros."""
    saida = []
    for op in oportunidades:
        if aplicar_filtros_duros:
            ok, motivo = dentro_do_perfil(op, perfil)
            if not ok:
                continue
        r = pontuar(op, perfil)
        if r.get("descartado") or r["score"] < perfil.score_minimo:
            continue
        saida.append({**op, **r, "alerta": r["score"] >= perfil.score_alerta})
    saida.sort(key=lambda x: x["score"], reverse=True)
    return saida


# ------------------------------------------------ hook opcional: semântico
def score_semantico(texto_a: str, texto_b: str) -> float:
    """Similaridade léxica barata (SequenceMatcher) — placeholder da camada 2.

    NÃO é embedding: serve para fuzzy-match de títulos/erros de digitação de
    editais. Em produção, troque por embeddings (ex.: sentence-transformers
    multilíngue ou API) + índice vetorial, mantendo a mesma assinatura.
    """
    return SequenceMatcher(None, normalizar(texto_a), normalizar(texto_b)).ratio()


def deduplicar_similares(oportunidades: list[dict], limiar: float = 0.93) -> list[dict]:
    """Remove quase-duplicatas (mesmo objeto republicado em portais diferentes)."""
    vistos: list[str] = []
    saida = []
    for op in oportunidades:
        chave = normalizar(op.get("objeto"))[:400]
        if any(score_semantico(chave, v) >= limiar for v in vistos):
            continue
        vistos.append(chave)
        saida.append(op)
    return saida
