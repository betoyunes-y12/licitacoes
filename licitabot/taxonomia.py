"""Classificação ESTRUTURAL por código oficial — o complemento que faltava.

CONTEXTO
--------
O PNCP não fornece código de classificação (verificado: 0% em NCM e catálogo).
Por isso a classificação setorial teve de ser por texto, com as limitações
conhecidas: falso positivo de "servidor público", "drone agrícola", nome do
fornecedor no texto, e setores genuinamente híbridos.

O Compras.gov.br **fornece** código, e ele é bom:

  codigoGrupo  -> grupo de SERVIÇO, da taxonomia oficial (148 grupos)
  codigoClasse -> classe de MATERIAL (711 classes)

Medido num lote real de 500 itens: 72 classes distintas e 17 grupos distintos,
incluindo `grupo=131` (Serviços de Computação em Nuvem) e `grupo=151`
(Outsourcing de Impressão). Ou seja: código presente e significativo.

DIVISÕES DA TAXONOMIA OFICIAL (as que importam para setor)

  div 11-17  Desenvolvimento de software, nuvem, telecom, outsourcing de
             impressão, infraestrutura de TI, análise de dados
  div 18     Licenciamento e transferência de tecnologia
  div 80     Serviços de TIC (guarda-chuva)
  div 84     Telecomunicações e fornecimento de informações on-line
  div 54     Construção
  div 63     Alojamento e alimentação
  div 64-67  Transporte
  div 85     Suporte (inclui 853 = limpeza)
  div 86     Agricultura
  div 92     Educação
  div 93     Saúde
  div 94     Esgoto e saneamento
  div 96     Recreação, cultura e esporte

ESTE MÓDULO É O CAMINHO PREFERENCIAL
------------------------------------
`setor_de_codigo()` tem prioridade sobre `setores.classificar_item()`. Código
oficial é fato; palavra no texto é indício. O texto continua sendo usado como
fallback — para os registros cujo código não existe ou não foi mapeado.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Grupo de SERVIÇO -> setor. O código de grupo é o mais específico, então vem
# primeiro na decisão.
# ---------------------------------------------------------------------------
GRUPO_PARA_SETOR: dict[int, str] = {
    # div 11-12: desenvolvimento e manutenção de software
    111: "tecnologia", 112: "tecnologia", 113: "tecnologia",
    114: "tecnologia", 115: "tecnologia", 116: "tecnologia", 117: "tecnologia",
    # div 13: computação em nuvem
    131: "tecnologia",
    # div 14: telecomunicações e telefonia
    141: "tecnologia", 142: "tecnologia",
    # div 15: outsourcing de impressão
    151: "tecnologia", 152: "tecnologia", 153: "tecnologia",
    # div 16: infraestrutura de TI
    161: "tecnologia", 162: "tecnologia", 163: "tecnologia", 164: "tecnologia",
    # div 17: análise de dados e consultoria em TI
    171: "tecnologia", 172: "tecnologia", 173: "tecnologia", 174: "tecnologia",
    # div 18: licenciamento de tecnologia
    182: "tecnologia",
    # div 80: guarda-chuva de TIC
    800: "tecnologia",
    # div 84: telecomunicações e informações on-line
    841: "tecnologia", 842: "tecnologia", 843: "tecnologia",
    # div 54: construção
    541: "obras", 542: "obras", 543: "obras", 544: "obras", 545: "obras",
    # div 63: alimentação
    631: "cultura_esporte",   # alojamento
    632: "alimentacao", 633: "alimentacao",
    # div 64-67: transporte
    641: "transporte", 642: "transporte", 643: "transporte", 644: "transporte",
    651: "transporte", 652: "transporte", 653: "transporte",
    661: "transporte", 662: "transporte", 663: "transporte",
    671: "transporte", 672: "transporte", 673: "transporte",
    # div 68: postal
    681: "transporte",
    # div 71-73: financeiro, imobiliário, leasing
    711: "servicos_gerais", 712: "servicos_gerais", 713: "servicos_gerais",
    721: "servicos_gerais", 722: "servicos_gerais",
    731: "transporte", 732: "servicos_gerais", 733: "servicos_gerais",
    # div 81-83: P&D, jurídico, engenharia e consultoria
    811: "educacao", 812: "educacao", 813: "educacao",
    821: "servicos_gerais", 822: "servicos_gerais", 823: "servicos_gerais",
    831: "servicos_gerais",
    832: "obras",           # arquitetura e planejamento urbano
    833: "obras",           # engenharia
    # div 85: suporte
    851: "servicos_gerais", 852: "seguranca", 853: "limpeza",
    854: "servicos_gerais",
    # div 86: agricultura e meio ambiente
    861: "meio_ambiente", 862: "meio_ambiente", 863: "meio_ambiente",
    864: "meio_ambiente",
    # div 87: manutenção e reparo (genérico)
    871: "servicos_gerais", 872: "servicos_gerais", 873: "servicos_gerais",
    # div 89: reprodução e impressão
    891: "servicos_gerais",
    # div 91: administração pública
    911: "servicos_gerais", 912: "servicos_gerais", 913: "servicos_gerais",
    # div 92: educação
    921: "educacao", 922: "educacao", 923: "educacao", 924: "educacao",
    929: "educacao",
    # div 93: saúde
    931: "saude", 932: "saude", 933: "servicos_gerais",
    # div 94: saneamento
    941: "obras", 942: "meio_ambiente", 943: "obras",
    # div 96: recreação, cultura, esporte
    961: "cultura_esporte", 962: "cultura_esporte", 963: "cultura_esporte",
    964: "cultura_esporte", 965: "cultura_esporte",
    # div 97: outros
    971: "limpeza",
}

# ---------------------------------------------------------------------------
# Divisão (2 primeiros dígitos) -> setor. Usado quando o grupo não está mapeado
# mas a divisão dá sinal suficiente.
# ---------------------------------------------------------------------------
DIVISAO_PARA_SETOR: dict[int, str] = {
    11: "tecnologia", 12: "tecnologia", 13: "tecnologia", 14: "tecnologia",
    15: "tecnologia", 16: "tecnologia", 17: "tecnologia", 18: "tecnologia",
    54: "obras",
    63: "alimentacao",
    64: "transporte", 65: "transporte", 66: "transporte", 67: "transporte",
    68: "transporte",
    80: "tecnologia",
    81: "educacao",
    82: "servicos_gerais", 83: "servicos_gerais",
    84: "tecnologia",
    85: "limpeza",
    86: "meio_ambiente",
    87: "servicos_gerais",
    89: "servicos_gerais",
    91: "servicos_gerais",
    92: "educacao",
    93: "saude",
    94: "obras",
    96: "cultura_esporte",
    97: "limpeza",
}

# ---------------------------------------------------------------------------
# Classe de MATERIAL -> setor. Faixas da tabela oficial de materiais.
# Aqui vale a FAIXA, não a classe individual (são 711): a primeira parte do
# código já separa os grandes grupos.
# ---------------------------------------------------------------------------
FAIXA_CLASSE_PARA_SETOR: list[tuple[int, int, str]] = [
    # informática e equipamentos de escritório
    (7000, 7099, "tecnologia"),   # equipamentos de TI
    (7400, 7499, "tecnologia"),   # máquinas de escritório / informática
    (5800, 5899, "tecnologia"),   # equipamentos de comunicação
    (6000, 6099, "tecnologia"),   # instrumentos de medição e comunicação
    # saúde
    (6500, 6599, "saude"),        # instrumentos médicos e cirúrgicos
    (6600, 6699, "saude"),        # instrumentos de laboratório
    (7300, 7399, "saude"),        # material médico (faixa usual)
    # obras e materiais de construção
    (5600, 5699, "obras"),        # materiais de construção
    (3800, 3899, "obras"),
    # materiais em geral / escritório
    (7500, 7599, "servicos_gerais"),
    (8100, 8199, "servicos_gerais"),
    (8500, 8599, "servicos_gerais"),
    # química e combustíveis
    (9100, 9199, "servicos_gerais"),
    (6800, 6899, "servicos_gerais"),
    # agrícola
    (3700, 3799, "meio_ambiente"),
    (8700, 8799, "meio_ambiente"),
    # veículos e peças
    (2300, 2399, "transporte"),
    (2500, 2599, "transporte"),
    (2900, 2999, "transporte"),
]


def setor_de_codigo(codigo_grupo=None, codigo_classe=None,
                    material_ou_servico: str | None = None) -> tuple[str | None, str]:
    """Classifica por código oficial. Devolve (setor, confianca).

    Confiança:
      'alta'  -> grupo de serviço mapeado (o mais específico da taxonomia)
      'media' -> divisão conhecida, ou faixa de classe de material
      None    -> sem código utilizável
    """
    # O grupo é o código mais específico. Vale para serviço.
    if codigo_grupo is not None:
        try:
            g = int(codigo_grupo)
        except (TypeError, ValueError):
            g = None
        if g is not None:
            if g in GRUPO_PARA_SETOR:
                return GRUPO_PARA_SETOR[g], "alta"
            div = g // 10 if g >= 100 else g
            if div in DIVISAO_PARA_SETOR:
                return DIVISAO_PARA_SETOR[div], "media"

    # Classe é código de material: usa faixa.
    if codigo_classe is not None:
        try:
            c = int(codigo_classe)
        except (TypeError, ValueError):
            c = None
        if c is not None:
            for lo, hi, setor in FAIXA_CLASSE_PARA_SETOR:
                if lo <= c <= hi:
                    return setor, "media"

    return None, None
