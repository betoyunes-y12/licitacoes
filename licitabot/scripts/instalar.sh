#!/usr/bin/env bash
# ============================================================================
# LicitaBot — instalador de produção
#
# Roda NO SERVIDOR de destino (web-host). É idempotente: pode rodar de novo
# sem quebrar o que já existe.
#
#   curl -fsSL <url>/instalar.sh | bash
#   # ou, com o repositório já clonado:
#   sudo bash scripts/instalar.sh
#
# O que faz:
#   1. verifica Python (3.10+) e git
#   2. clona/atualiza o repositório em /opt/licitabot
#   3. cria o diretório de dados e o usuário de execução
#   4. instala o timer systemd (ou cron, se systemd não existir)
#   5. roda uma coleta inicial de validação
#   6. imprime como verificar se está funcionando
# ============================================================================
set -euo pipefail

# $HOME pode não estar definido em cron/systemd, e git falha sem ele
export HOME="${HOME:-/root}"

REPO_URL="${LICITABOT_REPO:-git@github-licitacoes:betoyunes-y12/licitacoes.git}"
DESTINO="${LICITABOT_DIR:-/opt/licitabot}"
USUARIO="${LICITABOT_USER:-root}"

info()  { printf '\033[1;34m[info]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
erro()  { printf '\033[1;31m[erro]\033[0m %s\n' "$*" >&2; }
aviso() { printf '\033[1;33m[aviso]\033[0m %s\n' "$*"; }

# --------------------------------------------------------------- 1. pré-requisitos
info "verificando pré-requisitos"

command -v git >/dev/null 2>&1 || { erro "git não encontrado. Instale: apt install -y git"; exit 1; }

PY=$(command -v python3 || true)
[ -n "$PY" ] || { erro "python3 não encontrado. Instale: apt install -y python3"; exit 1; }

VER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
"$PY" - <<'PYCHK' || { erro "Python 3.10+ é necessário (usei $VER)."; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PYCHK
ok "python $VER em $PY"

# O projeto usa SOMENTE biblioteca padrão — nenhum pip install é necessário.
"$PY" -c "import sqlite3, urllib.request, json" \
  || { erro "faltam módulos da stdlib (sqlite3/urllib)."; exit 1; }
ok "stdlib completa (sqlite3, urllib) — sem dependências externas"

# --------------------------------------------------------------- 2. código
info "preparando $DESTINO"

if [ -d "$DESTINO/.git" ]; then
    info "repositório já existe — atualizando"
    git -C "$DESTINO" fetch --all --quiet || aviso "fetch falhou (rede/credencial?)"
    git -C "$DESTINO" pull --ff-only --quiet || aviso "pull não avançou (sem upstream ou já atualizado)"
    ok "código atualizado: $(git -C "$DESTINO" log --oneline -1)"
else
    mkdir -p "$(dirname "$DESTINO")"
    if git clone --quiet "$REPO_URL" "$DESTINO" 2>/dev/null; then
        ok "clonado de $REPO_URL"
    else
        erro "clone falhou."
        echo
        echo "  Causas comuns:"
        echo "   - a chave SSH desta máquina não tem acesso ao repositório"
        echo "   - o repositório ainda não recebeu o primeiro push"
        echo
        echo "  Alternativa sem credencial: copie o bundle e clone dele"
        echo "    scp licitabot.bundle $(hostname):/tmp/"
        echo "    git clone /tmp/licitabot.bundle $DESTINO"
        echo
        exit 1
    fi
fi

cd "$DESTINO"

# --------------------------------------------------------------- 3. dados
info "preparando diretório de dados"

mkdir -p "$DESTINO/licitabot/data"          # banco + cache HTTP
mkdir -p "$DESTINO/dados"                   # exports e PDFs baixados
chmod +x "$DESTINO/licitabot/scripts/atualizar.sh" 2>/dev/null || true
ok "diretórios criados (banco e cache ficam em licitabot/data/)"

# o .gitignore cobre data/ e dados/ — conferindo
if git -C "$DESTINO" check-ignore -q licitabot/data 2>/dev/null; then
    ok "dados gerados estão ignorados pelo git"
else
    aviso "licitabot/data NÃO está no .gitignore — confira antes de commitar"
fi

# --------------------------------------------------------------- 4. agendamento
info "configurando atualização horária"

if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
    sed -e "s|/root/Licitações|$DESTINO|g" \
        -e "s|^User=.*|User=$USUARIO|" \
        -e "s|^Environment=HOME=.*|Environment=HOME=$(eval echo ~$USUARIO)|" \
        "$DESTINO/licitabot/scripts/licitabot-sync.service" \
        > /etc/systemd/system/licitabot-sync.service
    cp "$DESTINO/licitabot/scripts/licitabot-sync.timer" \
       /etc/systemd/system/licitabot-sync.timer
    systemctl daemon-reload
    systemctl enable --now licitabot-sync.timer >/dev/null 2>&1 || true
    ok "timer systemd instalado"
    systemctl list-timers licitabot-sync.timer --no-pager 2>/dev/null | head -3 || true
else
    # cron como alternativa
    CRON_LINHA="0 * * * * cd $DESTINO && HOME=$(eval echo ~$USUARIO) ./licitabot/scripts/atualizar.sh >> /var/log/licitabot.log 2>&1"
    if crontab -l 2>/dev/null | grep -qF "licitabot/scripts/atualizar.sh"; then
        ok "cron já configurado"
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINHA") | crontab -
        ok "cron instalado (a cada hora)"
    fi
fi

# --------------------------------------------------------------- 5. validação
info "rodando coleta inicial de validação (pode levar ~2 min)"

if "$PY" -m licitabot.cli --delay 0.5 sync --janela 7 2>&1 | tail -3; then
    ok "coleta inicial concluída"
else
    aviso "coleta inicial falhou — verifique rede e o log acima"
fi

info "classificando a base"
"$PY" -m licitabot.cli classificar 2>&1 | tail -6 || aviso "classificação falhou"

# --------------------------------------------------------------- 6. resumo
echo
echo "============================================================================"
echo "  INSTALAÇÃO CONCLUÍDA"
echo "============================================================================"
"$PY" -m licitabot.cli resumo 2>/dev/null | head -14 || true
echo
echo "  Comandos úteis:"
echo "    cd $DESTINO"
echo "    python3 -m licitabot.cli resumo            # panorama da base"
echo "    python3 -m licitabot.cli listar --abertos  # o que está aberto"
echo "    python3 -m licitabot.cli contratos         # quem ganhou, por quanto"
echo "    ./licitabot/scripts/atualizar.sh           # forçar atualização agora"
echo
echo "  Verificar agendamento:"
echo "    systemctl list-timers licitabot-sync.timer   # se systemd"
echo "    crontab -l                                    # se cron"
echo "    tail -f /var/log/licitabot.log                # acompanhar execuções"
echo "============================================================================"
