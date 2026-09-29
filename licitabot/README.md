# LicitaBot

Base de dados própria de licitações públicas brasileiras, **sempre atualizada**,
construída sobre as APIs públicas do PNCP — mais um motor de aderência que casa
essa base com o perfil de cada empresa.

Dois usos, nesta ordem:

1. **Acervo** — banco de dados completo e atualizado de licitações, independente
   de qualquer empresa. É o ativo.
2. **Alertas** — filtrar esse acervo para *uma* empresa e avisar antes do prazo.

## Por que isto funciona

O **PNCP** é obrigatório por lei (Lei 14.133/2021, art. 174) e agrega União,
26 estados, DF e ~5.570 municípios numa **API REST/JSON pública, sem
autenticação**. Não é preciso integrar 5.570 portais nem raspar HTML.

## Instalação

Sem dependências externas — Python 3.10+ e biblioteca padrão apenas.

```bash
python3 -m licitabot.cli fontes        # catálogo de fontes verificadas
```

## Uso

### Construir a base

```bash
# Snapshot do que está aberto (o mais valioso) — todas as modalidades
python3 -m licitabot.cli --delay 0.5 ingerir --abertos --janela-abertos 15 --modalidades todas

# Backfill histórico de um período
python3 -m licitabot.cli --delay 0.5 ingerir --backfill 2026-08-01:2026-09-30

# Atualização incremental (é isto que vai no cron)
python3 -m licitabot.cli --delay 0.5 sync --janela 20
```

### Consultar a base (sem perfil de empresa)

```bash
python3 -m licitabot.cli resumo                                    # panorama
python3 -m licitabot.cli listar --uf SP --abertos                  # aberto em SP
python3 -m licitabot.cli listar --texto "software" --tipo S        # só serviços
python3 -m licitabot.cli exportar --formato csv --destino dados/licitacoes.csv
```

### Filtrar para uma empresa

```bash
python3 -m licitabot.cli varredura --dias 7     # ranqueia por aderência ao perfil
```

## ⚠️ O PNCP tem limite de requisições

Acelerar demais derruba a coleta: **HTTP 429 "Limite de Requisições Excedido"**.
Use `--delay 0.5` ou maior. O cliente já trata 429 com espera longa (60 s
dobrando até 900 s) e mostra `rate_limits` nas estatísticas. Detalhes em
[`docs/BASE-DE-DADOS.md`](docs/BASE-DE-DADOS.md) §5.

| Taxa | Resultado |
|---|---|
| `--delay 0.12` | ❌ bloqueado pelo portal em minutos |
| `--delay 0.5` | ✅ 995 registros, 10,6 min, 0 erros |

## Comandos

| Comando | Função |
|---|---|
| `ingerir` | ingestão em massa (backfill + snapshot) |
| `sync` | atualização incremental — para cron/systemd |
| `enriquecer` | material/serviço, benefício ME/EPP, itens — em lotes |
| `atualizar-dados` | re-extrai todos os campos (detalhe + itens), pega prorrogações |
| `renormalizar` | re-extrai colunas do `raw_json` — **sem rede** |
| `resumo` / `base` | panorama da base (texto / JSON) |
| `listar` | consulta sem perfil de empresa (filtros por UF, esfera, SRP, ME/EPP, base legal…) |
| `exportar` | CSV ou JSONL |
| `links` / `baixar` | links do edital e download dos PDFs |
| `varredura` / `buscar` | ranqueamento por perfil de empresa |
| `fontes` | catálogo de fontes verificadas |
| `status` | estatísticas do banco |

## O que a base guarda

**`oportunidades`: 55 colunas** · **`itens`: 38 colunas**

Além do básico (órgão, UF, valor, prazo), a base captura campos que só aparecem
nos payloads completos do PNCP:

| Campo | Por que importa |
|---|---|
| `tipo_beneficio` | **"Participação exclusiva para ME/EPP"** — vantagem legal (LC 123), poucos concorrentes |
| `amparo_legal` | Base legal: `Art. 28` = pregão, `Art. 75` = dispensa, `Art. 79` = credenciamento |
| `modo_disputa` | Aberto, Aberto-Fechado, Fechado, Dispensa Com Disputa |
| `processo` | Nº do processo administrativo — para achar o edital no portal do órgão |
| `unidade_nome` | A secretaria/setor que realmente compra (não só a prefeitura) |
| `existe_resultado` | Já foi homologada? Filtra o que ainda dá para disputar |
| `srp` | Registro de Preços (contrato futuro, não compra imediata) |
| `codigo_ibge` | Cruzamento com dados do IBGE |
| `usuario_nome` | Sistema que publicou (Compras.gov.br, Licitanet, GOVTEC…) |

**Payloads completos preservados** em `raw_json`, `detalhe_json` (38 campos) e
`itens_json` (38 campos por item). Qualquer campo — mesmo os que não viraram
coluna — é consultável via `json_extract`, sem migração de schema:

```sql
SELECT id, json_extract(detalhe_json, '$.fontesOrcamentarias[0].nome') AS fonte
FROM oportunidades WHERE fonte IS NOT NULL;
```

Consequência prática: se o PNCP adicionar um campo amanhã, o dado histórico já
está no disco e basta reprocessar com `renormalizar` — **sem re-baixar nada**.


## Automação

```bash
# cron (a cada hora)
0 * * * * /root/Licitações/licitabot/scripts/atualizar.sh >> /var/log/licitabot.log 2>&1

# ou systemd timer (recomendado — tem log, retry e Persistent=true)
sudo cp licitabot/scripts/licitabot-sync.{service,timer} /etc/systemd/system/
sudo systemctl enable --now licitabot-sync.timer
```

O script usa `flock` para impedir execuções simultâneas sobre o SQLite.

## Arquitetura

```
Coleta (PNCP) → Normalização/Enriquecimento → Base SQLite → Consulta/Alerta
```

- **`http.py`** — throttle compartilhado, retry, tratamento de 429, cache em disco
- **`ingest.py`** — backfill, snapshot e enriquecimento em lote (idempotentes)
- **`store.py`** — SQLite: `oportunidades`, `itens`, `matches`, `coletas`
- **`export.py`** — consulta genérica, CSV, JSONL, panorama
- **`matching.py`** — score ponderado e **explicável** por perfil
- **`collectors/pncp.py`** — PNCP: busca, prazo, itens, enriquecimento

### Decisões que importam

**Idempotente por design.** Upsert por id + log de blocos concluídos. Se cair no
meio, rode de novo — nada duplica, nada quebra.

**`raw_json` preservado.** Se o PNCP adicionar um campo amanhã, você reprocessa
sem re-baixar nada. Nunca descarte o dado bruto.

**Material vs. serviço.** O matcher lê os itens e penaliza *compra de
equipamento*. Sem isso, "aquisição de 200 notebooks" aparece como oportunidade
para uma software house — e ruído é o que mata produtos de alerta.

**Score explicável.** Cada match devolve os termos que o dispararam.

## Documentação

- [`docs/BASE-DE-DADOS.md`](docs/BASE-DE-DADOS.md) — operação da base: modos de
  ingestão, limite de requisições, dimensionamento real, enriquecimento, automação.
- [`docs/FONTES-E-PLANO.md`](docs/FONTES-E-PLANO.md) — catálogo de fontes
  verificadas ao vivo, arquitetura, plano em fases, modelo de negócio e riscos.

## Aviso

Dados públicos, APIs públicas. **Não** faça scraping de portais privados sem
autorização formal — verifique termos de uso. Licitações-e (BB) bloqueia acesso
automatizado (HTTP 403). Respeite o `--delay` e não sobrecarregue portais
públicos: eles são um serviço público, não um recurso seu.
