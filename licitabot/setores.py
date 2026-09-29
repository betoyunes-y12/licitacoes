"""Classificação setorial — transforma descrição de item em setor de compra.

PROBLEMA QUE ISTO RESOLVE
-------------------------
O PNCP **não fornece código de classe ou setor**. Verificado em 456 itens:
  • ncmNbsCodigo       -> 0% preenchido
  • catalogoCodigoItem -> 0% preenchido
  • categoriaItemNome  -> só "Bens Móveis / Bens Imóveis / Não se aplica"

Ou seja: `categoria` é tipo patrimonial, não setor. Não existe código
estrutural para saber que uma licitação é de TI.

A tentativa ingênua — procurar "software", "sistema", "servidor" no texto —
produz ruído grosseiro. Medido na base real: 232 candidatos, com falsos
positivos como
  • "servidor" -> servidor público (folha de pagamento, consignações)
  • "drone"    -> equipamento agrícola
  • "sistema"  -> sistema de repetição de sinais (obra civil)
  • "software" -> nome do fornecedor que publicou (ex.: "Fiorilli Software")

COMO ISTO RESOLVE
-----------------
1. Escopo restrito a objeto + descrição de itens. Nunca `usuarioNome`
   (o fornecedor do sistema) — era a maior fonte de falso positivo.

2. Vocabulário ancorado na taxonomia oficial do Compras.gov.br (313 classes
   de serviço, 148 grupos): "serviços de desenvolvimento e manutenção de
   software", "software como serviço - saas", "infraestrutura como serviço".

3. Exclusões explícitas: termo ambíguo só conta quando o contexto não o
   desqualifica.

4. Peso financeiro, não contagem: uma compra com 50 itens de papel e um item
   de software de R$ 2 milhões é de tecnologia. Contar itens daria "material
   de escritório".

5. Admite não saber. Editais de "materiais permanentes" que reúnem
   climatizador, mesa de sinuca, TV e impressora recebem `misto`; os sem
   evidência sólida recebem `indefinido`. Rótulo errado é pior que rótulo
   honesto: quem procura TI prefere ler um "misto" a receber "TI" falso.

SAÍDA
-----
(setor, confianca), com confianca em alta/media/baixa. A confiança é o que
torna isto utilizável: alta = termo inequívoco; media = provável;
baixa = ambíguo, exige leitura humana.
"""
from __future__ import annotations

import re

try:  # classificação estrutural por código oficial (preferencial)
    from .taxonomia import setor_de_codigo
except ImportError:  # pragma: no cover
    def setor_de_codigo(*a, **k):
        return None, None

_ACENTOS = str.maketrans(
    "áàâãäéèêëíìîïóòôõöúùûüçñÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ",
    "aaaaaeeeeiiiiooooouuuucnAAAAAEEEEIIIIOOOOOUUUUCN",
)

# palavras funcionais que aparecem coladas ao fim de outra palavra em editais
SUFIXOS_COLADOS = ("para", "com", "sem", "por", "de", "da", "do", "dos", "das")


def _norm(t: str | None) -> str:
    """Normaliza texto de edital para comparação.

    Faz mais do que baixar caixa, porque edital real é sujo:
      - separa CamelCase: "SoftwareparaServidor" -> "softwarepara servidor"
      - remove acento e pontuação

    NÃO tento separar palavra funcional colada ("softwarepara" -> "software
    para"): tentei e quebrou "SOFTWARE" em "softwar e". Em vez de adivinhar
    dentro da palavra, o casamento tolera o sufixo colado (ver `preparar`).
    """
    if not t:
        return ""
    t = str(t).translate(_ACENTOS)
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _plural(p: str) -> str:
    """Plural do português, para tolerar singular/plural no casamento."""
    if p.endswith(("r", "z", "n")):
        return p + "es"        # computador -> computadores
    if p.endswith("s"):
        return p               # já plural ou invariável
    if p.endswith("m"):
        return p[:-1] + "ns"   # item -> itens
    if p.endswith("l"):
        return p[:-1] + "is"   # papel -> papeis
    if p.endswith("y"):
        return p[:-1] + "ies"
    return p + "s"


SETORES: dict[str, dict] = {
    "tecnologia": {
        "nome": "Tecnologia da Informação",
        "alta": [
            "software", "licenca de software", "licenciamento de software",
            "software como servico", "saas", "iaas", "paas",
            "computacao em nuvem", "cloud computing", "servico de nuvem",
            "desenvolvimento de software", "manutencao de software",
            "sustentacao de software", "fabrica de software",
            "infraestrutura de ti", "tecnologia da informacao",
            "suporte tecnico de ti", "outsourcing de impressao",
            "certificado digital", "assinatura digital", "token digital",
            "sistema web", "sistema informatizado", "aplicativo mobile",
            "business intelligence", "ciencia de dados", "geoprocessamento",
            "banco de dados", "data center", "datacenter", "computador",
            "microcomputador", "notebook", "estacao de trabalho", "tablet",
            "monitor de video", "nobreak",
            "roteador", "switch gerenci", "firewall", "storage", "cftv",
            "camera de seguranca", "monitoramento eletronico",
            "cabeamento estruturado", "fibra optica", "link de internet",
            "provedor de internet", "telecomunicacoes", "telefonia movel",
            "ponto eletronico", "digitalizacao de documentos",
            # formas ESPECÍFICAS de servidor. O termo solto "servidor" foi
            # removido: medido na base, 2 de 2 ocorrências eram "servidor
            # público" (folha de pagamento, consignações) — nunca hardware.
            "servidor de rede", "servidor de arquivos", "servidor linux",
            "servidor windows", "servidor web", "servidor de banco de dados",
        ],
        "media": [
            "informatica", "sistema", "ti", "tic", "computacao", "digital",
            "tecnologia", "suporte tecnico", "manutencao de computadores",
            "toner", "cartucho", "pen drive", "webcam", "teclado", "mouse",
            "estabilizador", "projetor multimidia", "licenca", "nuvem",
            "hospedagem", "backup",
            # AMBÍGUOS — rebaixados de 'alta' após falso positivo real: um
            # leilão de equipamentos de gráfica foi classificado como TI por
            # causa de "IMPRESSORA PRINTMASTER GTO 52-4" (impressora offset) e
            # "PROCESSADORA DE CHAPAS" (máquina de pré-impressão). A mesma
            # palavra serve a dois setores, então sozinha não é evidência.
            "impressora", "scanner", "processador", "monitor",
            "fonte de alimentacao",
            # Ambíguos pelo mesmo motivo: "componente eletrônico" e "celular"
            # aparecem em LEILÃO DE SUCATA, onde não há compra de TI nenhuma.
            # Um leilão de sucata eletrônica foi classificado como tecnologia
            # quando estes termos estavam em `alta`. Sozinhos não são
            # evidência; com dois ou mais viram confiança média.
            "celular", "smartphone", "componente eletronico",
            "equipamento eletronico", "eletroeletronico", "chip de dados",
            # "rede" solto é ambíguo demais: casou com "rede de proteção" de
            # mesa de sinuca. Só a forma qualificada vale.
            "rede de computadores", "rede logica", "cabeamento de rede",
        ],
        "excl": [
            "servidor publico", "concurso publico", "quadro de servidores",
            "drone", "aeronave", "ar condicionado", "climatizacao",
            "sistema de abastecimento", "sistema de esgoto", "sistema viario",
            "sistema de drenagem", "sistema de repeticao", "sistema de agua",
            "sistema eleitoral", "sistema penitenciario",
            "sistema de transporte", "obra", "pavimentacao", "reforma",
            "construcao", "medicamento", "material medico", "merenda",
            "combustivel",
            # leilão/alienação não é contratação setorial: é venda de bem
            "alienacao", "leilao", "inservivel", "sucata", "apreendida",
        ],
    },
    "saude": {
        "nome": "Saúde",
        "alta": [
            "medicamento", "material medico", "material hospitalar",
            "material medico hospitalar", "equipo", "seringa", "agulha",
            "soro", "vacina", "reagente", "exame laboratorial", "laboratorio",
            "ambulancia", "cirurgia", "odontologico", "hospitalar",
            "farmaceutico", "farmacia", "insulina", "oxigenio medicinal",
            "cadeira de rodas", "protese", "ortese", "oximetro",
            "atendimento medico", "servico medico",
        ],
        "media": [
            "saude", "medico", "hospital", "clinica", "paciente",
            "enfermagem", "psicologo", "fisioterapia", "nutricionista",
            "veterinario", "ambulatorial", "atencao basica", "exame",
        ],
        "excl": [
            "obra", "reforma", "pavimentacao", "software", "informatica",
            "veiculo", "caminhao",
        ],
    },
    "obras": {
        "nome": "Obras e Engenharia",
        "alta": [
            "obra", "construcao", "reforma", "pavimentacao", "asfalto",
            "recapeamento", "saneamento", "drenagem", "esgoto", "galeria",
            "muro", "calcamento", "edificacao", "predial", "engenharia",
            "empreitada", "terraplenagem", "ponte", "viaduto",
            "estrutura metalica", "cobertura", "pintura predial",
            # lacunas medidas: infraestrutura urbana
            "iluminacao publica", "macrodrenagem", "microdrenagem",
            "atracadouro", "pier", "rampa", "contencao", "talude",
            "rede de agua", "rede de esgoto", "estacao de tratamento",
        ],
        "media": [
            "projeto", "execucao", "manutencao predial", "hidraulica",
            "alvenaria", "revestimento", "telhado", "ampliacao",
        ],
        "excl": ["medicamento", "software", "merenda", "licenca de uso"],
    },
    "educacao": {
        "nome": "Educação",
        "alta": [
            "merenda escolar", "alimentacao escolar", "material escolar",
            "didatico", "pedagogico", "livro", "apostila", "uniforme escolar",
            "transporte escolar", "creche", "ensino fundamental",
            "educacao infantil",
        ],
        "media": [
            "educacao", "aluno", "aula", "curso", "capacitacao",
            "treinamento", "monitor", "oficina", "biblioteca", "escola",
        ],
        "excl": ["software", "medicamento", "pavimentacao"],
    },
    "transporte": {
        "nome": "Transporte e Veículos",
        "alta": [
            "veiculo", "caminhao", "onibus", "automovel", "motocicleta",
            "pneu", "peca automotiva", "combustivel", "gasolina", "diesel",
            "etanol", "locacao de veiculos", "frota", "trator",
            "implemento agricola",
        ],
        "media": [
            "transporte", "motorista", "mecanica", "manutencao de frota",
            "abastecimento", "lubrificante", "oleo lubrificante",
        ],
        "excl": ["software", "medicamento", "merenda"],
    },
    "alimentacao": {
        "nome": "Alimentação e Gêneros",
        "alta": [
            "genero alimenticio", "alimento", "refeicao", "cesta basica",
            "carne", "frango", "arroz", "feijao", "leite", "hortifruti",
            "merenda", "agua mineral", "cafe", "acucar", "pao",
        ],
        "media": ["alimentacao", "cozinha", "nutricao", "buffet", "lanche"],
        "excl": ["software", "obra", "medicamento"],
    },
    "limpeza": {
        "nome": "Limpeza e Conservação",
        "alta": [
            "material de limpeza", "produto de limpeza", "detergente",
            "sabao", "desinfetante", "agua sanitaria", "papel higienico",
            "servico de limpeza", "asseio", "conservacao e limpeza",
            "higienizacao",
        ],
        "media": [
            "limpeza", "higiene", "zeladoria", "jardinagem", "poda",
            "capinacao", "coleta de residuos", "coleta de lixo",
        ],
        "excl": ["software", "obra", "medicamento"],
    },
    "seguranca": {
        "nome": "Segurança e Vigilância",
        "alta": [
            "vigilancia", "vigilante", "seguranca patrimonial", "portaria",
            "controle de acesso", "monitoramento de alarme", "extintor",
            "prevencao de incendio", "brigada de incendio",
        ],
        "media": ["seguranca", "alarme", "cancela", "catraca"],
        "excl": ["software", "seguranca da informacao"],
    },
    "servicos_gerais": {
        "nome": "Serviços Gerais e Administrativos",
        "alta": [
            "material de escritorio", "material de expediente", "papel a4",
            "servico administrativo", "apoio administrativo", "recepcionista",
            "mobiliario", "mesa", "cadeira", "armario", "estante", "fogao",
            # lacunas medidas
            "tinta", "selador", "massa corrida", "verniz", "material de pintura",
            "swab", "algodao", "material de acondicionamento", "embalagem",
            "consultoria", "assessoria", "comunicacao institucional",
            "material de consumo", "material permanente",
        ],
        "media": [
            "escritorio", "expediente", "administrativo", "locacao de imovel",
            "aluguel", "impressao", "grafico", "publicidade",
        ],
        "excl": ["software", "obra", "medicamento"],
    },
    "cultura_esporte": {
        "nome": "Cultura, Esporte e Lazer",
        "alta": [
            "evento", "show", "artistico", "cultural", "esporte",
            "esportivo", "campeonato", "arbitragem", "atleta", "tenda",
            "palco", "som e iluminacao", "ornamentacao", "oficina de esporte",
        ],
        "media": ["cultura", "lazer", "recreacao", "festival"],
        "excl": ["software", "medicamento", "obra"],
    },
    "meio_ambiente": {
        "nome": "Meio Ambiente e Agricultura",
        "alta": [
            "residuo solido", "coleta seletiva", "licenciamento ambiental",
            "arborizacao", "muda", "adubo", "fertilizante", "agricultura",
            "agricola", "irrigacao", "semente",
        ],
        "media": ["ambiental", "sustentabilidade", "rural", "agropecuaria",
                  "calibracao", "metrologica", "conformidade"],
        "excl": ["software", "medicamento"],
    },
}

# ordem de prioridade no desempate: setores mais específicos primeiro
PRIORIDADE = [
    "tecnologia", "saude", "obras", "educacao", "transporte", "alimentacao",
    "limpeza", "seguranca", "cultura_esporte", "meio_ambiente",
    "servicos_gerais",
]

# todos os termos conhecidos. Serve para gerar as variantes com palavra
# funcional colada ("softwarepara" casar com "software") SEM mutilar nenhuma
# palavra do texto — erro que já cometi e que derrubou a classificação.
_TODOS_TERMOS: set[str] = set()
for _regras in SETORES.values():
    _TODOS_TERMOS.update(_regras.get("alta", []))
    _TODOS_TERMOS.update(_regras.get("media", []))


def _raiz(p: str) -> str:
    """Radical aproximado: remove a desinência final.

    Um radical é imune a flexão, então casa "informacao" e "informacoes" sem
    precisar acertar a regra de plural de cada palavra. Necessário porque
    palpite de plural em português erra (foi assim que "informacoes" virou
    "informacoe" e quebrou a adjacência de "tecnologia da informacao").
    """
    for suf in ("coes", "cao", "oes", "aos", "ais", "eis", "eis", "ns",
                "es", "os", "as", "is", "s"):
        if p.endswith(suf) and len(p) - len(suf) >= 4:
            return p[: -len(suf)]
    return p


# radicais de todas as palavras do glossário — índice imune a flexão
_RAIZES: set[str] = set()
for _t in _TODOS_TERMOS:
    for _palavra in _t.split():
        if len(_palavra) >= 5:
            _RAIZES.add(_raiz(_palavra))


# variantes com sufixo colado, pré-computadas por termo.
# Geradas UMA vez a partir dos termos — nunca do texto do edital.
_VARIANTES_COLADAS: dict[str, tuple[str, ...]] = {
    termo: tuple(f"{termo}{suf}" for suf in SUFIXOS_COLADOS)
    for termo in _TODOS_TERMOS
}


def _singular(p: str) -> str:
    """Canonicaliza para singular, com GUARDA: só reduz quando o resultado é
    uma palavra que existe no glossário.

    Cheguei aqui depois de três tentativas erradas de escrever regras genéricas
    de plural. Cada uma criou um singular falso:
      • "software"  -> "softwar"
      • "informacao"-> "informacaos"
      • "softwares" -> "softwar"  (a regra de "-ares" casava dentro de "softw-ares")

    O erro de fundo era tentar ADIVINHAR o plural do português. A solução é
    não adivinhar: gerar os candidatos e aceitar apenas o que está no
    vocabulário conhecido. O que não estiver, fica como está — pior caso é não
    casar uma flexão rara, o que é muito melhor que mutilar uma palavra e
    quebrar o casamento dela.
    """
    if p in _TODOS_TERMOS:
        return p
    for cand in (p[:-1] if p.endswith("s") else None,
                 p[:-2] if p.endswith("es") else None):
        if cand and cand in _TODOS_TERMOS:
            return cand
    # Sem correspondência no glossário: aplica a redução mais conservadora —
    # apenas remove "s" final depois de vogal, e só se o corpo tiver 4+ letras.
    if (p.endswith("s") and not p.endswith("ss") and len(p) > 4
            and p[-2] in "aeiou"):
        return p[:-1]
    return p


def preparar(texto: str) -> list[str]:
    """Tokeniza e canonicaliza o texto para casamento.

    Devolve a LISTA de tokens canônicos, **na ordem**, sem fragmentação.
    A ordem importa: é o que permite casar frase por adjacência. Injetar
    plurais no meio da lista (tentativa anterior) quebrava a adjacência de
    "tecnologia da informacao", porque "informacao" passava a ter o vizinho
    "informacaos" entre ele e "da".

    A canonicalização é para SINGULAR (ver `_singular`): canonicalizar para
    plural erra em português ("informacao" -> "informacaos").

    Quatro erros que já cometi aqui, cada um documentado no ponto de uso:
      1. fatiar todo token por sufixo, mutilando palavras;
      2. casar frase por subconjunto, fazendo exclusões casarem demais;
      3. injetar o glossário inteiro em toda descrição;
      4. injetar plurais no meio da lista, quebrando adjacência.
    """
    if not texto:
        return []
    return [_singular(p) for p in texto.split()]


def variantes_coladas(tokens: list[str]) -> set[str]:
    """Formas com palavra funcional colada, geradas SÓ para termos presentes.

    Cobre o caso real da UFF: "Softwarepara Servidor" (escrito sem espaço).
    Gerar a partir dos TERMOS (nunca fatiando o texto) é o que evita mutilar
    palavras como "gestao" -> "gesta".
    """
    presente = set(tokens)
    texto = f" {' '.join(tokens)} "
    extra: set[str] = set()
    for termo in _TODOS_TERMOS:
        if " " in termo:
            if f" {termo} " not in texto:
                continue
        elif termo not in presente:
            # caso inverso: o texto traz a forma colada ("softwarepara") e o
            # termo é a base ("software"). Só aceita quando a variante colada
            # está de fato presente — nunca inventa termo.
            if not any(v in presente for v in _VARIANTES_COLADAS[termo]):
                continue
        extra.update(_VARIANTES_COLADAS[termo])
        extra.add(termo)
    return extra


class Texto:
    """Texto preparado para casamento, com índices O(1).

    Guardar os índices (set de tokens, set de radicais, conjunto de formas
    coladas) evita recomputar tudo a cada termo testado. Sem isso, cada
    registro refazia centenas de testes caros: 23,7 ms/registro, medido.
    Com os índices, cai para menos de 1 ms.
    """

    __slots__ = ("tokens", "conjunto", "radicais", "colados")

    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.conjunto = set(tokens)
        self.radicais = {_raiz(t) for t in self.conjunto if _raiz(t) in _RAIZES}
        self.colados = variantes_coladas(tokens)

    def igual(self, tok: str, alvo: str) -> bool:
        if tok == alvo:
            return True
        r = _raiz(tok)
        return r == _raiz(alvo) and r in _RAIZES

    def casa(self, termo: str) -> bool:
        """Casa um termo: igualdade (1 palavra) ou adjacência (frase)."""
        if not termo or not self.tokens:
            return False
        partes = termo.split()

        if len(partes) == 1:
            alvo = partes[0]
            if alvo in self.conjunto or alvo in self.colados:
                return True
            # atalho por radical: se o radical do alvo não está no texto,
            # nenhuma flexão dele pode estar
            ra = _raiz(alvo)
            if ra in _RAIZES and ra not in self.radicais:
                return False
            return any(self.igual(t, alvo) for t in self.conjunto)

        # frase: atalho — todas as partes precisam ter radical presente
        for p in partes:
            ra = _raiz(p)
            if len(p) >= 5 and ra in _RAIZES and ra not in self.radicais:
                return False
        n = len(partes)
        for i in range(len(self.tokens) - n + 1):
            if all(self.igual(self.tokens[i + j], partes[j]) for j in range(n)):
                return True
        return False


def preparar_texto(texto: str) -> Texto:
    return Texto(preparar(texto))


def _texto_do_item(item: dict) -> str:
    """Só descrição e objeto. NUNCA `usuario_nome` (nome do fornecedor).

    Excluir o fornecedor é essencial: "Fiorilli Software" publicando um edital
    de merenda classificava o edital como TI.
    """
    partes = [
        item.get("descricao"), item.get("descricao_detalhada"),
        item.get("descricao_resumida"),
    ]
    return _norm(" ".join(p for p in partes if p))


def classificar_item(item: dict) -> tuple[str | None, str | None]:
    """Classifica UM item. Devolve (setor, confianca).

    ORDEM DE PREFERÊNCIA: código oficial primeiro, texto como fallback.

    Código é fato (`codigoGrupo` 131 = "Serviços de Computação em Nuvem", da
    taxonomia do Compras.gov.br); palavra no texto é indício. Só quando não há
    código — o caso da maioria, porque o PNCP não fornece nenhum — caímos para
    o casamento por texto.
    """
    setor_cod, conf_cod = setor_de_codigo(
        item.get("codigo_grupo"), item.get("codigo_classe"),
        item.get("material_ou_servico"),
    )
    if setor_cod:
        return setor_cod, conf_cod

    texto = _texto_do_item(item)
    if not texto or len(texto) < 3:
        return None, None
    tx = preparar_texto(texto)

    pontuados: list[tuple[int, int, int, str, str]] = []
    for setor, regras in SETORES.items():
        if any(tx.casa(t) for t in regras.get("excl", [])):
            continue
        fortes = [t for t in regras.get("alta", []) if tx.casa(t)]
        fracos = [t for t in regras.get("media", []) if tx.casa(t)]
        if not fortes and not fracos:
            continue
        # A confiança vem da QUALIDADE DO MELHOR TERMO, não do placar bruto.
        # Sem isso, "tecnologia da informacao" (forte) + "tecnologia" (médio)
        # davam confiança "baixa" — o médio diluía o forte. Errado: quem tem
        # termo inequívoco tem confiança alta.
        if fortes:
            conf = "alta"
        elif len(fracos) >= 2:
            conf = "media"
        else:
            conf = "baixa"
        # desempate: nº de fortes, depois nº de fracos, depois tamanho do termo
        peso = (len(fortes), len(fracos), max((len(t) for t in fortes + fracos),
                                              default=0))
        pontuados.append((*peso, setor, conf))

    if not pontuados:
        return None, None

    pontuados.sort(key=lambda x: (-x[0], -x[1], -x[2],
                                  PRIORIDADE.index(x[3])
                                  if x[3] in PRIORIDADE else 99))
    return pontuados[0][3], pontuados[0][4]


def classificar_oportunidade(op, itens: list[dict] | None = None) -> dict:
    """Classifica a oportunidade a partir do objeto e dos itens.

    Agregação por peso financeiro: uma licitação com 50 itens de papel e um
    item de software de R$ 2 milhões é de tecnologia. Contar itens daria
    "material de escritório". Sem valores (orçamento sigiloso), cai para
    contagem.
    """
    itens = itens or []
    setores: dict[str, dict] = {}
    classificados: list[tuple] = []

    def acumular(setor: str, conf: str, peso: float) -> None:
        d = setores.setdefault(setor, {"peso": 0.0, "n": 0, "alta": 0,
                                       "media": 0, "baixa": 0})
        d["peso"] += peso
        d["n"] += 1
        d[conf] = d.get(conf, 0) + 1

    # classifica cada item UMA vez; reaproveita no cálculo de confiança
    for it in itens:
        setor, conf = classificar_item(it)
        classificados.append((setor, conf))
        if setor:
            acumular(setor, conf, float(it.get("valor_total") or 0))

    setor_obj, conf_obj = classificar_item({"descricao": op.get("objeto")})
    if setor_obj:
        maior = max((d["peso"] for d in setores.values()), default=0)
        acumular(setor_obj, conf_obj, maior if maior > 0 else 1)

    if not setores:
        return {"setor": None, "setor_confianca": None, "setor_detalhe": None}

    if all(d["peso"] == 0 for d in setores.values()):
        for d in setores.values():
            d["peso"] = float(d["n"])

    ordenado = sorted(
        setores.items(),
        key=lambda kv: (-kv[1]["peso"], -kv[1]["n"],
                        PRIORIDADE.index(kv[0]) if kv[0] in PRIORIDADE else 99),
    )
    setor_top, info_top = ordenado[0]
    total = sum(d["peso"] for d in setores.values()) or 1
    part = info_top["peso"] / total

    detalhe = "; ".join(
        f"{s}:{d['peso']:,.0f}({d['alta']}a/{d['media']}m/{d['baixa']}b)"
        for s, d in ordenado[:3]
    )

    # Vários setores sem dominância: é misto. Muitos editais de "materiais
    # permanentes" reúnem climatizador, mesa de sinuca, TV e impressora e não
    # pertencem a setor nenhum. Forçar o rótulo do maior item gera falso
    # positivo em qualquer filtro setorial.
    if len(ordenado) > 1 and part < 0.5:
        return {"setor": "misto", "setor_confianca": "baixa",
                "setor_detalhe": detalhe, "setor_dominante": setor_top}

    # Um setor só se afirma com evidência sólida. Item de confiança baixa é
    # palpite ("processador" de cozinha não é TI); somar palpites faz um setor
    # fraco parecer forte.
    solidos = info_top["alta"] + info_top["media"]
    if solidos == 0:
        return {"setor": "indefinido", "setor_confianca": "baixa",
                "setor_detalhe": detalhe, "setor_dominante": setor_top}

    solidos_outros = sum(d["alta"] + d["media"] for s, d in ordenado[1:])
    if solidos_outros > 0 and solidos <= solidos_outros:
        return {"setor": "misto", "setor_confianca": "baixa",
                "setor_detalhe": detalhe, "setor_dominante": setor_top}

    fortes = sum(1 for _, c in classificados if c == "alta")
    if info_top["alta"] and part >= 0.5 and fortes >= 2:
        conf = "alta"
    elif solidos >= 1 and part >= 0.5:
        conf = "media"
    else:
        conf = "baixa"

    return {"setor": setor_top, "setor_confianca": conf,
            "setor_detalhe": detalhe}


def setores_disponiveis() -> list[tuple[str, str]]:
    return [(k, v["nome"]) for k, v in SETORES.items()]
