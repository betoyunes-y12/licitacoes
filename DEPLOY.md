# Deploy do LicitaBot — estado atual

## No ar agora

| O que | Onde |
|---|---|
| **Interface de licitações** | http://100.72.121.78:8081/ |
| **Interface de resultados** | http://100.72.121.78:8081/resultados |
| Acesso local no web-host | http://127.0.0.1:8081 |
| Código | `/root/licitacoes` (web-host) e `/root/Licitações` (dsh) |
| Commit | `a9c42fd` |

Dados servidos: **1.510 licitações** (1.059 abertas, R$ 9,38 bi) e
**2.717 contratos** (1.658 fornecedores, R$ 7,51 bi).

## Comandos

    cd /root/licitacoes

    python3 -m licitabot.cli web --porta 8081     # sobe a interface
    python3 -m licitabot.cli resumo               # panorama no terminal
    python3 -m licitabot.cli listar --abertos     # lista no terminal
    ./licitabot/scripts/atualizar.sh              # forçar coleta + classificação

    systemctl status licitabot-web                # interface
    systemctl status licitabot-sync.timer         # coleta horária
    journalctl -u licitabot-web -f                # log da interface

## API

    /api/licitacoes?setor=tecnologia&abertos=1&por_pagina=25
    /api/contratos?fornecedor=iacit&ordem=valor
    /api/facetas?setor=tecnologia     # contagens respeitam os filtros ativos
    /api/estatisticas
    /api/exportar.csv?uf=SP&abertos=1

## Segurança

A interface escuta em `0.0.0.0` mas o próprio app **barra origem não
autorizada**: libera apenas localhost e a faixa do Tailscale (100.64.0.0/10).
Verificado: 127.0.0.1 → 200, IP da rede local → 403.

A base é aberta em **modo somente leitura** (`sqlite mode=ro`): um bug na
camada web vira erro de consulta, não corrupção de dado.

Para expor na internet, use `--permitir-todas` **atrás de autenticação**
(nginx/caddy). A base tem dado comercial.

## Git — RESOLVIDO

O push funcionou. A causa do bloqueio era sutil e vale registrar:

A mesma chave autenticava num repositório e falhava em outro, porque o
`licitacoes` tinha uma **deploy key read-only com precedência** sobre o acesso
de conta. A solução foi uma chave dedicada, cadastrada como chave de CONTA:

    chave   : id_ed25519_licitacoes
    config  : Host github-licitacoes (IdentitiesOnly yes)
    ja subiu: commit 2a5153c (34 arquivos, 9 commits)

Conferir o diagnóstico pelo cumprimento do SSH:

    ssh -T github-licitacoes
    # "Hi <usuario>/<repo>!"  -> autenticou por DEPLOY KEY do repo
    # "Hi <usuario>!"         -> autenticou por chave de CONTA

### web-host

O servidor tem chave propria (`licitabot@web-host`), ainda **nao cadastrada**.
Para que ele possa puxar direto do GitHub em vez de usar bundle, cadastre a
publica em https://github.com/settings/keys:

    ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP2hx9RYBqJwLvQ8e/4EqfBXNLOas/v0MSC3wXbH2cmP licitabot@web-host
    fingerprint SHA256:7+bPBhK3y4zJBeHVq9ogSnoU+1uYfe4gV/+5ETGvmbs

Enquanto isso, o deploy por bundle continua funcionando.

## Pendência 2 — PNCP bloqueou o IP público (177.221.121.85)

    Recv failure: Connection reset by peer   (após TLS estabelecer)

Afeta dsh e web-host (mesmo IP). O Compras.gov.br continua acessível.
Aguardar algumas horas. O cliente agora desiste após 3 cortes em vez de
insistir, então o timer não piora o bloqueio.

**Quando liberar:**

    cd /root/licitacoes
    python3 -m licitabot.cli --delay 0.5 sync --janela 15   # não use delay < 0.5
    python3 -m licitabot.cli classificar                     # offline, 2s

## Lições do deploy (para não repetir)

- **`ProtectSystem=full` / `ProtectHome` quebram o serviço** quando o
  repositório está em `/root`: o processo não lê o próprio código e sai limpo,
  sem log. O unit usa `ProtectHome=false`.
- **`PYTHONUNBUFFERED=1` é obrigatório** no service: sem ele o log só aparece
  quando o buffer enche, e um serviço que "não loga nada" é impossível de
  depurar.
- A porta **8080 já estava ocupada** pelo docker-proxy no web-host. Usamos 8081.
