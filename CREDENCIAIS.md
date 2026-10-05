# Acesso à interface do LicitaBot

## Link externo

    https://web-host.sapsucker-city.ts.net/licitabot/

Disponível apenas dentro do **tailnet** (não está na internet). Funciona do
MacBook, do pve e de qualquer máquina logada na mesma conta Tailscale.

## Credenciais

    usuário: licitabot
    senha:   K4Hei9aew74nJXtgATb7

## Trocar a senha

    cd /root/licitacoes
    python3 -m licitabot.cli senha                    # gera uma nova
    systemctl restart licitabot-web

A senha não fica armazenada em texto: o arquivo guarda só o hash
(`licitabot/data/.htpasswd`, permissão 600, fora do git).

## Onde fica cada coisa

| | |
|---|---|
| Interface (licitações) | https://web-host.sapsucker-city.ts.net/licitabot/ |
| Interface (resultados) | https://web-host.sapsucker-city.ts.net/licitabot/resultados |
| API | https://web-host.sapsucker-city.ts.net/licitabot/api/licitacoes?uf=SP&abertos=1 |
| Código no servidor | `/root/licitacoes` (web-host) |
| Repositório | https://github.com/betoyunes-y12/licitacoes |

## Para expor na INTERNET (fora do tailnet)

Hoje **não** está exposto. Se precisar:

    tailscale funnel --bg --https=443 --set-path=/licitabot http://127.0.0.1:8083

Isso torna a URL pública. A autenticação Basic continua valendo, mas Basic +
internet é proteção fraca — nesse caso, troque por autenticação de verdade
antes (sessão com hash forte) ou coloque um proxy com rate limit na frente.
