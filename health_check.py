#!/usr/bin/env python3
"""Health check script for polymarket_latevalue bot."""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime

BOT_DIR = "/Users/bastian/Documents/polymarket_latevalue"
TRADES_LOG = os.path.join(BOT_DIR, "trades.log")
STATS_JSON = os.path.join(BOT_DIR, "stats.json")
VERSION_FILE = os.path.join(BOT_DIR, "VERSION")

RESTART_CMD = (
    f"cd {BOT_DIR} && source venv/bin/activate && "
    "nohup python3 main.py >> bot.log 2>&1 &"
)


def is_bot_alive() -> bool:
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    return "main.py" in result.stdout


def restart_bot() -> bool:
    result = subprocess.run(["bash", "-c", RESTART_CMD], capture_output=True, text=True)
    return result.returncode == 0


def read_version() -> str:
    try:
        return open(VERSION_FILE).read().strip()
    except Exception:
        return "unknown"


def read_stats() -> dict:
    try:
        return json.loads(open(STATS_JSON).read())
    except Exception:
        return {}


def read_trades_today() -> list[dict]:
    today = date.today().isoformat()
    trades = []
    try:
        with open(TRADES_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Skip non-trade records
                if rec.get("type") in ("VERSION_RESET",):
                    continue
                ts = rec.get("entry_time", rec.get("settle_time", ""))
                if ts.startswith(today):
                    trades.append(rec)
    except FileNotFoundError:
        pass
    return trades


def check_anomalies(trades: list[dict], stats: dict) -> list[str]:
    anomalies = []

    # 1. Edge > 0.50 on entered trades
    high_edge = [
        t
        for t in trades
        if isinstance(t.get("edge"), (int, float)) and t["edge"] > 0.50
    ]
    for t in high_edge:
        anomalies.append(
            f"HIGH EDGE {t['edge']:.2f} on {t.get('symbol','?')} "
            f"@ {t.get('entry_time','?')}"
        )

    # 2. stats.json vs log PnL divergence > $5
    log_pnl = sum(
        t.get("pnl", 0) for t in trades if isinstance(t.get("pnl"), (int, float))
    )
    stats_pnl = stats.get("total_pnl", None)
    if stats_pnl is not None and abs(stats_pnl - log_pnl) > 5:
        anomalies.append(
            f"PnL DIVERGENCE: stats.json={stats_pnl:.2f} vs log={log_pnl:.2f} "
            f"(diff={abs(stats_pnl - log_pnl):.2f})"
        )

    # 3. WIN logged with RTDS-pre/post but close_vs_strike contradicts result
    for t in trades:
        source = t.get("settle_source", "")
        result = t.get("result", "")
        side = t.get("side", "")
        close_vs = t.get("close_vs_strike", "")
        if result == "WIN" and source in ("RTDS-pre", "RTDS-post"):
            # YES side wins if close_vs_strike == "above"
            # NO side wins if close_vs_strike == "below"
            expected_close = "above" if side == "YES" else "below"
            if close_vs and close_vs != expected_close:
                anomalies.append(
                    f"RESULT CONTRADICTION: {t.get('symbol','?')} {side} WIN "
                    f"but close_vs_strike={close_vs} via {source} "
                    f"@ {t.get('entry_time','?')}"
                )

    return anomalies


def main():
    parser = argparse.ArgumentParser(description="Bot health check")
    parser.add_argument(
        "--restart", action="store_true", help="Force restart even if alive"
    )
    args = parser.parse_args()

    version = read_version()
    stats = read_stats()
    trades = read_trades_today()
    anomalies = check_anomalies(trades, stats)

    alive = is_bot_alive()
    restarted = False

    if not alive or args.restart:
        if args.restart and alive:
            print("[health_check] --restart flag set, restarting bot...")
        restarted = restart_bot()

    # Bot status line
    if alive and not args.restart:
        status_str = "ALIVE"
    elif restarted:
        status_str = "DEAD -> RESTARTED" if not alive else "ALIVE -> RESTARTED"
    else:
        status_str = "DEAD (restart failed)"

    wins = sum(1 for t in trades if t.get("result") == "WIN")
    losses = sum(1 for t in trades if t.get("result") == "LOSS")
    stats_pnl = stats.get("total_pnl", 0.0)

    print("=" * 50)
    print(f"  Polymarket LateValue Bot — Health Check")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    print(f"  Bot status : {status_str}")
    print(f"  Version    : {version}")
    print(f"  Trades today: {wins}W / {losses}L")
    print(f"  PnL (stats): ${stats_pnl:+.2f}")
    print("-" * 50)
    if anomalies:
        print(f"  Anomalies ({len(anomalies)}):")
        for a in anomalies:
            print(f"    ! {a}")
    else:
        print("  Anomalies  : Todo OK \u2713")
    print("=" * 50)


if __name__ == "__main__":
    main()
