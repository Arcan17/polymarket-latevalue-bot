#!/bin/bash
# ─────────────────────────────────────────────────────────────
# restart.sh — Reinicia el Late Value Bot después del kill switch
# Uso: ./restart.sh
# ─────────────────────────────────────────────────────────────

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="/tmp/latevalue_bot.pid"
LOG_FILE="/tmp/latevalue_bot.log"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  LATE VALUE BOT — REINICIO"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Matar instancia anterior si existe
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Deteniendo instancia anterior (PID $OLD_PID)..."
        kill -9 "$OLD_PID" 2>/dev/null
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# 2. Borrar estado temporal (NO borra stats.json — las ganancias se conservan)
rm -f /tmp/latevalue_state.json
echo "Estado temporal limpiado (stats preservadas)"

# 3. Arrancar bot
cd "$BOT_DIR"
source venv/bin/activate

echo "Iniciando bot..."
nohup caffeinate -s python3 main.py > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "Bot iniciado con PID $NEW_PID"
echo ""

# 4. Esperar y mostrar confirmación
sleep 5
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "✓ Bot corriendo correctamente"
    echo ""
    echo "Ver logs:      tail -f $LOG_FILE"
    echo "Ver dashboard: cd $BOT_DIR && source venv/bin/activate && python3 dashboard.py"
    echo "Detener:       kill -9 \$(cat $PID_FILE)"
else
    echo "✗ Error al iniciar — revisa los logs:"
    tail -20 "$LOG_FILE"
fi
