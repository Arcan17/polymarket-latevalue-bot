#!/usr/bin/env python3
"""
Archiva la sesión actual y empieza datos limpios para una nueva versión del bot.

Usar SIEMPRE que se haga un cambio que invalide los datos históricos:
  - Cambio en la lógica de entrada (filtros, edge mínimo, etc.)
  - Cambio en el modelo de pricing
  - Corrección de un bug que distorsionaba resultados
  - Cualquier cambio que haga que los datos viejos no sean comparables

Uso:
  python3 reset_version.py              → pide confirmación interactiva
  python3 reset_version.py --confirm    → sin preguntar (para scripts)

Qué hace:
  1. Archiva trades.log → archive/trades_vX.Y_YYYY-MM-DD.log
  2. Archiva stats.json → archive/stats_vX.Y_YYYY-MM-DD.json
  3. Resetea stats.json a cero
  4. Limpia trades.log
  5. Incrementa VERSION (v2.0 → v2.1, v2.1 → v2.2, etc.)
  6. Escribe un marker en el trades.log nuevo con el motivo del reset
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
TRADES = BASE / "trades.log"
STATS = BASE / "stats.json"
VERSION_F = BASE / "VERSION"
ARCHIVE_DIR = BASE / "archive"


def _read_version() -> str:
    try:
        return VERSION_F.read_text().strip()
    except Exception:
        return "v1.0"


def _next_version(current: str) -> str:
    """v2.0 → v2.1  |  v2.9 → v2.10  |  v1.0 → v1.1"""
    try:
        num = current.lstrip("v")
        parts = num.split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return "v" + ".".join(parts)
    except Exception:
        return current + ".1"


def _summarize_trades(path: Path) -> dict:
    trades, corrections = [], 0
    try:
        with open(path) as f:
            for line in f:
                try:
                    t = json.loads(line.strip())
                    if t.get("type") == "API_CORRECTION":
                        corrections += 1
                        continue
                    trades.append(t)
                except Exception:
                    pass
    except Exception:
        pass

    if not trades:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "corrections": corrections,
        }

    wins = sum(1 for t in trades if t.get("result") == "WIN")
    losses = sum(1 for t in trades if t.get("result") == "LOSS")
    pnl = sum(t.get("pnl", 0.0) for t in trades)
    wagered = sum(t.get("size_usdc", 0.0) for t in trades)
    stale = sum(1 for t in trades if t.get("edge", 0) > 0.35)
    real = len(trades) - stale

    dates = sorted(
        {t.get("entry_time", "")[:10] for t in trades if t.get("entry_time")}
    )
    date_range = f"{dates[0]} → {dates[-1]}" if dates else "?"

    return {
        "count": len(trades),
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
        "wagered": wagered,
        "stale": stale,
        "real": real,
        "corrections": corrections,
        "date_range": date_range,
    }


def main() -> None:
    auto_confirm = "--confirm" in sys.argv

    ARCHIVE_DIR.mkdir(exist_ok=True)

    current_version = _read_version()
    next_version = _next_version(current_version)
    date_str = datetime.now().strftime("%Y-%m-%d")

    # ── Resumen de la versión actual ──────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"  RESET DE VERSIÓN: {current_version} → {next_version}")
    print("=" * 60)

    summary = _summarize_trades(TRADES)
    if summary["count"] > 0:
        wr = (
            summary["wins"] / (summary["wins"] + summary["losses"])
            if (summary["wins"] + summary["losses"]) > 0
            else 0
        )
        roi = summary["pnl"] / summary["wagered"] if summary["wagered"] > 0 else 0
        print(
            f"\n  Trades en {current_version}:  {summary['count']}  ({summary['date_range']})"
        )
        print(
            f"  Resultados:      {summary['wins']}W / {summary['losses']}L  (WR {wr:.0%})"
        )
        print(f"  PnL total:       ${summary['pnl']:+.2f}  (ROI {roi:+.1%})")
        print(
            f"  Stale (edge>35%): {summary['stale']} trades  ← serán datos limpios en {next_version}"
        )
        print(f"  Reales (edge≤35%): {summary['real']} trades")
    else:
        print(f"\n  trades.log vacío o sin datos — reseteo limpio.")

    print()

    # ── Pedir motivo del reset ────────────────────────────────────────────────
    if auto_confirm:
        reason = "reset automático"
    else:
        reason = input(
            "  Motivo del reset (ej: 'max_edge guard implementado'): "
        ).strip()
        if not reason:
            reason = "sin motivo especificado"

        print()
        confirm = (
            input(f"  ¿Confirmar reset {current_version} → {next_version}? [s/N]: ")
            .strip()
            .lower()
        )
        if confirm not in ("s", "si", "sí", "y", "yes"):
            print("  Cancelado.")
            return

    print()

    # ── 1. Archivar trades.log ────────────────────────────────────────────────
    if TRADES.exists() and TRADES.stat().st_size > 0:
        archive_name = f"trades_{current_version}_{date_str}.log"
        shutil.copy(TRADES, ARCHIVE_DIR / archive_name)
        print(f"  ✓ Archivado: archive/{archive_name}")
    else:
        print(f"  - trades.log vacío, nada que archivar")

    # ── 2. Archivar stats.json ────────────────────────────────────────────────
    if STATS.exists():
        stats_archive = f"stats_{current_version}_{date_str}.json"
        shutil.copy(STATS, ARCHIVE_DIR / stats_archive)
        print(f"  ✓ Archivado: archive/{stats_archive}")

    # ── 3. Resetear stats.json ────────────────────────────────────────────────
    empty_stats = {
        "bets_placed": 0,
        "bets_won": 0,
        "bets_lost": 0,
        "total_wagered": 0.0,
        "total_pnl": 0.0,
        "best_edge": 0.0,
    }
    with open(STATS, "w") as f:
        json.dump(empty_stats, f, indent=2)
    print(f"  ✓ stats.json reseteado a cero")

    # ── 4. Limpiar trades.log y escribir marker de inicio ────────────────────
    marker = {
        "type": "VERSION_RESET",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "from_version": current_version,
        "to_version": next_version,
        "reason": reason,
        "archived_trades": summary["count"],
        "archived_pnl": round(summary["pnl"], 4),
    }
    with open(TRADES, "w") as f:
        f.write(json.dumps(marker) + "\n")
    print(f"  ✓ trades.log limpiado (marker de inicio escrito)")

    # ── 5. Actualizar VERSION ─────────────────────────────────────────────────
    VERSION_F.write_text(next_version + "\n")
    print(f"  ✓ VERSION: {current_version} → {next_version}")

    # ── 6. Limpiar live_positions si existe ───────────────────────────────────
    live_pos_file = Path("/tmp/live_positions.json")
    if live_pos_file.exists():
        live_pos_file.unlink()
        print(f"  ✓ /tmp/live_positions.json limpiado")

    print()
    print(f"  Bot listo para empezar limpio como {next_version}")
    print(f"  Motivo: {reason}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
