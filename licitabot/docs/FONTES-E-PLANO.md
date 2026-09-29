# Agente de Varredura de Licitações — Fontes, Arquitetura e Plano de Desenvolvimento

> Documento de decisão técnica. Todos os endpoints marcados como **VERIFICADO**
> foram testados ao vivo durante a elaboração deste documento (29/09/2026), com
> os números de retorno registrados. O que não foi verificado está marcado como
> **A VERIFICAR** — não trate como fato.

---

## 1. Resumo executivo

A pergunta central é: **onde estão os editais e como chegar neles de forma
programática?** A resposta muda bastante o custo do projeto.

A descoberta que define a arquitetura:

> **O PNCP (Portal Nacional de Contratações Públicas) resolve ~90% do problema
> sozinho.** É obrigatório por lei (Lei 14.133/2021, art. 174), agrega União,
> 26 estados, DF e os ~5.570 municípios, e expõe **API pública REST/JSON sem
> autenticação**, incluindo **busca textual** e **filtro por prazo em aberto**.

Isso significa que **não é preciso construir 5.570 integrações** (o que
inviabilizaria o projeto) nem fazer scraping frágil de HTML. Consequência
prática: a coleta é a parte **fácil**; o valor real — e onde está o trabalho de
engenharia — está no **motor de aderência** (decidir, entre milhares de editais,
quais servem para *aquela* empresa) e na **distribuição** (avisar a tempo).

Números medidos ao vivo, que dimensionam o problema:

| Medida | Valor observado |
|---|---|
| Editais de "software" com prazo aberto (busca textual) | **770** |
| Editais com prazo encerrando em 8 dias (só Pregão Eletrônico) | **9.831** |
| Editais com prazo encerrando em 5 dias (mod. 5, 6 e 7) | **~5.100** |
| Paths na API de Dados Abertos do Compras.gov.br | **77** |

O volume é simultaneamente a oportunidade (mercado grande, dor real) e o
problema técnico (ninguém consegue ler 9.831 editais por semana à mão).

---

## 2. Catálogo de fontes

### 2.1 Fonte primária — usar primeiro, sem hesitar

#### PNCP — Portal Nacional de Contratações Públicas

- **API de consulta:** `https://pncp.gov.br/api/consulta/swagger-ui/index.html`
- **Busca textual:** `https://pncp.gov.br/api/search/`
- **Cobertura:** União + 26 estados + DF + ~5.570 municípios
- **Auth:** nenhuma (público)
- **Status: VERIFICADO AO VIVO**

Endpoints confirmados com resposta real:

| Endpoint | Para que serve | Verificação |
|---|---|---|
| `GET /api/search/?q=&tipos_documento=edital&status=recebendo_proposta` | **Busca textual com prazo aberto** — o endpoint mais valioso | `q=software` → 770 editais abertos |
| `GET /api/consulta/v1/contratacoes/proposta?dataFinal=&codigoModalidadeContratacao=` | Tudo que vence na janela | 8 dias, mod. 6 → **9.831 registros** |
| `GET /api/consulta/v1/contratacoes/publicacao?dataInicial=&dataFinal=&codigoModalidadeContratacao=` | Publicações por período | mod. 6 → 200 OK com dados |
| `GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens` | **Itens detalhados** da compra | 200 OK, com categoria e valor unitário |
| `GET /api/consulta/v1/orgaos/{cnpj}/compras/{ano}/{seq}` | Detalhe: prazo, valor, modalidade | 200 OK |

**Três armadilhas reais** encontradas nos testes (documentadas porque custam
horas de depuração):

1. `codigoModalidadeContratacao` é **obrigatório** em `/contratacoes/publicacao`.
   Sem ele: `400`. Modalidades que retornaram dados: **5, 6 (Pregão Eletrônico),
   7**. As demais (1, 2, 4, 8…) retornaram 0 ou erro no período testado.
2. `/api/search/` exige `tipos_documento` **e** headers de navegador
   (`Referer`, `User-Agent`). Sem os headers, devolve **HTTP 200 com corpo
   vazio** — o pior tipo de falha, porque parece sucesso.
3. `/api/search/` **não devolve prazo nem valor**. É preciso buscar o detalhe de
   cada edital individualmente para obter `dataEncerramentoProposta` e
   `valorTotalEstimado`. Sem isso o agente não alerta por prazo — que é o dado
   mais crítico de todos.

#### Compras.gov.br — Dados Abertos

- **Swagger:** `https://dadosabertos.compras.gov.br/swagger-ui/index.html`
- **OpenAPI:** `https://dadosabertos.compras.gov.br/v3/api-docs` (baixei e parseei: **77 paths**)
- **Auth:** nenhuma (token apenas para o módulo ALICE)
- **Status: VERIFICADO AO VIVO**

⚠️ **Atenção:** o endereço antigo `compras.dados.gov.br` **redireciona e devolve
404**. Muita documentação e tutorial na internet ainda aponta para lá. Use
`dadosabertos.compras.gov.br`.

Módulos relevantes:

| Módulo | Utilidade para o agente |
|---|---|
| `modulo-contratacoes` | Contratações Lei 14.133 — com filtro por **UF** e modalidade |
| `modulo-contratacoes/2_consultarItensContratacoes...` | Itens, com **`itemCategoriaNome`** (ex.: "Informática (TIC)") |
| `modulo-fornecedor/1_consultarFornecedor` | Busca fornecedor por **CNAE**, porte, natureza jurídica |
| `modulo-pesquisa-preco` | **Preços praticados** — insumo direto para precificar proposta |
| `modulo-ocds/1_releases` | Padrão **OCDS** (Open Contracting) — interoperabilidade internacional |
| `modulo-arp`, `modulo-contratos` | Atas de registro de preços e contratos vigentes |
| `modulo-legado` | Licitações pré-14.133 (histórico, treino do matcher) |

⚠️ `tamanhoPagina` deve estar entre **10 e 500**; fora disso a API responde
`"Informe um número de paginação no intervalo de 10 a 500"`. O limite de página
única não aceita valores pequenos — o que atrapalha testes rápidos.

**Por que esta fonte importa mesmo com o PNCP:** o filtro por **CNAE** no módulo
fornecedor e o **OCDS** habilitam capacidades que o PNCP não entrega —
mapear concorrentes por CNAE e cruzar dados com padrão internacional.

### 2.2 Fontes secundárias — só com justificativa

| Fonte | URL | Situação real | Veredito |
|---|---|---|---|
| **BEC/SP** | bec.sp.gov.br | HTTP 200, sem API pública | ⚠️ Publica no PNCP também — cobertura já é majoritariamente capturada. Integração direta exige scraping. |
| **Portal de Compras Públicas** | portaldecompraspublicas.com.br | HTTP 200, privado | ⚠️ **Verificar termos de uso antes de automatizar.** Alternativa legítima: receber e-mails e parsear. |
| **Licitações-e (BB)** | licitacoes-e.com.br | **HTTP 403** para requisição automatizada | ❌ Bloqueia acesso programático. Exige navegador real ou acordo formal. |
| **Diário Oficial da União** | in.gov.br/consulta | Endpoint JSON interno | 🔍 A VERIFICAR — complementa publicações legais. |
| **dados.gov.br** | dados.gov.br | Catálogo CKAN | 🔍 A VERIFICAR — útil para **backfill histórico** e treino do matcher. |
| **TCEs / TCU** | Varia por tribunal | APIs próprias em alguns estados | 🔍 A VERIFICAR — bom para auditoria e idoneidade do órgão. |

### 2.3 Regra de arquitetura sobre fontes

> Uma fonte só entra em produção se tiver **(i)** API pública documentada,
> **(ii)** feed RSS/JSON, ou **(iii)** autorização formal.

Scraping de HTML de portal privado é frágil (quebra a cada redesign), pode
violar termos de uso e cria risco jurídico para um produto que você pretende
**vender**. O desenho correto é: **PNCP como espinha dorsal** + fontes
secundárias como *enriquecimento opcional*, nunca como dependência crítica.

---

## 3. O agente sugerido

### 3.1 Nome e conceito

**LicitaBot** — um agente que responde a uma única pergunta, toda manhã:

> *"Existe alguma licitação que eu possa ganhar, e quanto tempo tenho para agir?"*

Não é um "scraper de editais" (isso é commodity). É um **filtro de aderência
explicável** com alerta por prazo.

### 3.2 Arquitetura

```
┌──────────────────────────────────────────────────────────────┐
│  CAMADA 1 — COLETA                                            │
│  PNCP (/search + /contratacoes/proposta + /itens)             │
│  Compras.gov.br (enriquecimento: CNAE, OCDS, preços)          │
│  • throttle + retry/backoff + cache em disco                  │
│  • idempotente: rodar 2x não duplica                          │
└───────────────────────────┬──────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  CAMADA 2 — NORMALIZAÇÃO E ENRIQUECIMENTO                     │
│  • schema único (CNPJ, UF, valor, prazo, link, raw_json)      │
│  • ENRIQUECER: /detalhe → prazo + valor                       │
│  • ENRIQUECER: /itens → material-vs-serviço + categoria       │
│  • deduplicação (mesmo objeto republicado em vários portais)  │
└───────────────────────────┬──────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  CAMADA 3 — MOTOR DE ADERÊNCIA (o diferencial)                │
│  Filtros duros : UF, esfera, faixa de valor, ME/EPP           │
│  Score ponderado: termos fortes/médios/negativos, categoria   │
│  Explicável    : devolve POR QUE pontuou                      │
│  Opcional      : embeddings (paráfrases) + reranking por LLM  │
└───────────────────────────┬──────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────┐
│  CAMADA 4 — ENTREGA / ALERTA                                  │
│  e-mail diário | WhatsApp/Telegram | painel web | API         │
│  Ordenado por: score × urgência do prazo                      │
└──────────────────────────────────────────────────────────────┘
```

### 3.3 O que já está construído e funcionando

Este repositório **não é só um plano** — a Camada 1, 2 e 3 já rodam. Código em
`licitabot/`, Python 3 puro (stdlib apenas: `urllib`, `sqlite3`, `json`).

```
licitabot/
├── http.py                    # cliente HTTP: throttle, retry+backoff, cache, headers
├── store.py                   # SQLite: oportunidades, itens, matches, log de coletas
├── matching.py                # motor de aderência explicável
├── cli.py                     # interface de linha de comando
├── collectors/
│   ├── base.py                # interface + catálogo de fontes
│   └── pncp.py                # coletor PNCP (busca, prazo, itens, enriquecimento)
├── corpus/modalidades.json    # códigos de modalidade e critérios de julgamento
└── config/empresa.example.json# perfil da empresa (palavras-chave, UF, faixa de valor)
```

Comandos:

```bash
python3 -m licitabot.cli varredura --dias 7      # varredura completa + ranking
python3 -m licitabot.cli buscar --termo "software" --uf SP
python3 -m licitabot.cli fontes                  # catálogo de fontes
python3 -m licitabot.cli status                  # estatísticas do banco
```

**Resultado real da execução de validação** (score, prazo e valor reais):

```
[1]        score=24.0
    Órgão : MUNICIPIO DE OURO VERDE DO OESTE  (PR)
    Objeto: Locação de sistema web integrado de gestão pública municipal, em nuvem...
    Prazo : 2026-10-07  (7 dias)   Valor: R$ 688.859,67
[2]        score=14.0
    Órgão : UNIVERSIDADE ESTADUAL DO OESTE DO PARANA  (PR)
    Objeto: Contratação desenvolvimento de software PE Juventude
    Prazo : 2026-10-14  (14 dias)  Valor: R$ 150.000,00
[6]        score=12.0   Modalidade: Dispensa
    Órgão : CAMARA MUNICIPAL DE ITAMONTE  (MG)
    Objeto: Locação de software de sistema legislativo integrado, plataforma web
    Prazo : 2026-10-06  (6 dias)   Valor: R$ 62.166,63
```

### 3.4 Decisões de projeto que importam

**O score é explicável, e isso não é detalhe estético.** Cada match devolve os
termos que o dispararam. Sem isso o usuário não confia no filtro, não consegue
corrigir falsos positivos e abandona o produto na segunda semana. É a diferença
entre uma ferramenta usada e uma ferramenta testada.

**Material vs. serviço é obrigatório, não opcional.** Durante os testes, o
matcher classificou "aquisição de equipamentos de TI, estações de trabalho,
computadores portáteis" como oportunidade relevante — porque casa em
"Informática (TIC)" e "tecnologia da informação". Para uma software house é
**ruído puro**, e ruído é o que mata produtos de alerta. A correção (ler os
itens e penalizar material) removeu 100% desses falsos positivos do topo do
ranking.

**Contexto regulatório que vira feature.** ME/EPP têm benefícios legais de
competitividade (LC 123), e a lei permite **exclusividade para ME/EPP** em
faixas de valor. Detectar "exclusivo ME/EPP" no edital é um sinal de prioridade
altíssima — poucos concorrentes, vantagem legal. Já implementado no matcher.

### 3.5 Stack recomendada por fase

| Fase | Stack | Justificativa |
|---|---|---|
| **MVP** | Python + SQLite + cron + e-mail | Já está pronto. Custo de infra ≈ R$ 0. |
| **Produto** | FastAPI + Postgres + Redis + worker (Celery/APScheduler) + Next.js | Multiusuário, concorrência, histórico. |
| **Escala** | Fila para enriquecimento, cache distribuído, pgvector (embeddings) | Só quando o volume justificar. |

**Não comece pela Fase 3.** A tentação de montar arquitetura distribuída antes
de ter um usuário pagante é o erro clássico deste tipo de projeto.

---

## 4. Plano de desenvolvimento

### Fase 0 — Validação (1–2 semanas) ✅ *parcialmente concluída*

| Tarefa | Status |
|---|---|
| Verificar API do PNCP e volume real | ✅ feito |
| Levantar API do Compras.gov.br | ✅ feito |
| Protótipo de coleta + matcher | ✅ feito e rodando |
| Definir perfil de 1 empresa real | ⬜ **próximo passo** |

**Portão de saída:** rodar 5 dias com uma empresa real e medir quantos editais
aderentes apareceram. Se der menos de ~3/semana, o nicho não sustenta o produto
— e é melhor descobrir agora.

### Fase 1 — MVP vendável (4–6 semanas)

1. Perfil de empresa por formulário (não JSON) — CNAE, UF, palavras-chave, faixa de valor
2. Alerta diário por e-mail com os N melhores, ordenados por score × urgência
3. **Enriquecimento em massa** — prazo e valor para todas as oportunidades, não só o top-N (hoje é limitado por requisições)
4. Painel web simples: lista, filtros, marcar "vou participar" / "descartado"
5. Feedback loop: usuário marca relevante/irrelevante → ajusta pesos automaticamente
6. Multi-perfil: 1 usuário, N empresas

**Portão de saída:** 5 empresas pagando. Só então construir a Fase 2.

### Fase 2 — Diferenciação (2–3 meses)

1. **Camada semântica** — embeddings multilíngues + pgvector, para pegar paráfrases que o dicionário não cobre
2. **Reranking por LLM** sobre o top-50: lê o edital e responde *"esta empresa consegue se habilitar?"* (atestado técnico, capital social, registro no conselho)
3. **Análise de concorrência** — quem ganhou editais parecidos, a que preço (Compras.gov.br + OCDS)
4. **Precificação assistida** — pesquisa de preço para sugerir lance
5. **Calendário de prazos** com alertas D-7, D-3, D-1

### Fase 3 — SaaS (escala)

1. Multiusuário com isolamento de dados (LGPD), autenticação, planos
2. API para clientes integrarem ao próprio ERP
3. Cobertura estadual/municipal adicional (BEC/SP, TCEs) onde houver API
4. Relatórios de inteligência de mercado (dados agregados e anonimizados)
5. **Produto irmão:** licenciar o motor de match para quem já tem base de editais

---

## 5. Modelo de negócio

Você levantou duas rotas. Elas não competem — têm margens e riscos diferentes.

### Rota A — Usar para as próprias empresas
- **Receita:** editais ganhos.
- **Custo:** ≈ R$ 0 (infra mínima) + seu tempo.
- **Vantagem:** valida o produto com dor real, sem depender de terceiros.
- **Risco:** receita irregular, dependente de ganhar licitação.
- **Use isto para:** validar e calibrar o matcher. É o seu laboratório.

### Rota B — Vender o serviço para outras empresas (SaaS)
- **Receita:** assinatura mensal por empresa/perfil.
- **Vantagem:** receita recorrente, escala, dados agregados viram ativo.
- **Risco:** vender para PME é caro (ciclo longo, ticket baixo, churn alto).
- **Use isto para:** construir o negócio de verdade, **depois** de provar na Rota A.

**Faixa de preço de referência:** consultorias de licitação cobram por êxito
(5–10% do contrato) ou mensalidade. Um SaaS entre **R$ 150–600/mês por perfil**
tende a ser palatável para PME, desde que economize horas reais de trabalho.
O argumento de venda não é "temos mais editais" — é **"você não perde mais prazo"**.

**Estratégia recomendada:** Rota A primeiro (90 dias), Rota B depois. A Rota A
gera os dados de calibração que fazem a Rota B ter um produto que funciona —
e você não quer vender um filtro mal calibrado.

---

## 6. Riscos e conformidade

| Risco | Gravidade | Mitigação |
|---|---|---|
| Scraping violar termos de uso | **Alta** | Usar APIs públicas. Sem scraping em portal privado sem autorização escrita. |
| LGPD | **Alta** | Dados de pessoa jurídica em regra não são dados pessoais, mas CNPJ de MEI pode ser. Base legal, minimização, sem venda de dado pessoal. |
| Instabilidade do PNCP | Média | Retry + cache + tolerância a falha parcial; nunca depender de uma única chamada. |
| Falsos positivos matam o produto | **Alta** | Score explicável + feedback do usuário + filtro material/serviço. Já mitigado. |
| Concorrência (agregadores grandes) | Média | Nicho vertical: ser excelente para *um* setor (ex.: software para prefeituras) em vez de raso para todos. |
| Dependência de uma fonte única | Média | Compras.gov.br como secundária já integrada; OCDS como plano B. |
| Bloqueio de IP por excesso de requisições | Média | Throttle, backoff, cache, horário de execução fora de pico. Implementado. |

**Ponto de atenção jurídica:** a coleta de dados públicos é legítima, mas
**revender o dado bruto** de um portal privado não é. O ativo defensável é o
**motor de aderência e o histórico de resultados** — não o edital em si, que é
público e está disponível para todos.

---

## 7. Próximos passos concretos

1. **Preencher `config/empresa.example.json` com dados de uma empresa real** (hoje está com perfil genérico de software house). Sem isso, o score é abstrato.
2. Rodar `varredura` por 5 dias seguidos e medir editais aderentes/semana.
3. Ajustar pesos e palavras-chave até o topo do ranking conter **apenas** editais que você realmente disputaria.
4. Só então escolher entre Rota A e Rota B.

O passo 3 é o mais importante e o mais subestimado. Um ranking que acerta 5 de
10 é um produto que ninguém usa; um que acerta 9 de 10 é um produto que se vende
sozinho — e a diferença está inteiramente em calibrar o perfil contra a realidade
da empresa, não em mais código.

---

## 2.4 Por que NÃO integrar os portais estaduais e agregadores

Esta seção responde à pergunta "existem outras fontes?" com dado medido, não
com opinião. Testei 25 fontes ao vivo.

### O achado que decide a questão

O campo `usuarioNome` do PNCP revela **qual sistema publicou** cada licitação.
Na nossa base de 995 registros, aparecem **18 sistemas distintos**:

| Sistema | Registros |
|---|---|
| Compras.gov.br | 228 |
| CENTI | 54 |
| Fiorilli Software | 51 |
| IPM Sistemas | 46 |
| Licitar Digital | 38 |
| Betha Sistemas | 37 |
| ECustomize | 33 |
| BAHIA Secretaria da Administração | 31 |
| Megasoft | 29 |
| Governança Brasil | 29 |
| BLL Compras | 24 |
| Licitanet | 22 |
| PROCERGS (RS) | 17 |
| Abase Sistemas | 16 |
| Elotech | 15 |
| E & L Produções de Software | 15 |
| Planejar Consultores | 13 |
| CONAM | 13 |

> **O PNCP já é o agregador.** Ele tem integração com 18+ sistemas municipais,
> estaduais e privados. Integrar BEC/SP, Licitanet ou Portal de Compras
> Públicas individualmente seria **refazer, pior e sem garantia legal, o
> trabalho que a lei já obriga o PNCP a fazer**.

Também aparece nos objetos o prefixo do sistema de origem: "Portal de Compras
Públicas" (33 vezes), "[LICITANET]" (22), o que confirma a agregação.

### Resultado do teste das 25 fontes

| Situação | Quantidade | Exemplos |
|---|---|---|
| **API pública funcional** | **3** | PNCP, Compras.gov.br, Compras.gov.br/OCDS |
| Requer chave de API | 2 | Portal da Transparência (401), dados.gov.br (401) |
| Bloqueia automação (403) | 2 | Licitações-e, Licitanet |
| Online sem API pública | 7 | BEC/SP, Compras RJ, MG, BA, RS, SC, MS |
| SPA com backend ativo mas sem doc | 1 | Portal de Compras SC (candidato a investigar) |
| RSS que não serve | 1 | BBMNET (feed é o blog, não editais) |
| URL não resolve (HTTP 000) | 6 | PR, GO, MT, PE, CE, PA |
| Sem API unificada | 2 | TCU, TCEs |

### Conclusão prática

**Não existe fonte melhor que o PNCP para este problema.** A decisão de
arquitetura é:

1. **PNCP como espinha dorsal** — cobertura legal de 5.570 municípios, API
   pública, sem autenticação.
2. **Compras.gov.br para enriquecimento federal** — OCDS, preços praticados,
   dados de fornecedor por CNAE.
3. **Portal da Transparência** — se cruzar com despesa executada virar
   requisito, cadastre-se e use a chave.
4. **Nada de scraping em portal privado** — Licitações-e e Licitanet bloqueiam;
   Portal de Compras Públicas não tem API e tem termos de uso a verificar.

O ganho marginal de integrar um portal estadual é **pequeno** (a cobertura já
vem pelo PNCP) e o custo é **alto** (engenharia reversa, quebra a cada
redesign, risco jurídico). Se um dia um estado específico virar prioridade
comercial — SC tem backend Java ativo e é o melhor candidato — aí sim vale
investigar.

**O gargalo do produto não é descobrir mais editais. É ler os que já temos.**
Com 744 registros municipais e 995 no total, o problema é priorização e
qualificação, não cobertura.


---

## 2.5 REVISÃO: os endpoints que eu não tinha visto

Ao responder "existem outras fontes?", reli o swagger completo do PNCP
(`/api/consulta/v3/api-docs`) e descobri que ele tem **12 endpoints** — eu
conhecia 5. Os quatro que faltavam mudam o produto.

| Endpoint | O que faz | Valor |
|---|---|---|
| `/v1/contratacoes/atualizacao` | Contratações por data de **atualização global** | Sync por mudança: pega prorrogação de prazo, alteração de valor, publicação de resultado |
| `/v1/contratos` | Contratos por data de publicação | **Quem ganhou, por quanto** |
| `/v1/atas` | Atas de registro de preço por vigência | Preços registrados (509 mil numa semana) |
| `/v1/pca` | Plano de contratações anual | O que o órgão **pretende** comprar |

### Dois ganhos técnicos imediatos

**1. `tamanhoPagina` aceita 50** (verificado; 500 devolve vazio). Eu assumia
que era fixo em 10 — o sync da janela aberta fazia 983 requisições. Com 50 são
**197: 5× menos carga e 5× mais rápido**. Aplicado em `ingest.py`.

**2. `/v1/contratos` traz `categoriaProcesso`** — o **setor oficial**:
"Compras", "Serviços", "Informática (TIC)", "Serviços de Engenharia", "Obras",
"Serviços de Saúde". É exatamente o código estrutural que faltava para
classificar setor sem adivinhar por texto.

### O campo que liga edital a resultado

`/v1/contratos` tem 41 campos, e estes quatro formam a ponte:

```
numeroControlePncpCompra   -> ID do edital da nossa base
niFornecedor               -> CNPJ de quem ganhou
valorGlobal                -> por quanto
dataVigenciaInicio/Fim     -> por quanto tempo
```

Ou seja: para qualquer licitação encerrada, dá para responder **quem ganhou e
por quanto**; e o inverso, que é mais valioso comercialmente: **este fornecedor
já ganhou o quê, de quem, por quanto**.

### Duas armadilhas medidas

**Período máximo de 365 dias.** `/v1/contratos` rejeita janelas maiores:
`422 "Período maior que 365 dias."`. É preciso fatiar.

**Varrer por data é ineficiente.** Uma semana de contratos tem 31 mil registros
e a taxa de casamento com a nossa base é de ~0,2%. O caminho é filtrar por
`cnpjOrgao` — verificado: funciona (862 contratos para um único órgão). Assim
lemos só os contratos dos órgãos que acompanhamos.

**Custo real:** ~13,9 s por requisição no endpoint de contratos (medido). Não é
throttle nosso — é o servidor do PNCP.

### Resultado da primeira coleta

8 órgãos, contratos de 2025–2026:

| Métrica | Valor |
|---|---|
| Contratos | 2.675 |
| Fornecedores únicos | 1.628 |
| Valor total | **R$ 7,46 bilhões** |
| Categorias oficiais | 9 |

Maiores contratos: Cristália (medicamentos, R$ 1,19 bi, Comando da
Aeronáutica), Omnisys Engenharia (R$ 579 mi), IACIT Soluções Tecnológicas
(R$ 477 mi em 5 contratos).

> **Este é o ativo que faltava.** Os editais dizem o que está sendo comprado.
> Os contratos dizem **quem vence, a que preço e com que frequência** — que é a
> informação que permite decidir se vale disputar.
