#!/usr/bin/env bash
# Atualização contínua da base de licitações.
#
# Instalação (cron, a cada hora):
#   crontab -e
#   0 * * * * /root/Licitações/licitabot/scripts/atualizar.sh >> /var/log/licitabot.log 2>&1
#
# Instalação (systemd timer): ver scripts/licitabot-sync.service e .timer
#
# O script é seguro para rodar em paralelo? NÃO — use flock (já embutido) para
# evitar que duas execuções escrevam no SQLite ao mesmo tempo.

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HOME="${HOME:-/root}"
LOCK="/tmp/licitabot-sync.lock"
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$LOG_PREFIX outra execução em andamento — saindo"
    exit 0
fi

cd "$RAIZ"

echo "$LOG_PREFIX iniciando atualização incremental"

# 1) Snapshot da janela aberta: captura novos editais e atualiza prazos.
#    Janela de 20 dias cobre com folga os prazos mais longos de pregão.
#    --delay 0.5: taxa segura. Abaixo disso o PNCP responde HTTP 429.
python3 -m licitabot.cli --delay 0.5 sync --janela 20

# 2) Backfill do mês corrente: garante que nada ficou para trás.
#    Idempotente — blocos já coletados são pulados automaticamente.
INICIO_MES="$(date -d "$(date +%Y-%m-01)" +%Y-%m-%d)"
HOJE="$(date +%Y-%m-%d)"
python3 -m licitabot.cli --delay 0.5 ingerir \
    --backfill "${INICIO_MES}:${HOJE}" \
    --modalidades principais

# 3) Enriquecimento de itens em lotes (material vs serviço + categoria).
#    Retomável: processa o próximo lote de pendentes a cada execução.
#    Limite baixo de propósito — não deve competir com a coleta.
python3 -m licitabot.cli --delay 0.5 enriquecer --limite 300

# 4) Classificação setorial: derivada, sem rede, roda em segundos.
#    Separada da coleta de propósito — ver docs/BASE-DE-DADOS.md secao 8.
#    NÃO usar --buscar-itens aqui: o endpoint de itens do PNCP leva ~13s por
#    licitação (medido), o que inviabiliza rodar de hora em hora.
python3 -m licitabot.cli classificar

echo "$LOG_PREFIX concluído"
