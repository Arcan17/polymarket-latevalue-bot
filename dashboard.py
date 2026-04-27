"""
Dashboard para Late Value Bot — muestra estado en tiempo real.
Uso: venv/bin/python3 dashboard.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

STATE_FILE = "/tmp/latevalue_state.json"
REFRESH_RATE = 1  # segundos

SYM_COLORS = {"BTC": "yellow", "ETH": "cyan", "SOL": "magenta", "XRP": "blue"}


def load_state() -> dict | None:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def fmt_uptime(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_header(state: dict) -> Panel:
    mode   = state.get("mode", "?")
    btc    = state.get("btc_price", 0)
    eth    = state.get("eth_price", 0)
    sol    = state.get("sol_price", 0)
    xrp    = state.get("xrp_price", 0)
    btc_b  = state.get("btc_binance", 0)
    uptime = fmt_uptime(state.get("uptime_s", 0))
    updated = state.get("updated", "--:--:--")
    source  = state.get("price_source", "?")
    tp      = state.get("take_profit_price", 0)

    mode_color = "green" if mode == "PAPER" else "red bold"
    src_color  = "green bold" if source == "Chainlink" else "yellow"

    text = Text()
    text.append("  LATE VALUE BOT  ", style="bold white on dark_blue")
    text.append("  modo=")
    text.append(mode, style=mode_color)
    text.append("  |  ")
    text.append(source, style=src_color)
    text.append(": ")
    text.append(f"BTC ${btc:,.2f}", style="yellow bold")
    if eth:
        text.append(f"  ETH ${eth:,.2f}", style="cyan bold")
    if sol:
        text.append(f"  SOL ${sol:,.4f}", style="magenta bold")
    if xrp:
        text.append(f"  XRP ${xrp:,.4f}", style="blue bold")

    if source == "Chainlink" and btc_b and btc:
        diff = btc - btc_b
        diff_color = "yellow" if abs(diff) > 20 else "dim"
        text.append(f"  (Δ {diff:+.0f})", style=diff_color)

    if tp > 0:
        text.append(f"  |  TP@{tp:.2f}", style="green bold")

    text.append(f"  |  uptime: {uptime}  |  {updated}")
    return Panel(text, box=box.DOUBLE_EDGE, style="bold")


def build_stats(state: dict) -> Panel:
    s = state.get("stats", {})
    pnl   = s.get("total_pnl", 0)
    dpnl  = s.get("daily_pnl", 0)
    roi   = s.get("roi", 0)
    cap   = s.get("capital", 42.0)
    pnl_color  = "green" if pnl  >= 0 else "red"
    dpnl_color = "green" if dpnl >= 0 else "red"

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("key",  style="dim")
    table.add_column("val",  style="bold")
    table.add_column("key2", style="dim")
    table.add_column("val2", style="bold")

    table.add_row("Apuestas",   f"{s.get('bets_placed',0)}",
                  "Win rate",   f"{s.get('win_rate',0):.0%}")
    table.add_row("Ganadas",    f"[green]{s.get('bets_won',0)}[/]",
                  "Perdidas",   f"[red]{s.get('bets_lost',0)}[/]")
    table.add_row("Apostado",   f"${s.get('total_wagered',0):.2f}",
                  "Mejor edge", f"{s.get('best_edge',0):.1%}")
    table.add_row("PnL total",  f"[{pnl_color}]${pnl:+.4f}[/]",
                  "PnL hoy",    f"[{dpnl_color}]${dpnl:+.4f}[/]")
    table.add_row("ROI",        f"[{pnl_color}]{roi:+.1%}[/]",
                  "Capital",    f"${cap:.2f}")

    return Panel(table, title="[bold cyan]ESTADÍSTICAS[/]", box=box.ROUNDED)


def build_positions(state: dict) -> Panel:
    positions = state.get("open_positions", [])
    tp = state.get("take_profit_price", 0)

    table = Table(box=box.SIMPLE_HEAD, show_header=True)
    table.add_column("Cripto",    style="bold", width=5)
    table.add_column("Lado",      style="bold", width=5)
    table.add_column("Entrada",   justify="right", width=8)
    table.add_column("Prob",      justify="right", width=7)
    table.add_column("Spot entr", justify="right", width=12)
    table.add_column("TP target", justify="right", width=9)
    table.add_column("Size",      justify="right", width=7)

    if not positions:
        table.add_row("[dim]—[/]", "[dim]sin posiciones abiertas[/]", "", "", "", "", "")
    else:
        for p in positions:
            side_color = "green" if p["side"] == "YES" else "red"
            sym = p.get("symbol", "?")
            sym_color = SYM_COLORS.get(sym, "white")
            # Ganancia estimada si llega al TP
            tp_pnl = ""
            if tp > 0 and p["entry_price"] > 0:
                shares = p["size"] / p["entry_price"]
                fee_buy  = p["size"] * 0.072 * (1 - p["entry_price"])
                fee_sell = shares * tp * 0.072 * (1 - tp)
                est_pnl  = shares * tp - p["size"] - fee_buy - fee_sell
                tp_pnl = f"[green]+${est_pnl:.2f}[/]" if est_pnl > 0 else f"[red]${est_pnl:.2f}[/]"
            table.add_row(
                f"[{sym_color}]{sym}[/]",
                f"[{side_color}]{p['side']}[/]",
                f"{p['entry_price']:.3f}",
                f"{p['our_prob']:.1%}",
                f"${p['spot_entry']:,.2f}",
                tp_pnl if tp > 0 else "[dim]—[/]",
                f"${p['size']:.2f}",
            )

    return Panel(table, title="[bold cyan]POSICIONES ABIERTAS[/]", box=box.ROUNDED)


SPOT_KEYS = {"BTC": "btc_price", "ETH": "eth_price", "SOL": "sol_price", "XRP": "xrp_price"}


def build_markets(state: dict) -> Panel:
    markets = state.get("markets", [])

    markets_5m  = [m for m in markets if m.get("timeframe", "5m") == "5m"]
    markets_15m = [m for m in markets if m.get("timeframe", "5m") == "15m"]

    table = Table(box=box.SIMPLE_HEAD, show_header=True, expand=True)
    table.add_column("TF",         width=4,   style="dim")
    table.add_column("Sym",        width=4)
    table.add_column("Strike",     justify="right",  width=12)
    table.add_column("Dist",       justify="center", width=8)
    table.add_column("Dirección",  justify="center", width=12)
    table.add_column("Tiempo",     justify="right",  width=12)

    def add_section(mlist: list, label: str, color: str) -> None:
        if not mlist:
            return
        table.add_row(
            f"[{color} bold]── {label} ──[/]", "", "", "", "", "",
            style="on grey19"
        )
        for m in sorted(mlist, key=lambda x: x["tte_s"])[:8]:
            tte = m["tte_s"]
            sym = m.get("symbol", "?")

            # ── Tiempo restante ──────────────────────────────────────
            if tte < 90:
                tte_str = f"[yellow bold]{tte}s ← ENTR[/]"
            elif tte < 180:
                tte_str = f"[yellow]{tte}s[/]"
            else:
                mm, ss = divmod(tte, 60)
                tte_str = f"[dim]{mm}m {ss:02d}s[/]"

            # ── Strike ───────────────────────────────────────────────
            strike_ok  = m.get("strike_ok", False)
            strike_val = m["strike"]
            # Formato compacto: BTC→2 decimales, SOL/XRP→4 si <100
            if strike_val >= 1000:
                s_fmt = f"${strike_val:,.0f}"
            elif strike_val >= 10:
                s_fmt = f"${strike_val:,.2f}"
            else:
                s_fmt = f"${strike_val:,.4f}"
            conf_icon  = "✓" if strike_ok else "~"
            conf_color = "green" if strike_ok else "yellow"
            strike_str = f"[{conf_color}]{s_fmt}{conf_icon}[/]"

            # ── Distancia al strike (spot vs strike) ─────────────────
            # Verde >0.20% | Amarillo 0.08-0.20% | Rojo <0.08% (muy incierto)
            spot = state.get(SPOT_KEYS.get(sym, ""), 0)
            if spot > 0 and strike_val > 0:
                dist_pct = (spot - strike_val) / strike_val * 100
                abs_dist = abs(dist_pct)
                sign     = "▲" if dist_pct > 0 else "▼"
                if abs_dist >= 0.20:
                    dist_color = "green bold"
                elif abs_dist >= 0.08:
                    dist_color = "yellow"
                else:
                    dist_color = "red"
                dist_str = f"[{dist_color}]{sign}{abs_dist:.2f}%[/]"
            else:
                dist_str = "[dim]—[/]"

            # ── Dirección del mercado ─────────────────────────────────
            # Muestra el lado dominante + su probabilidad de forma clara
            yes_ask = m.get("yes_ask")
            no_ask  = m.get("no_ask")
            if yes_ask is not None and no_ask is not None:
                if yes_ask >= 0.60:
                    dir_str = f"[green bold]UP  {yes_ask:.0%}[/]"
                elif no_ask >= 0.60:
                    dir_str = f"[red bold]DOWN {no_ask:.0%}[/]"
                else:
                    # Mercado indeciso — mostrar ambos
                    dir_str = f"[dim]{yes_ask:.0%}↑ {no_ask:.0%}↓[/]"
            elif yes_ask is not None:
                dir_str = f"[green]UP  {yes_ask:.0%}[/]"
            elif no_ask is not None:
                dir_str = f"[red]DOWN {no_ask:.0%}[/]"
            else:
                dir_str = "[dim]sin book[/]"

            sym_color = SYM_COLORS.get(sym, "white")

            table.add_row(
                f"[{color}]{m.get('timeframe','?')}[/]",
                f"[{sym_color} bold]{sym}[/]",
                strike_str,
                dist_str,
                dir_str,
                tte_str,
            )

    if not markets:
        table.add_row("[dim]Buscando...[/]", "", "", "", "", "")
    else:
        add_section(markets_5m,  "5 MINUTOS",  "cyan")
        add_section(markets_15m, "15 MINUTOS", "green")

    return Panel(table, title="[bold cyan]MERCADOS MONITOREADOS[/]", box=box.ROUNDED)


def build_trades(state: dict) -> Panel:
    trades = state.get("last_trades", [])
    table = Table(box=box.SIMPLE_HEAD, show_header=True)
    table.add_column("Hora",    width=8)
    table.add_column("TF",      width=4)
    table.add_column("Cripto",  width=5)
    table.add_column("Lado",    width=5)
    table.add_column("Strike",  justify="right", width=11)
    table.add_column("Sal",     justify="right", width=11)
    table.add_column("Entrada", justify="right", width=8)
    table.add_column("PnL",     justify="right", width=10)
    table.add_column("",        width=4)

    if not trades:
        table.add_row("[dim]—[/]", "", "[dim]sin trades aún[/]", "", "", "", "", "", "")
    else:
        for t in reversed(trades):
            pnl        = t["pnl"]
            pnl_color  = "green" if pnl >= 0 else "red"
            result     = t.get("result", "?")
            exit_type  = t.get("exit_type", "EXP")
            timeframe  = t.get("timeframe", "5m")
            tf_color   = "green" if timeframe == "15m" else "cyan"

            # Icono de resultado + tipo de salida
            if exit_type == "TP":
                icon = "💰TP"
                icon_color = "green bold"
            elif result == "WIN":
                icon = "✓"
                icon_color = "green"
            else:
                icon = "✗"
                icon_color = "red"

            side_color = "green" if t["side"] == "YES" else "red"
            sym        = t.get("symbol", "?")
            sym_color  = SYM_COLORS.get(sym, "white")
            exit_p     = t.get("exit_price", t.get("btc_exit", 0))

            # Formatear strike y sal según el símbolo
            strike_fmt = f"${t['strike']:,.4f}" if t['strike'] < 100 else f"${t['strike']:,.2f}"
            sal_fmt    = f"${exit_p:,.4f}"      if exit_p < 100      else f"${exit_p:,.2f}"

            table.add_row(
                t["time"],
                f"[{tf_color}]{timeframe}[/]",
                f"[{sym_color}]{sym}[/]",
                f"[{side_color}]{t['side']}[/]",
                strike_fmt,
                sal_fmt,
                f"{t['entry_price']:.3f}",
                f"[{pnl_color}]${pnl:+.4f}[/]",
                f"[{icon_color}]{icon}[/]",
            )

    return Panel(table, title="[bold cyan]ÚLTIMOS TRADES[/]", box=box.ROUNDED)


def build_waiting() -> Panel:
    return Panel(
        "[yellow]Esperando al bot...\n\nAsegúrate de que el bot está corriendo:[/]\n"
        "[dim]venv/bin/python3 main.py[/]",
        title="[bold red]SIN DATOS[/]",
        box=box.ROUNDED,
    )


def make_layout(state: dict | None) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header",  size=3),
        Layout(name="body"),
        Layout(name="trades",  size=15),
    )
    layout["body"].split_row(
        Layout(name="stats",   ratio=1),
        Layout(name="right",   ratio=3),
    )
    layout["right"].split_column(
        Layout(name="positions", ratio=1),
        Layout(name="markets",   ratio=2),
    )

    if state is None:
        layout["header"].update(Panel("[red]BOT NO ACTIVO[/]", box=box.DOUBLE_EDGE))
        layout["stats"].update(build_waiting())
        layout["positions"].update(Panel("", title="POSICIONES"))
        layout["markets"].update(Panel("", title="MERCADOS"))
        layout["trades"].update(Panel("", title="TRADES"))
    else:
        layout["header"].update(build_header(state))
        layout["stats"].update(build_stats(state))
        layout["positions"].update(build_positions(state))
        layout["markets"].update(build_markets(state))
        layout["trades"].update(build_trades(state))

    return layout


def main() -> None:
    console = Console()
    console.clear()
    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            state = load_state()
            live.update(make_layout(state))
            time.sleep(REFRESH_RATE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
