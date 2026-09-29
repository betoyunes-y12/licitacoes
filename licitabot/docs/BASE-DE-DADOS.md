# Base de Dados de Licitações — Operação e Manutenção

Objetivo: manter um acervo **próprio, completo e sempre atualizado** de
licitações públicas brasileiras, **independente de perfil de empresa**. O perfil
vem depois — a base é o ativo.

---

## 1. Os dois modos de ingestão (e por que são dois)

Descobri testando ao vivo que o PNCP tem **duas portas diferentes**, com
comportamentos distintos:

| | `/contratacoes/publicacao` | `/contratacoes/proposta` |
|---|---|---|
| **Aceita data passada?** | ✅ **Sim** | ❌ Não (HTTP 422) |
| **Filtro de data** | `dataInicial` + `dataFinal` | **`dataInicial` + `dataFinal`** |
| **Para que serve** | **Backfill histórico** | **Snapshot do que está aberto** |
| **Verificado** | 31.543 editais em 1 mês (mod. 6) | 9.830 abertos em 15 dias |

O `/proposta` rejeita `dataFinal` no passado com a mensagem explícita
*"Data Final deve ser maior ou igual a data atual"*. Isso **não é limitação** —
é exatamente o que se quer para atualização contínua: ele sempre devolve a
janela viva.

**Conclusão prática:** usar `/publicacao` para construir o histórico e
`/proposta` para o sync horário. Os dois juntos dão cobertura completa.

> ⚠️ **Página fixa de 10 registros.** O PNCP não permite aumentar o
> `tamanhoPagina` nesses endpoints. Um mês de uma modalidade = ~3.155
> requisições (confirmado: `totalPaginas: 3155` para set/2026, mod. 6). É por
> isso que a ingestão precisa ser resumível e rodar em lote.

### ⚠️ O PNCP tem limite de requisições (HTTP 429)

Descoberto na prática, durante os testes: **acelerar demais derruba a coleta.**

```
HTTP 429 — "Limite de Requisições Excedido"
```

Com `--delay 0.12` (≈8 req/s) o portal bloqueou a coleta em poucos minutos. Com
`--delay 0.5` rodou 10,6 minutos contínuos, fez 89 requisições, ingeriu 995
registros, **0 erros** — e ainda assim bateu no limite 8 vezes, recuperando-se
automaticamente a cada vez.

**Três medidas tomadas no código:**

1. **Tratamento dedicado de 429** — espera longa (60 s, dobrando até 900 s)
   em vez do backoff curto usado para erros comuns. Insistir durante a janela
   de bloqueio só a prolonga.
2. **Portão de throttle compartilhado** — o cliente de consulta e o de busca
   textual usavam relógios independentes, gerando o **dobro** da taxa efetiva.
   Agora compartilham o mesmo relógio. Esta era a causa raiz.
3. **Contador de 429 por execução** — aparece em `stats['rate_limits']`, para
   você saber se está no limite antes de ser bloqueado de vez.

**Recomendação operacional:** use `--delay 0.5` ou maior. A diferença entre
0.12 s e 0.5 s é ~4× mais lento, mas 0.12 s simplesmente **não funciona** para
volumes grandes.


---

## 2. Comandos

### Construir / ampliar a base

```bash
# Backfill de um mês, todas as modalidades principais, + janela aberta
python3 -m licitabot.cli ingerir \
    --backfill 2026-08-01:2026-09-30 \
    --abertos --janela-abertos 15 \
    --modalidades principais

# Tudo (13 modalidades) — mais lento, cobertura máxima
python3 -m licitabot.cli ingerir --backfill 2026-09-01:2026-09-30 --modalidades todas

# Modalidades específicas (5=Concorrência Presencial, 6=Pregão Eletrônico, 8=Dispensa)
python3 -m licitabot.cli ingerir --backfill 2026-09-01:2026-09-30 --modalidades 6,8,12
```

| Opção | Efeito |
|---|---|
| `--backfill A:B` | período histórico (AAAA-MM-DD:AAAA-MM-DD) |
| `--abertos` | inclui snapshot da janela aberta |
| `--janela-abertos N` | dias à frente (padrão 15) |
| `--modalidades` | `principais` \| `todas` \| `6,8,12` |
| `--passo-dias N` | tamanho da janela de backfill (padrão 31) |
| `--max-paginas N` | limita páginas por bloco (para testar) |
| `--recoletar` | ignora o controle de blocos já feitos |

**A ingestão é idempotente e resumível.** Rodar duas vezes não duplica nada
(upsert por id) e blocos já concluídos são pulados — se o processo cair no meio,
basta rodar de novo.

### Atualização contínua

```bash
python3 -m licitabot.cli sync --janela 20
```

Captura a janela aberta e atualiza prazos. É este comando que roda no cron.

### Enriquecimento em lotes

```bash
python3 -m licitabot.cli --delay 0.5 enriquecer --limite 500
```

Preenche `material_ou_servico` (S/M) e `categoria_item` na base. Retomável —
rode em quantas execuções quiser. Ver §5.1.

### Consultar (sem perfil de empresa)

```bash
# Panorama da base
python3 -m licitabot.cli resumo

# Tudo aberto em SP
python3 -m licitabot.cli listar --uf SP --abertos --limite 30

# Serviços de TI acima de R$ 500 mil
python3 -m licitabot.cli listar --texto "software" --tipo S --valor-min 500000

# Dispensas eletrônicas (contratação direta — alto valor estratégico)
python3 -m licitabot.cli listar --modalidade Dispensa --abertos
```

### Exportar

```bash
# CSV (abre no Excel; separador ';' por causa da vírgula decimal pt-BR)
python3 -m licitabot.cli exportar --formato csv --destino dados/licitacoes.csv

# Só o que está aberto em SP
python3 -m licitabot.cli exportar --formato csv --destino sp_abertos.csv --uf SP --abertos

# JSONL com payload original completo (para BI / pipeline)
python3 -m licitabot.cli exportar --formato jsonl --destino tudo.jsonl --com-raw
```

O CSV inclui **três colunas de link**, que servem a propósitos diferentes:

| Coluna | O que é | Cobertura medida |
|---|---|---|
| `link` | Página da licitação no PNCP | **100%** |
| `link_sistema_origem` | Portal do órgão onde o edital costuma estar | ~49% |
| `link_processo` | Processo eletrônico (SEI e similares) | ~12% |

---

## 2.1 Baixar os editais (arquivos anexados)

Descoberta relevante: **o PNCP hospeda os arquivos**, não só aponta para eles.

```
GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}/arquivos
→ [{"titulo": "Anexo I - Termo de Referencia.pdf",
    "tipoDocumentoNome": "Termo de Referência",
    "url": "https://pncp.gov.br/pncp-api/v1/orgaos/.../arquivos/1"}]
```

O download devolve o binário real — verificado: PDF 1.7, 13 páginas, 469 KB.
Ou seja, dá para **baixar o edital inteiro automaticamente**, não só linkar.

```bash
# Ver links e documentos sem baixar
python3 -m licitabot.cli links --limite 10 --com-documentos

# Baixar os editais (prazo mais próximo primeiro)
python3 -m licitabot.cli baixar --limite 20

# Só editais, ignorando avisos e anexos
python3 -m licitabot.cli baixar --tipos "Edital" --limite 30

# Filtrar por UF e assunto
python3 -m licitabot.cli baixar --uf SP --texto "software" --limite 10
```

Organização em disco:

```
dados/editais/{UF}/{ano}/{numeroControlePNCP}/
    01-Edital.pdf
    02-Termo_de_Referencia.pdf
    03-Estudo_Tecnico_Preliminar.pdf
    _documentos.json     ← metadados originais (título, tipo, URL, data)
```

**Duas limitações reais:**

1. **Nem toda contratação tem anexo no PNCP.** Órgãos que publicam só o resumo
   e mantêm o edital no portal próprio aparecem sem arquivos. Nesse caso use
   `link_sistema_origem` — presente em ~49% dos registros e não baixável
   automaticamente, porque cada portal (BEC/SP, Licitanet, Portal de Compras
   Públicas, Compras.gov.br) tem fluxo próprio.
2. **O nome do arquivo vem sem extensão** em parte dos casos — o PNCP informa
   só o nome lógico. O downloader assume `.pdf` nesses casos. Detalhe que
   causou bug na primeira versão: truncar o nome **depois** de acrescentar o
   sufixo corta a extensão e o arquivo fica sem tipo reconhecido. Agora o
   sufixo é separado, o miolo é truncado, e só então reconcatena.

### Consulta SQL direta

```sql
-- O que vence nos próximos 3 dias
SELECT orgao, uf, objeto, data_encerramento, valor_estimado
FROM oportunidades
WHERE data_encerramento BETWEEN datetime('now') AND datetime('now', '+3 days')
ORDER BY data_encerramento;

-- Órgãos que mais compram software
SELECT orgao, uf, COUNT(*) n, SUM(valor_estimado) total
FROM oportunidades
WHERE objeto LIKE '%software%'
GROUP BY cnpj_orgao ORDER BY n DESC LIMIT 20;
```

---

## 3. Automação

### Opção A — cron (mais simples)

```bash
crontab -e
# atualiza a cada hora
0 * * * * /root/Licitações/licitabot/scripts/atualizar.sh >> /var/log/licitabot.log 2>&1
```

### Opção B — systemd timer (recomendado)

```bash
sudo cp licitabot/scripts/licitabot-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now licitabot-sync.timer
systemctl list-timers licitabot-sync.timer
journalctl -u licitabot-sync.service -f
```

Vantagens sobre o cron: log centralizado, `Restart=on-failure`, `Persistent=true`
(executa o que perdeu se a máquina estava desligada) e sobrevive a reboot.

**O script usa `flock`** — se uma execução ainda estiver rodando quando a
próxima começar, a segunda sai sem fazer nada. Isso evita dois processos
escrevendo no SQLite simultaneamente (que corromperia ou daria `database is
locked`).

---

## 4. Estrutura da base

| Tabela | Conteúdo |
|---|---|
| `oportunidades` | **55 colunas.** 1 linha por licitação: órgão, unidade, localização, modalidade, modo de disputa, instrumento, processo, objeto, valores, base legal, tipo de benefício (ME/EPP), datas, situação, links |
| `itens` | **38 colunas.** Itens detalhados: descrição, material/serviço, categoria, quantidade, unidade, valores, critério de julgamento, benefício, margem de preferência, conteúdo nacional, NCM, catálogo |
| `matches` | Ranqueamento por perfil (**opcional** — a base não depende disto) |
| `coletas` | Log de ingestão: o que foi coletado, quando, quantos registros, erros |

### Campos que valem destacar

Os mais úteis para filtro, descobertos ao varrer os payloads reais:

| Campo | Por que importa |
|---|---|
| `tipo_beneficio` | **"Participação exclusiva para ME/EPP"** — vantagem legal da LC 123, poucos concorrentes. Encontrado em ~15% dos itens analisados |
| `amparo_legal` | Base legal: `Art. 28, I` = pregão, `Art. 75` = dispensa, `Art. 79` = credenciamento |
| `modo_disputa` | Aberto, Aberto-Fechado, Dispensa Com Disputa, Fechado |
| `instrumento` | Edital ou Aviso de Contratação Direta |
| `processo` | Nº do processo administrativo — chave para consultar no portal do órgão |
| `srp` | Registro de Preços: contrato futuro, não compra imediata |
| `conteudo_nacional` | Exigência de conteúdo local (margem de preferência) |
| `existe_resultado` | Já foi homologada? Filtra o que ainda dá para disputar |
| `usuario_nome` | Sistema que publicou (Compras.gov.br, Licitanet, GOVTEC…) |
| `codigo_ibge` | Cruzamento com dados do IBGE (população, região) |

### Payloads completos preservados

Três colunas guardam o JSON **íntegro**, para consultar qualquer campo — inclusive
os que não viraram coluna, e os que o PNCP venha a adicionar no futuro:

| Coluna | Conteúdo |
|---|---|
| `raw_json` | Payload da listagem |
| `detalhe_json` | Payload do `/detalhe` — **38 campos** |
| `itens_json` | Payload dos itens — **38 campos por item** |

Isso permite consulta direta via `json_extract`, sem migração de schema:

```sql
-- origem orçamentária do dinheiro (campo sem coluna dedicada)
SELECT id, json_extract(detalhe_json, '$.fontesOrcamentarias[0].nome') AS fonte
FROM oportunidades WHERE fonte IS NOT NULL;

-- descrição completa de um item específico
SELECT json_extract(itens_json, '$[0].descricaodetalhada') FROM oportunidades
WHERE id = '18428839000190-1-000160/2026';
```

Índices em `uf`, `data_encerramento`, `data_publicacao` e `fonte` — as consultas
mais comuns são geográficas e por prazo.

### Filtros disponíveis

```bash
python3 -m licitabot.cli listar --beneficio "ME/EPP"        # exclusivo para pequenas
python3 -m licitabot.cli listar --amparo-legal "Art. 75"    # dispensas
python3 -m licitabot.cli listar --srp --tipo S              # registro de preços, serviços
python3 -m licitabot.cli listar --esfera M --uf SP --abertos
python3 -m licitabot.cli listar --modo-disputa "Aberto-Fechado"
python3 -m licitabot.cli listar --sem-resultado --abertos   # ainda dá para disputar
python3 -m licitabot.cli listar --nacional --categoria "Informática"
python3 -m licitabot.cli listar --texto "software"          # objeto + itens + processo
```

O `--texto` procura também **nas descrições dos itens**, não só no objeto. Isso
importa: muita licitação tem objeto genérico ("aquisição de bens de TI") e só
os itens revelam o que é de fato.

### Atualizar os dados de uma base já coletada

```bash
python3 -m licitabot.cli atualizar-dados --abertos --limite 100
```

Rebaixa o detalhe e os itens e re-extrai **todos** os campos. Serve para:
(i) bases coletadas por versão anterior que não extraía tudo;
(ii) pegar prorrogações de prazo, alterações de valor e publicação de resultado.


---

## 5. Dimensionamento real

Medições feitas ao vivo durante a construção:

| Medida | Valor observado |
|---|---|
| Editais/mês, só Pregão Eletrônico (mod. 6, ago/2026) | **33.822** |
| Editais/mês, mod. 6 (set/2026) | **31.543** (`totalPaginas: 3155`) |
| Abertos em 15 dias (mod. 5, 6, 7) | **~9.830** |
| Registros por requisição | 10 (fixo) |
| Requisições por mês/modalidade | ~3.155 |
| **Taxa segura de coleta** | **~2 req/s (`--delay 0.5`)** |
| 89 requisições com `--delay 0.5` | 995 registros em **10,6 min** |

**Implicação:** a base cresce ~30–40 mil registros/mês **por modalidade
relevante**. Com as 7 modalidades principais, espere algo na ordem de
**150–250 mil registros/mês**. SQLite aguenta isso com folga (o limite prático
é centenas de milhões de linhas); o gargalo real é **tempo de coleta**, não
armazenamento.

**Tempo estimado** (com `--delay 0.5`, que é a taxa que funciona de fato):

| Cenário | Requisições | Tempo |
|---|---|---|
| Snapshot de 15 dias, 1 modalidade | ~980 | ~8 min |
| Snapshot de 15 dias, todas as 13 | ~1.400 | ~12 min |
| 1 mês de backfill, 1 modalidade | ~3.155 | ~26 min |
| 1 mês de backfill, 7 modalidades | ~22.000 | ~3 h |
| 12 meses, 7 modalidades | ~264.000 | **~37 h** |

**Por isso a estratégia é:** `sync` horário (leve, ~1 min) mantém a base viva;
backfill histórico é operação de **uma vez**, rodada em lote ao longo de dias.

> **Não paralelize dentro de uma modalidade** — a paginação é sequencial e você
> só aumenta a pressão no portal. Paralelizar *entre* modalidades é possível,
> mas com o limite de requisições do PNCP o ganho é pequeno e o risco de
> bloqueio é alto. Melhor rodar mais tempo com taxa segura.

---

## 5.1 Enriquecimento de itens (job separado)

`material_ou_servico` e `categoria_item` **não vêm** nos endpoints de listagem.
Precisamos buscar `/itens` de cada oportunidade — **2 requisições por registro**.

```bash
# Lotes de 500, retomável: rode quantas vezes quiser, continua de onde parou
python3 -m licitabot.cli --delay 0.5 enriquecer --limite 500
```

Por que é um job separado e não parte da coleta: enriquecer durante a ingestão
multiplicaria o tempo por ~3 e criaria acoplamento. Separado, você decide quando
pagar esse custo — e as oportunidades já classificadas nunca são reprocessadas.

**É este passo que habilita o filtro mais útil da base:** separar *"vendo
serviço"* de *"vendo produto"*. Sem ele, uma software house recebe alerta de
"aquisição de 200 notebooks" — ruído que destrói a confiança no produto.

Nota: quando os itens não trazem categoria útil, a API devolve
`"Não se aplica"`. O campo `material_ou_servico` (S/M) é o sinal confiável.


---

## 6. Operação responsável

- **`--delay`**: padrão 0.6 s. Baixei para 0.12 no backfill em lote — é um
  portal público e gratuito; se for rodar volumes grandes com frequência,
  mantenha ≥ 0.2 s.
- **Cache em disco**: ativo por padrão; re-execuções não re-baixam o mesmo.
  Desative com `--no-cache` quando precisar de dado fresco (o `sync` já faz isso).
- **Backoff exponencial**: 4 tentativas com espera crescente. Erros 400/422 não
  são repetidos (erro de parâmetro não se resolve com retry).
- **Falha parcial não aborta**: se uma modalidade falhar, as outras continuam e
  o erro fica registrado em `coletas`.
- **Sem scraping de portal privado.** Ver `docs/FONTES-E-PLANO.md` §2.3.

---

## 7. Próximos passos sugeridos

1. **Backfill de 12 meses** — dá base para inteligência de mercado (sazonalidade,
   órgãos recorrentes, faixas de preço). Rode em lote, uma vez.
2. **Enriquecimento de itens em massa** — job separado que percorre as
   oportunidades sem `material_ou_servico` e preenche via `/itens`. Habilita
   filtro por categoria (TIC, obras, saúde…) em toda a base.
3. **Índice de texto completo** — `FTS5` do SQLite sobre `objeto`. Busca textual
   instantânea em centenas de milhares de registros, sem servidor externo.
4. **Migrar para Postgres** só quando houver múltiplos usuários escrevendo ao
   mesmo tempo. Antes disso é complexidade sem retorno.

---

## 8. Classificação setorial (rotina separada)

### Por que é separada da coleta

A classificação é **derivada**, não coletada. Mantê-la separada dá três ganhos:

1. **Reclassificar é grátis.** Melhorou a regra? Roda de novo — o insumo
   (objeto + `itens_json`) já está no banco. 995 registros levam **1,8 s** e
   zero requisições.
2. **Coleta e interpretação não se contaminam.** O coletor só traz dado; se a
   regra mudar, o dado bruto continua intacto.
3. **Permite versionar.** `VERSAO_REGRAS` diz qual versão classificou cada
   registro.

```bash
python3 -m licitabot.cli classificar                    # classifica tudo
python3 -m licitabot.cli classificar --listar-setores   # lista setores
python3 -m licitabot.cli classificar --buscar-itens --limite 200
python3 -m licitabot.cli setores                        # panorama
python3 -m licitabot.cli listar --setor tecnologia --confianca media
```

### O problema: o PNCP não tem código de setor

Verificado em 456 itens:

| Campo | Preenchimento |
|---|---|
| `ncmNbsCodigo` | **0%** |
| `catalogoCodigoItem` | **0%** |
| `categoriaItemNome` | só "Bens Móveis / Bens Imóveis / Não se aplica" |

`categoria` é **tipo patrimonial**, não setor. Não há código estrutural para
saber que uma licitação é de TI — a classificação tem de ser por texto.

### Resultado medido

Filtro ingênuo por palavra-chave: **232 candidatos**, com falsos positivos em
"sistema de repetição de sinais" (obra), "servidor público" (RH),
"drone" (agrícola) e até no **nome do fornecedor** que publicou o edital
("Fiorilli Software" num edital de merenda).

Depois da classificação:

| Métrica | Resultado |
|---|---|
| Casos de TI identificados corretamente | **6 / 6** |
| Falsos positivos em 12 casos não-TI | **0** |
| Tecnologia na base | **26** (vs 13 do filtro ingênuo) |
| Desempenho | **1,84 ms/registro** (995 em 1,8 s) |

### Cinco erros de implementação que valem registro

Cada um quebrou a classificação de um jeito diferente. Documentados porque
voltariam a acontecer:

1. **Injetar o glossário inteiro em toda descrição** (o pior). A descrição
   "aquisição de software" passou a *conter* "merenda" e "material médico", as
   exclusões bloquearam todos os setores e o classificador devolveu `None` para
   tudo.
2. **Casar frase por subconjunto.** A exclusão "sistema de abastecimento"
   casava com "aquisição de software" (bastavam "de" + "abastecimento"). Agora
   frase exige **adjacência**.
3. **Fatiar todo token por sufixo**, mutilando palavras ("gestão" → "gesta").
4. **Injetar plurais no meio da lista de tokens**, quebrando a adjacência de
   "tecnologia da informação".
5. **Adivinhar plural do português.** "informação" → "informacaos",
   "softwares" → "softwar". A solução foi parar de adivinhar: canonicalizar
   para singular **só quando o resultado existe no glossário**, e usar radical
   como índice imune a flexão.

### Limitação que você precisa conhecer

**895 dos 995 registros não têm itens publicados pelo órgão.** Nesses, a
classificação vem só do objeto — que muitas vezes é genérico ("aquisição de
materiais permanentes"). Por isso a confiança nunca sai como `alta` sem itens,
e a flag `--buscar-itens` existe para reduzir essa lacuna (1 requisição por
registro).

Distribuição de confiança hoje: `media` 605, `baixa` 144, `alta` 21.

### Integração na rotina

`scripts/atualizar.sh` já chama `classificar` ao final: é barato (sem rede) e
mantém o setor em dia a cada hora.

### Resultado do `--buscar-itens` (execução medida)

Objetivo: reduzir os registros sem setor buscando os itens no PNCP.

| Métrica | Antes | Depois |
|---|---|---|
| Sem setor | 225 | **120** |
| Confiança alta | 21 | **63** |
| Tecnologia | 26 | **30** |
| Itens buscados | — | 212 de 225 |
| Tempo | — | **~18 min** (13,4 s/licitação, medido) |

**Custo real:** o endpoint de itens do PNCP leva **13,4 s por licitação**
(medido em 5 amostras: 0,1 s a 19,5 s). Para os 995 registros seriam ~3,7 h.
Não é throttle nem rate limit — é lentidão do servidor.

### Dois becos sem saída que valem registro

Tentei duas alternativas para escapar dos 13,4 s e **ambas não funcionaram**.
Documentadas para ninguém repetir:

**1. Endpoint em lote do Compras.gov.br — rápido, mas sem código.**
`/modulo-contratacoes/2_consultarItensContratacoes` devolve 500 itens em
**0,42 s** (contra 13,4 s por 1 licitação). Parecia a solução. Mas medido: os
campos `codigoGrupo` e `codigoClasse` **vêm nulos** — 0 de 50 na amostra
inicial, 0 de 534 itens gravados. O payload *tem* os campos, o valor é que não
vem. Sem código, o endpoint em lote não serve para classificação estrutural.

**2. Classificação estrutural por código oficial — o dado não está lá.**
O módulo `taxonomia.py` mapeia as divisões oficiais (div 11-18 e 80 = TIC,
54 = construção, 92 = educação, 93 = saúde, 853 = limpeza) e está correto e
testado. Mas como o PNCP não fornece código (0% em NCM e catálogo) e o
Compras.gov.br também não devolve `codigoGrupo` no endpoint em lote, **não há
código de onde classificar** para os nossos registros. O módulo fica no
repositório como caminho preferencial para quando/se o código estiver
disponível — `setor_de_codigo()` tem prioridade sobre o texto.

**Cobertura geográfica é o limite de fundo:** 744 dos 995 registros são
**municipais**, e o Compras.gov.br cobre essencialmente o federal. Os 159
sem setor que tentamos enriquecer são majoritariamente municipais — portais
municipais não estão no Compras.gov.br. Não existe atalho para esse universo;
o PNCP é o único caminho, com o custo medido.

### Vocabulário: lacunas fechadas com dado, não com palpite

Dos 120 ainda sem setor, a maioria é **genuinamente fora de qualquer setor**
(leilão de imóveis, alienação de sucata, permissão de uso de espaço público)
ou tem descrição genérica ("aquisição de swabs"). Não é falha do classificador.

Mas encontrei lacunas reais e fechei: `iluminação pública`, `macrodrenagem`,
`atracadouro` (obras); `tinta`, `selador`, `swab` (serviços gerais);
`calibração`, `metrológica` (conformidade). Também **excluí** `alienação` e
`leilão` do setor: leilão de sucata eletrônica estava sendo classificado como
tecnologia.

### Integração na rotina

`scripts/atualizar.sh` já chama `classificar` ao final: é barato (sem rede,
3,5 ms/registro) e mantém o setor em dia a cada hora. O `--buscar-itens` é
**manual**, dado o custo de 13,4 s por licitação — rode quando quiser ampliar
a cobertura, não a cada hora.
