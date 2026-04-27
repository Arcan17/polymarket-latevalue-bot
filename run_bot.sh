#!/bin/bash
# Auto-restart wrapper para el Late Value Bot
# Uso: ./run_bot.sh &
# El bot se reinicia automáticamente si crashea, con backoff exponencial.

cd "$(dirname "$0")"
source venv/bin/activate

MAX_RESTARTS=20        # máximo reinicios antes de rendirse
BACKOFF_BASE=5         # segundos de espera inicial
BACKOFF_MAX=120        # máximo 2 minutos entre reinicios
MIN_UPTIME=30          # si corre < 30s, cuenta como crash rápido

restarts=0
backoff=$BACKOFF_BASE

while [ $restarts -lt $MAX_RESTARTS ]; do
    echo "[RUNNER] $(date '+%H:%M:%S') — Iniciando bot (intento $((restarts+1))/$MAX_RESTARTS)..."
    start_ts=$(date +%s)

    PYTHONUNBUFFERED=1 python3 -u main.py
    exit_code=$?

    uptime=$(( $(date +%s) - start_ts ))
    echo "[RUNNER] $(date '+%H:%M:%S') — Bot detuvo (exit=$exit_code, uptime=${uptime}s)"

    # Si el bot corrió >30s, resetear backoff (fue un crash real, no loop de fallo)
    if [ $uptime -gt $MIN_UPTIME ]; then
        backoff=$BACKOFF_BASE
        restarts=0
    else
        restarts=$((restarts + 1))
        backoff=$(( backoff * 2 > BACKOFF_MAX ? BACKOFF_MAX : backoff * 2 ))
    fi

    # Exit limpio si fue kill intencional (SIGTERM/SIGINT = exit code 130/143)
    if [ $exit_code -eq 130 ] || [ $exit_code -eq 143 ]; then
        echo "[RUNNER] Detenido por señal — no reiniciando."
        exit 0
    fi

    echo "[RUNNER] Reiniciando en ${backoff}s..."
    sleep $backoff
done

echo "[RUNNER] Máximo de reinicios alcanzado ($MAX_RESTARTS). Revisar logs."
exit 1
