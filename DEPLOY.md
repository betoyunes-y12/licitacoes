# Deploy do LicitaBot — estado atual

## Concluído

| Item | Estado |
|---|---|
| Código commitado (dsh) | `043b5ab` — 26 arquivos, 6.412 linhas |
| Transferido ao web-host | `/root/licitacoes` em `043b5ab` |
| CLI funcionando no web-host | sim |
| Timer systemd | ativo (`licitabot-sync.timer`, de hora em hora) |
| Bundle de backup | `/root/licitabot.bundle` (99 KB, histórico completo) |

## PENDÊNCIA 1 — push para o GitHub (só você resolve)

Erro nas duas máquinas:

    ERROR: The key you are authenticating with has been marked as read only.

A chave autentica como `betoyunes-y12`, mas o repositório tem deploy key
somente leitura. Ajuste em:

    https://github.com/betoyunes-y12/licitacoes/settings/keys

Opção A — na deploy key existente, marcar "Allow write access"
Opção B — remover a deploy key (a chave do web-host já autentica como usuário)

Depois, o push funciona:

    cd /root/licitacoes && git push -u origin main     # no web-host
    cd /root/Licitações && git push -u origin main     # no dsh

## PENDÊNCIA 2 — PNCP bloqueou o IP público (177.221.121.85)

    Connected to pncp.gov.br port 443
    SSL connection using TLSv1.3 ... verify ok
    Recv failure: Connection reset by peer

TCP e TLS estabelecem; o servidor corta depois. É bloqueio por origem, causado
pela coleta intensiva. Afeta dsh E web-host, porque compartilham o IP público.

O Compras.gov.br continua acessível — o bloqueio é só do PNCP.

**Aguarde algumas horas.** O código agora desiste após 3 cortes (em vez de
insistir), e o timer vai falhar silenciosamente sem piorar o bloqueio.

## Verificação quando liberar

    cd /root/licitacoes
    python3 -m licitabot.cli --delay 0.5 sync --janela 15
    python3 -m licitabot.cli classificar
    python3 -m licitabot.cli resumo

## Funciona sem o PNCP

    python3 -m licitabot.cli fontes       # 25 fontes catalogadas
    python3 -m licitabot.cli setores      # setores disponíveis
    python3 -m licitabot.cli classificar  # offline, 2s

## Prevenção para não bloquear de novo

- `--delay 0.5` no mínimo (0,12 causou bloqueio imediato)
- não rode backfill de meses e sync simultâneos
- o circuit breaker agora protege: 3 cortes e desiste
