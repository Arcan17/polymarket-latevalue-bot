#!/bin/bash
# ─────────────────────────────────────────────────────────────
# setup.sh — Configura el Late Value Bot en una Mac nueva
# Uso: bash setup.sh
# ─────────────────────────────────────────────────────────────

set -e  # Abortar si algún comando falla

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  LATE VALUE BOT — SETUP NUEVA MAC"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Directorio: $BOT_DIR"
echo ""

# ── 1. Verificar Homebrew ──────────────────────────────────
if ! command -v brew &>/dev/null; then
    echo "▶ Instalando Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Añadir brew al PATH para Apple Silicon
    if [ -f "/opt/homebrew/bin/brew" ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
    fi
else
    echo "✓ Homebrew ya instalado: $(brew --version | head -1)"
fi

# ── 2. Instalar Python 3.11 ───────────────────────────────
if ! command -v python3.11 &>/dev/null; then
    echo "▶ Instalando Python 3.11..."
    brew install python@3.11
else
    echo "✓ Python 3.11 ya instalado: $(python3.11 --version)"
fi

# ── 3. Crear entorno virtual ──────────────────────────────
cd "$BOT_DIR"
if [ -d "venv" ]; then
    echo "▶ Eliminando venv anterior..."
    rm -rf venv
fi
echo "▶ Creando venv con Python 3.11..."
python3.11 -m venv venv
source venv/bin/activate
echo "✓ venv creado: $(python --version)"

# ── 4. Instalar dependencias ──────────────────────────────
echo ""
echo "▶ Instalando dependencias..."
pip install --upgrade pip --quiet

pip install \
    websockets \
    aiohttp \
    python-dotenv \
    pydantic \
    pydantic-settings \
    py-clob-client \
    rich \
    --quiet

echo "✓ Dependencias instaladas"

# ── 5. Verificar .env ─────────────────────────────────────
echo ""
if [ -f ".env" ]; then
    echo "✓ .env encontrado"
    # Mostrar modo actual (sin mostrar claves)
    MODE=$(grep "^TRADING_MODE" .env | cut -d= -f2 | tr -d ' ')
    echo "  Modo actual: $MODE"
else
    echo "⚠ No se encontró .env — copia el archivo desde la Mac original"
fi

# ── 6. Resetear stats.json (Mac nueva = capital inicial limpio) ───────────
echo "▶ Reseteando stats.json para empezar desde capital inicial (\$42)..."
echo '{"bets_placed":0,"bets_won":0,"bets_lost":0,"total_wagered":0.0,"total_pnl":0.0,"best_edge":0.0}' > stats.json
echo "✓ Stats en cero — el bot arrancará mostrando \$42.00"

# ── 7. Permisos scripts ───────────────────────────────────
chmod +x restart.sh setup.sh 2>/dev/null || true

# ── 8. Test de importaciones ──────────────────────────────
echo ""
echo "▶ Verificando importaciones..."
python - <<'PYCHECK'
import sys
errors = []
for mod in ["websockets", "aiohttp", "dotenv", "pydantic", "rich"]:
    try:
        __import__(mod)
    except ImportError:
        errors.append(mod)
try:
    from py_clob_client.client import ClobClient
except ImportError:
    errors.append("py_clob_client")

if errors:
    print(f"✗ Módulos faltantes: {', '.join(errors)}")
    sys.exit(1)
else:
    print("✓ Todas las importaciones OK")
PYCHECK

# ── Resumen ───────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SETUP COMPLETADO"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Para iniciar el bot:"
echo "  cd $BOT_DIR"
echo "  bash restart.sh"
echo ""
echo "Para ver logs en tiempo real:"
echo "  tail -f /tmp/latevalue_bot.log"
echo ""
echo "Para ver el dashboard:"
echo "  cd $BOT_DIR && source venv/bin/activate && python3 dashboard.py"
echo ""
