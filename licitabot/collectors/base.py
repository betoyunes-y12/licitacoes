"""Interface base para coletores e registro de fontes secundárias.

Regra de arquitetura: uma fonte secundária SÓ entra em produção se houver
(i) API pública documentada, (ii) feed RSS/JSON, ou (iii) autorização formal.
Scraping de HTML de portal privado é frágil, pode violar termos de uso e
quebra a cada redesign — por isso fica fora do escopo por padrão.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class FonteIndisponivel(RuntimeError):
    """Fonte exige scraping/credencial — não implementada de propósito."""


class ColetorBase:
    fonte: str = "base"
    requer_credencial: bool = False
    metodo: str = "api"          # api | rss | csv | scraping

    def coletar(self, **kwargs) -> list[dict]:
        raise NotImplementedError

    def disponivel(self) -> bool:
        return True


@dataclass
class Fonte:
    """Metadado de uma fonte, para o catálogo e para o relatório de cobertura."""
    nome: str
    url: str
    cobertura: str
    metodo: str
    auth: str
    status: str
    observacao: str = ""
    tags: list[str] = field(default_factory=list)


CATALOGO: list[Fonte] = [
    # ---------------------------------------------------------- AGREGADORES
    Fonte(
        nome="PNCP — Portal Nacional de Contratações Públicas",
        url="https://pncp.gov.br/api/consulta/swagger-ui/index.html",
        cobertura="União + 26 estados + DF + ~5.570 municípios (Lei 14.133/2021)",
        metodo="REST/JSON",
        auth="Nenhuma (público)",
        status="VERIFICADO AO VIVO — fonte primária",
        observacao=(
            "Obrigatório por lei (art. 174, Lei 14.133/2021). Busca textual + filtro "
            "status=recebendo_proposta. Melhor custo/benefício de longe."
        ),
        tags=["primaria", "gratuita", "completa"],
    ),
    Fonte(
        nome="Compras.gov.br — Dados Abertos",
        url="https://dadosabertos.compras.gov.br/swagger-ui/index.html",
        cobertura="Compras federais (UASG/órgãos federais) + séries históricas",
        metodo="REST/JSON + OpenAPI (77 endpoints)",
        auth="Nenhuma / token para ALICE",
        status="VERIFICADO AO VIVO — 77 paths, v3/api-docs acessível",
        observacao=(
            "Sucessor de compras.dados.gov.br (que hoje devolve 404). Módulos: "
            "contratações, contratos, ARP, fornecedor (filtro por CNAE!), "
            "pesquisa de preço, OCDS, materiais/serviços. OCDS permite cruzar "
            "com padrão internacional."
        ),
        tags=["federal", "gratuita", "ocds"],
    ),
    Fonte(
        nome="PNCP — API de Consulta (contratações)",
        url="https://pncp.gov.br/api/consulta/v1/contratacoes/proposta",
        cobertura="Todas as esferas que publicam no PNCP",
        metodo="REST/JSON",
        auth="Nenhuma",
        status="VERIFICADO AO VIVO — 9.831 editais com prazo em 8 dias (mod. 6)",
        observacao=(
            "Endpoint ideal para 'o que vence nos próximos N dias'. "
            "Exige codigoModalidadeContratacao (5, 6, 7 confirmados)."
        ),
        tags=["primaria", "prazo", "gratuita"],
    ),
    # ------------------------------------------------------------- ESTADUAIS
    Fonte(
        nome="BEC/SP — Bolsa Eletrônica de Compras",
        url="https://www.bec.sp.gov.br",
        cobertura="Estado de São Paulo (maior PIB do país)",
        metodo="Portal/HTML",
        auth="Cadastro para propor",
        status="Online (HTTP 200) — sem API pública documentada",
        observacao=(
            "Grande volume. Publica também no PNCP, então a cobertura PNCP já "
            "captura a maior parte. Integração direta exige scraping/credencial."
        ),
        tags=["estadual", "sp", "scraping"],
    ),
    Fonte(
        nome="Portal de Compras Públicas",
        url="https://www.portaldecompraspublicas.com.br",
        cobertura="Agregador multi-esfera (municípios e estados)",
        metodo="Portal/HTML",
        auth="Cadastro",
        status="Online (HTTP 200) — privado, sem API pública",
        observacao=(
            "Agregador privado. Verificar termos de uso ANTES de automatizar. "
            "Alternativa legítima: receber os editais via e-mail e parsear."
        ),
        tags=["agregador", "privado", "termos-de-uso"],
    ),
    Fonte(
        nome="Licitações-e (Banco do Brasil)",
        url="https://www.licitacoes-e.com.br",
        cobertura="Multi-esfera, forte em municípios",
        metodo="Portal/HTML",
        auth="Cadastro + certificado",
        status="HTTP 403 para requisição automatizada",
        observacao="Bloqueia acesso automatizado — requer navegador real ou acordo formal.",
        tags=["agregador", "bloqueado"],
    ),
    # ------------------------------------------------------------ AUXILIARES
    Fonte(
        nome="Diário Oficial da União — consulta",
        url="https://www.in.gov.br/consulta/-/buscar/dou",
        cobertura="Atos federais; editais publicados em DOU",
        metodo="REST/JSON interno",
        auth="Nenhuma",
        status="A verificar — endpoint interno JSON",
        observacao="Complementa o PNCP para publicações legais e avisos.",
        tags=["auxiliar"],
    ),
    Fonte(
        nome="Dados Abertos — Portal Brasileiro",
        url="https://dados.gov.br",
        cobertura="Catálogo de datasets de licitações",
        metodo="CKAN API",
        auth="Nenhuma",
        status="A verificar",
        observacao="Fonte de datasets em CSV para backfill histórico e treino do matcher.",
        tags=["auxiliar", "historico"],
    ),
    Fonte(
        nome="TCE/TCU — portais de transparência estaduais",
        url="https://portal.tcu.gov.br",
        cobertura="Fiscalização; dados de contratos estaduais",
        metodo="Varia por tribunal",
        auth="Varia",
        status="A verificar por estado",
        observacao=(
            "Vários TCEs (SP, RJ, MG, RS) publicam APIs próprias. Bom para "
            "auditoria e checagem de idoneidade do órgão."
        ),
        tags=["auxiliar", "auditoria"],
    ),

    # ------------------------------------------------- PORTAIS ESTADUAIS
    Fonte(
        nome="BEC/SP — Bolsa Eletrônica de Compras",
        url="https://www.bec.sp.gov.br",
        cobertura="Estado de São Paulo (maior PIB do país)",
        metodo="SPA/HTML — backend responde JSON mas endpoint não é público",
        auth="Cadastro para propor",
        status="Online. API testada e NÃO encontrada (404 em /BEC_API/API/...)",
        observacao=(
            "Já publica no PNCP (campo usuarioNome revela o sistema de origem). "
            "Integração direta exigiria engenharia reversa do frontend."
        ),
        tags=["estadual", "sp", "sem-api-publica"],
    ),
    Fonte(
        nome="Compras RJ",
        url="https://www.compras.rj.gov.br",
        cobertura="Estado do Rio de Janeiro",
        metodo="SPA/HTML",
        auth="Cadastro",
        status="/api devolve o index.html (SPA) — sem API pública",
        observacao="Publica no PNCP.",
        tags=["estadual", "rj", "sem-api-publica"],
    ),
    Fonte(
        nome="Compras MG",
        url="https://www.compras.mg.gov.br",
        cobertura="Estado de Minas Gerais",
        metodo="HTML",
        auth="Cadastro",
        status="Online. /api e /compraaberta/api devolvem 404",
        observacao="Publica no PNCP.",
        tags=["estadual", "mg", "sem-api-publica"],
    ),
    Fonte(
        nome="Portal de Compras SC",
        url="https://www.portaldecompras.sc.gov.br",
        cobertura="Estado de Santa Catarina",
        metodo="SPA (Vue/Vite) com backend Spring Boot",
        auth="Cadastro",
        status="/api devolve erro JSON Spring, mas /v3/api-docs cai no index.html",
        observacao=(
            "Tem backend Java ativo (resposta JSON de erro), mas a documentação "
            "não está exposta no caminho padrão. Candidato a investigação se "
            "SC for prioridade."
        ),
        tags=["estadual", "sc", "investigar"],
    ),
    Fonte(
        nome="Comprasnet BA / Compras RS / Compras MS",
        url="https://www.comprasnet.ba.gov.br",
        cobertura="Bahia, Rio Grande do Sul, Mato Grosso do Sul",
        metodo="HTML/SPA",
        auth="Cadastro",
        status="Online, sem /api pública (404)",
        observacao="Todos publicam no PNCP.",
        tags=["estadual", "sem-api-publica"],
    ),
    Fonte(
        nome="Portais estaduais sem resposta",
        url="licitacoes.pr.gov.br, compras.go.gov.br, compras.mt.gov.br, compras.pe.gov.br, compras.ce.gov.br, compras.pa.gov.br",
        cobertura="PR, GO, MT, PE, CE, PA",
        metodo="—",
        auth="—",
        status="DNS/host não resolve (HTTP 000) — URL provavelmente mudou",
        observacao=(
            "Os estados usam domínios variados (ex.: compras.pr.gov.br). "
            "Não testado individualmente; o valor de integrar é baixo porque "
            "todos publicam no PNCP."
        ),
        tags=["estadual", "url-a-confirmar"],
    ),
    # --------------------------------------------------------- AGREGADORES
    Fonte(
        nome="Portal de Compras Públicas",
        url="https://www.portaldecompraspublicas.com.br",
        cobertura="Agregador multi-esfera (municípios e estados)",
        metodo="HTML/SPA — sem RSS (/rss, /feed, /rss.xml todos 404)",
        auth="Cadastro",
        status="Online. Sem API nem feed. Objetos aparecem no PNCP com o prefixo '[Portal de Compras Públicas]'",
        observacao=(
            "Aparece 33 vezes nos nossos objetos. Verificar termos de uso antes "
            "de qualquer automação."
        ),
        tags=["agregador", "privado", "sem-api"],
    ),
    Fonte(
        nome="Licitações-e (Banco do Brasil)",
        url="https://www.licitacoes-e.com.br",
        cobertura="Multi-esfera, forte em municípios",
        metodo="—",
        auth="Cadastro + certificado",
        status="HTTP 403 para requisição automatizada",
        observacao="Bloqueia acesso programático desde a raiz.",
        tags=["agregador", "bloqueado"],
    ),
    Fonte(
        nome="Licitanet",
        url="https://www.licitanet.com.br",
        cobertura="Agregador municipal",
        metodo="—",
        auth="Cadastro",
        status="HTTP 403 para requisição automatizada",
        observacao="Aparece 22 vezes nos nossos objetos (prefixo '[LICITANET]').",
        tags=["agregador", "bloqueado"],
    ),
    Fonte(
        nome="BBMNET",
        url="https://www.bbmnet.com.br",
        cobertura="Municípios (Banco do Brasil)",
        metodo="RSS disponível — mas é o BLOG, não editais",
        auth="Cadastro",
        status="RSS em /rss responde 200, porém com 10 notícias institucionais",
        observacao=(
            "Verificado: o feed não traz licitações. Inútil para prospecção."
        ),
        tags=["agregador", "rss-inutil"],
    ),
    Fonte(
        nome="Outros sistemas municipais",
        url="BLL Compras, Licitar Digital, Fiorilli, IPM, Betha, Megasoft, Elotech, ECustomize, Abase…",
        cobertura="Municípios e estados",
        metodo="HTML/SPA",
        auth="Varia",
        status="Maioria sem API pública; alguns com 403",
        observacao=(
            "Não integre estes individualmente — o PNCP já os agrega. "
            "Ver a análise de cobertura em docs/FONTES-E-PLANO.md §2.4."
        ),
        tags=["municipal", "nao-integrar"],
    ),
    # ------------------------------------------------------ FEDERAIS AUX.
    Fonte(
        nome="Portal da Transparência — API de Dados",
        url="https://api.portaldatransparencia.gov.br/api-de-dados",
        cobertura="Execução orçamentária federal, convênios, benefícios",
        metodo="REST/JSON",
        auth="Chave de API (cadastro gratuito)",
        status="HTTP 401 sem chave — 'Chave de API não informada'",
        observacao=(
            "Tem endpoint de licitações. Com chave, é fonte complementar "
            "valiosa para cruzar com despesa executada."
        ),
        tags=["federal", "requer-chave", "historico"],
    ),
    Fonte(
        nome="Compras.gov.br — OCDS",
        url="https://dadosabertos.compras.gov.br/modulo-ocds/1_releases",
        cobertura="Compras federais em padrão Open Contracting",
        metodo="REST/JSON (OCDS 1.1)",
        auth="Nenhuma",
        status="VERIFICADO — devolve pacote OCDS 1.1 válido, filtrado por buyerID",
        observacao=(
            "Requer buyerID (BR-CNPJ-...) e janela de datas. Interoperável com "
            "ferramentas internacionais de análise de contratações."
        ),
        tags=["federal", "ocds", "gratuita"],
    ),
    Fonte(
        nome="Diário Oficial da União — consulta",
        url="https://www.in.gov.br/consulta",
        cobertura="Atos federais; editais publicados em DOU",
        metodo="HTML (endpoint JSON interno não respondeu)",
        auth="Nenhuma",
        status="Página online; endpoint JSON testado sem resposta (HTTP 000)",
        observacao="Extrair requer scraping do HTML da consulta.",
        tags=["auxiliar", "scraping"],
    ),
    Fonte(
        nome="dados.gov.br (CKAN)",
        url="https://dados.gov.br",
        cobertura="Catálogo de datasets públicos",
        metodo="CKAN API",
        auth="Requer autenticação (verificado: HTTP 401 na API de busca)",
        status="API de busca devolve 401 sem credencial",
        observacao="Interessante para backfill histórico, mas exige cadastro.",
        tags=["auxiliar", "requer-chave"],
    ),
    Fonte(
        nome="TCU / TCEs",
        url="https://portal.tcu.gov.br",
        cobertura="Fiscalização e auditoria",
        metodo="Varia por tribunal",
        auth="Varia",
        status="Portal online; api.tcu.gov.br e sistemas.tcu.gov.br/api devolvem 404",
        observacao=(
            "Não há API pública unificada. Útil para idoneidade do órgão, "
            "não para descobrir editais."
        ),
        tags=["auxiliar", "auditoria"],
    ),
]
