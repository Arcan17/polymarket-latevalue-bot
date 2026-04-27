"""
audit.py — Análisis completo de trades.log para auditoría del lunes.
Ejecutar: python3 audit.py [ruta_trades.log] [--from=YYYY-MM-DD]

Métricas incluidas:
  1. Sanity checks básicos
  2. Win rate y Z-score vs mercado
  3. Bootstrap confidence interval en PnL
  4. Correlación edge → PnL (Spearman)
  5. Calibración del modelo (ECE, Brier Score)
  6. Análisis de fees
  7. Red flags automáticos
  8. Breakdown por símbolo, lado, fuente de book
  9. Trailing 20-trade rolling PnL (¿se está deteriorando?)
 10. Dirección correcta vs win rate
 11. Análisis por settle_source, TTE y volatilidad
 12. Decisión: ¿listo para live?
 13. Verificación contra API de Polymarket
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
import numpy as np
from pathlib import Path
from collections import defaultdict

# ─── Verificación contra Polymarket API ──────────────────────────────────────

def _fetch_polymarket_result(market_id: str, token_id: str) -> dict | None:
    """
    Consulta la API de Polymarket para obtener el resultado real de un mercado.
    Retorna dict con: resolved, winning_token_id, outcome_prices
    """
    try:
        url = f"https://clob.polymarket.com/markets/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "audit-script/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        tokens = data.get("tokens", [])
        outcome_prices = {t["token_id"]: float(t.get("price", 0)) for t in tokens}

        # Mercado resuelto: el token ganador tiene precio 1.0
        winning_token = None
        for t in tokens:
            if float(t.get("price", 0)) >= 0.99:
                winning_token = t["token_id"]
                break

        return {
            "resolved":         data.get("closed", False),
            "winning_token_id": winning_token,
            "outcome_prices":   outcome_prices,
            "our_token_won":    winning_token == token_id if winning_token else None,
        }
    except Exception:
        return None


def _verify_against_polymarket(trades: list[dict]) -> None:
    """
    Compara resultado del bot vs resultado real de Polymarket para cada trade.
    Solo funciona en trades que ya están resueltos en Polymarket.
    """
    correct = 0
    wrong   = 0
    pending = 0
    errors  = 0
    wrong_trades = []

    # Deduplicar por market_id para no spamear la API
    seen_markets: dict[str, dict] = {}

    for t in trades:
        mid = t.get("market_id")
        tid = t.get("token_id")
        if not mid or not tid:
            continue

        # Cache para no repetir misma consulta
        if mid not in seen_markets:
            result = _fetch_polymarket_result(mid, tid)
            seen_markets[mid] = result
            time.sleep(0.1)  # no spamear la API
        else:
            # Mismo market_id, diferente token (YES/NO del mismo mercado)
            cached = seen_markets[mid]
            if cached:
                # Recalcular our_token_won para este token específico
                result = {**cached, "our_token_won": cached["winning_token_id"] == tid}
            else:
                result = None

        if result is None:
            errors += 1
            continue

        if not result["resolved"]:
            pending += 1
            continue

        bot_won       = t["result"] == "WIN"
        poly_won      = result["our_token_won"]

        if poly_won is None:
            pending += 1
            continue

        if bot_won == poly_won:
            correct += 1
        else:
            wrong += 1
            wrong_trades.append({
                "entry_time": t.get("entry_time"),
                "symbol":     t.get("symbol"),
                "side":       t.get("side"),
                "strike_bot": t.get("strike"),
                "bot_result": t.get("result"),
                "poly_result": "WIN" if poly_won else "LOSS",
                "pnl_logged": t.get("pnl"),
                "strike_confirmed": t.get("strike_confirmed"),
            })

    total_verified = correct + wrong
    print(f"\n  Resueltos verificados : {total_verified}")
    print(f"  Pendientes/sin datos  : {pending + errors}")

    if total_verified == 0:
        print(yellow("  ⚠ No hay trades resueltos verificables aún (mercados muy recientes)"))
        return

    match_rate = correct / total_verified
    if match_rate >= 0.98:
        print(green(f"  ✓ Coincidencia bot vs Polymarket: {match_rate:.1%} ({correct}/{total_verified})"))
        print(green("    Los resultados del bot son fiables."))
    elif match_rate >= 0.90:
        print(yellow(f"  ⚠ Coincidencia: {match_rate:.1%} — {wrong} trades con resultado incorrecto"))
    else:
        print(red(f"  ✗ Solo {match_rate:.1%} coinciden — resultados del bot NO son fiables"))

    if wrong_trades:
        print(f"\n  Trades con resultado INCORRECTO ({len(wrong_trades)}):")
        print(f"  {'Fecha':<20} {'Sym':<5} {'Lado':<5} {'Strike':<12} {'Bot':<6} {'Poly':<6} {'PnL':>8} {'Conf':>5}")
        print(f"  {'-'*70}")
        for wt in wrong_trades:
            conf = "✓" if wt.get("strike_confirmed") else "✗"
            print(
                f"  {str(wt['entry_time']):<20} {str(wt['symbol']):<5} {str(wt['side']):<5} "
                f"${wt['strike_bot']:<11} {str(wt['bot_result']):<6} {str(wt['poly_result']):<6} "
                f"${wt['pnl_logged']:>7.4f} {conf:>5}"
            )
        # PnL ajustado si los resultados estuvieran al revés
        pnl_correction = 0.0
        for wt in wrong_trades:
            pnl_logged = wt["pnl_logged"]
            if wt["bot_result"] == "WIN":
                pnl_correction += -pnl_logged - 1.02
            else:
                pnl_correction += -pnl_logged + 0.85
        print(f"\n  Corrección de PnL si se ajustan estos trades: ${pnl_correction:+.4f}")


# ─── Carga de datos ───────────────────────────────────────────────────────────

def load_trades(path: str = "trades.log", from_date: str = None) -> list[dict]:
    trades = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    t = json.loads(line)
                    if from_date and t.get("entry_time", "") < from_date:
                        continue
                    trades.append(t)
                except json.JSONDecodeError:
                    pass
    return trades


# ─── Helpers ─────────────────────────────────────────────────────────────────

SEP  = "─" * 62
SEP2 = "═" * 62

def header(title: str):
    print(f"\n{SEP2}")
    print(f"  {title}")
    print(SEP2)

def subheader(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def green(s):  return f"\033[92m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"

def traffic(val, g, y):
    """g=green threshold, y=yellow threshold (assumes higher is better)."""
    if val >= g:   return green(f"{val:.4f}")
    elif val >= y: return yellow(f"{val:.4f}")
    else:          return red(f"{val:.4f}")

def traffic_low(val, g, y):
    """Lower is better (e.g. ECE, Brier)."""
    if val <= g:   return green(f"{val:.4f}")
    elif val <= y: return yellow(f"{val:.4f}")
    else:          return red(f"{val:.4f}")


# ─── Análisis principal ───────────────────────────────────────────────────────

def run_audit(path: str = "trades.log", from_date: str = None):
    trades = load_trades(path, from_date)
    if not trades:
        print("No hay trades en el log.")
        return

    header(f"AUDITORÍA LATE VALUE BOT — {len(trades)} trades")
    if from_date:
        print(f"  Filtrando trades desde: {from_date}")

    # ── 1. SANITY CHECKS ──────────────────────────────────────────────────────
    subheader("1. Sanity Checks")

    errors = []
    for i, t in enumerate(trades):
        ep = t.get("entry_price", 0)
        if not (0.01 <= ep <= 0.99):
            errors.append(f"  Trade {i}: entry_price={ep} fuera de rango")
        prob = t.get("our_prob", 0)
        if not (0.0 <= prob <= 1.0):
            errors.append(f"  Trade {i}: our_prob={prob} fuera de rango")
        result = t.get("result")
        if result not in ("WIN", "LOSS"):
            errors.append(f"  Trade {i}: result='{result}' inesperado")
        if t.get("edge", 0) <= 0:
            errors.append(f"  Trade {i}: edge={t.get('edge')} ≤ 0 (entrada sin ventaja)")

    if errors:
        for e in errors:
            print(red(e))
    else:
        print(green("  ✓ Todos los campos válidos"))

    # Convertir a arrays numéricos
    entry_prices = np.array([t["entry_price"] for t in trades])
    our_probs    = np.array([t["our_prob"]    for t in trades])
    edges        = np.array([t["edge"]        for t in trades])
    results_bin  = np.array([1 if t["result"] == "WIN" else 0 for t in trades])
    pnl_arr      = np.array([t["pnl"]         for t in trades])
    sizes        = np.array([t.get("size_usdc", 1.0) for t in trades])

    # ── 2. ESTADÍSTICAS BÁSICAS ───────────────────────────────────────────────
    subheader("2. Estadísticas Básicas")

    n_trades  = len(trades)
    n_wins    = int(results_bin.sum())
    n_losses  = n_trades - n_wins
    win_rate  = n_wins / n_trades
    total_pnl = pnl_arr.sum()
    mean_pnl  = pnl_arr.mean()
    total_bet = sizes.sum()
    roi       = total_pnl / total_bet if total_bet > 0 else 0

    print(f"  Trades totales : {n_trades}")
    print(f"  Ganados        : {n_wins}  ({win_rate:.1%})")
    print(f"  Perdidos       : {n_losses}")
    print(f"  PnL total      : ${total_pnl:+.4f}")
    print(f"  PnL medio/trade: ${mean_pnl:+.4f}")
    print(f"  Total apostado : ${total_bet:.2f}")
    print(f"  ROI            : {roi:+.2%}")
    print(f"  Edge medio     : {edges.mean():.3f}")
    print(f"  Edge medio WIN : {edges[results_bin==1].mean():.3f}" if n_wins > 0 else "")
    print(f"  Edge medio LOSS: {edges[results_bin==0].mean():.3f}" if n_losses > 0 else "")

    # ── 3. Z-SCORE ────────────────────────────────────────────────────────────
    subheader("3. Z-Score (¿Edge real vs azar?)")

    # H0: win rate = market implied (entry_price)
    expected_wins = entry_prices.sum()
    variance      = (entry_prices * (1 - entry_prices)).sum()
    z_score       = (results_bin.sum() - expected_wins) / (variance ** 0.5)

    z_str = traffic(z_score, 2.0, 1.5)
    print(f"  Wins esperados (por precio mercado): {expected_wins:.1f}")
    print(f"  Wins reales                        : {results_bin.sum()}")
    print(f"  Z-score                            : {z_str}")
    if z_score >= 2.0:
        print(green("  ✓ Estadísticamente significativo al 95%"))
    elif z_score >= 1.5:
        print(yellow("  ⚠ Señal moderada — necesitas más trades para confirmar"))
    else:
        print(red("  ✗ No hay evidencia estadística de edge real aún"))

    print(f"\n  Nota: con {n_trades} trades necesitas ~400 para detectar edge del 2%")
    print(f"        con edge ≥3% puedes detectarlo en ~150 trades")

    # ── 4. BOOTSTRAP CI ───────────────────────────────────────────────────────
    subheader("4. Bootstrap — Intervalo de Confianza 95% en PnL medio")

    rng = np.random.default_rng(42)
    boots = [rng.choice(pnl_arr, size=n_trades, replace=True).mean() for _ in range(10_000)]
    ci_low, ci_high = np.percentile(boots, [2.5, 97.5])

    ci_str = f"[${ci_low:+.4f}, ${ci_high:+.4f}]"
    if ci_low > 0:
        print(green(f"  ✓ CI 95%: {ci_str} — expectativa positiva confirmada"))
    elif ci_high > 0:
        print(yellow(f"  ⚠ CI 95%: {ci_str} — cruza el cero (insuficiente muestra)"))
    else:
        print(red(f"  ✗ CI 95%: {ci_str} — expectativa negativa"))

    # ── 5. CORRELACIÓN EDGE → PnL ─────────────────────────────────────────────
    subheader("5. Correlación Spearman: Edge → PnL")

    try:
        from scipy.stats import spearmanr
        corr, pval = spearmanr(edges, pnl_arr)
        corr_str = traffic(corr, 0.15, 0.05)
        p_str = green(f"p={pval:.3f}") if pval < 0.05 else yellow(f"p={pval:.3f}")
        print(f"  Spearman r = {corr_str}  {p_str}")
        if corr >= 0.15 and pval < 0.05:
            print(green("  ✓ Edge del modelo se traduce en ganancias reales"))
        elif corr > 0:
            print(yellow("  ⚠ Correlación positiva pero débil o no significativa"))
        else:
            print(red("  ✗ Edge no correlaciona con PnL — revisar modelo"))
    except ImportError:
        # Cálculo manual sin scipy
        def rank(x):
            order = np.argsort(x)
            r = np.empty_like(order, dtype=float)
            r[order] = np.arange(len(x)) + 1
            return r
        re, rp = rank(edges), rank(pnl_arr)
        n = len(re)
        corr = 1 - 6 * ((re - rp)**2).sum() / (n * (n**2 - 1))
        print(f"  Spearman r = {traffic(corr, 0.15, 0.05)} (scipy no disponible, p-value omitido)")

    # ── 6. CALIBRACIÓN ────────────────────────────────────────────────────────
    subheader("6. Calibración del Modelo (ECE y Brier Score)")

    # Brier Score
    brier = ((our_probs - results_bin) ** 2).mean()
    print(f"  Brier Score: {traffic_low(brier, 0.20, 0.25)}")
    print(f"    (Referencia: sin habilidad=0.25, bien calibrado<0.20)")

    # ECE por bins de 5%
    bins = np.arange(0.50, 1.05, 0.05)
    bin_labels = [f"{b:.2f}-{b+0.05:.2f}" for b in bins[:-1]]
    ece_num = 0.0
    print(f"\n  Reliability Diagram (predicted vs actual):")
    print(f"  {'Bin':<14} {'Pred':>6} {'Actual':>8} {'N':>5} {'Diff':>7}")
    for b_low, b_high, label in zip(bins[:-1], bins[1:], bin_labels):
        mask = (our_probs >= b_low) & (our_probs < b_high)
        if mask.sum() == 0:
            continue
        mean_pred   = our_probs[mask].mean()
        mean_actual = results_bin[mask].mean()
        n_bin       = mask.sum()
        diff        = abs(mean_pred - mean_actual)
        ece_num    += (n_bin / n_trades) * diff
        diff_str    = green(f"{diff:+.3f}") if diff < 0.05 else (yellow(f"{diff:+.3f}") if diff < 0.10 else red(f"{diff:+.3f}"))
        print(f"  {label:<14} {mean_pred:>6.3f} {mean_actual:>8.3f} {n_bin:>5}   {diff_str}")

    print(f"\n  ECE: {traffic_low(ece_num, 0.04, 0.08)}")
    if ece_num < 0.04:
        print(green("  ✓ Modelo bien calibrado"))
    elif ece_num < 0.08:
        print(yellow("  ⚠ Calibración moderada — revisar bins con mayor desviación"))
    else:
        print(red("  ✗ Modelo mal calibrado — las probabilidades no son fiables"))

    # ── 7. ANÁLISIS DE FEES ───────────────────────────────────────────────────
    subheader("7. Análisis de Fees (Polymarket 7.2% taker)")

    TAKER_FEE_RATE = 0.072

    # Fee de compra: siempre se paga
    fees_buy = sizes * TAKER_FEE_RATE * (1 - entry_prices)

    # Fee de venta: solo en TP (settle_source == "TAKE-PROFIT")
    # Para TP usamos token_exit_price si está disponible, sino entry_price+0.15 como proxy
    fees_sell = np.zeros(len(trades))
    for i, t in enumerate(trades):
        if t.get("settle_source") == "TAKE-PROFIT":
            tex = t.get("token_exit_price") or (t.get("entry_price", 0) + 0.15)
            shares = sizes[i] / entry_prices[i]
            fees_sell[i] = shares * tex * TAKER_FEE_RATE * (1 - tex)

    fees       = fees_buy + fees_sell
    total_fees = fees.sum()
    tp_count   = int((fees_sell > 0).sum())

    # PnL bruto (sin ningún fee)
    gross_pnl = np.where(
        results_bin == 1,
        sizes / entry_prices - sizes,   # shares × $1 - cost
        -sizes
    )
    net_pnl = gross_pnl - fees

    print(f"  Fees compra             : ${fees_buy.sum():.4f}")
    if tp_count > 0:
        print(f"  Fees venta (TP x{tp_count})     : ${fees_sell.sum():.4f}")
    print(f"  Fees totales pagadas    : ${total_fees:.4f}")
    print(f"  PnL bruto (sin fees)    : ${gross_pnl.sum():+.4f}")
    print(f"  PnL neto (con fees)     : ${net_pnl.sum():+.4f}")
    print(f"  Fees como % del bruto   : {total_fees / max(abs(gross_pnl.sum()), 0.001):.1%}")

    # Edge mínimo requerido por trade para cubrir fees
    min_edges_yes = TAKER_FEE_RATE * (1 - entry_prices)  # simplificado YES
    above_threshold = (edges > min_edges_yes).mean()
    print(f"\n  Trades con edge > umbral de fees: {above_threshold:.1%}")
    if above_threshold >= 0.70:
        print(green("  ✓ Mayoría de trades superan el coste de fees"))
    elif above_threshold >= 0.50:
        print(yellow("  ⚠ Algunas entradas apenas cubren fees — subir MIN_EDGE"))
    else:
        print(red("  ✗ Muchas entradas no cubren fees — revisar MIN_EDGE urgentemente"))

    # ── 8. BREAKDOWNS ─────────────────────────────────────────────────────────
    subheader("8. Breakdown por Símbolo, Lado y Fuente de Book")

    def breakdown(key, label):
        groups = defaultdict(list)
        for t, p in zip(trades, pnl_arr):
            groups[t.get(key, "?")].append(p)
        print(f"\n  Por {label}:")
        print(f"  {'Valor':<14} {'N':>4} {'PnL':>10} {'Medio':>10} {'WR':>8}")
        for k, pnls in sorted(groups.items(), key=lambda x: str(x[0])):
            pnls = np.array(pnls)
            wins = [t for t in trades if t.get(key) == k and t["result"] == "WIN"]
            wr = len(wins) / len(pnls) if pnls.size > 0 else 0
            pnl_str = green(f"${pnls.sum():+.4f}") if pnls.sum() >= 0 else red(f"${pnls.sum():+.4f}")
            print(f"  {str(k):<14} {len(pnls):>4} {pnl_str:>20} ${pnls.mean():>+8.4f} {wr:>8.1%}")

    breakdown("symbol",       "Símbolo")
    breakdown("side",         "Lado (YES/NO)")
    breakdown("book_source",  "Fuente del book")
    breakdown("strike_confirmed", "Strike confirmado")

    # Breakdown por timeframe (5m vs 15m) — nuevo desde Apr 21
    if any("timeframe" in t for t in trades):
        breakdown("timeframe", "Timeframe (5m vs 15m)")

    # Breakdown por tipo de salida (EXP vs TP) — nuevo desde Apr 21
    tp_trades  = [t for t in trades if t.get("settle_source") == "TAKE-PROFIT"]
    exp_trades = [t for t in trades if t.get("settle_source") != "TAKE-PROFIT"]
    if tp_trades:
        print(f"\n  Por tipo de salida (EXP vs TP):")
        print(f"  {'Tipo':<14} {'N':>4} {'WR':>8} {'PnL medio':>12} {'PnL total':>12}")
        for label, grp in [("EXP", exp_trades), ("TAKE-PROFIT", tp_trades)]:
            if not grp:
                continue
            wr    = sum(1 for t in grp if t["result"] == "WIN") / len(grp)
            pnls  = np.array([t["pnl"] for t in grp])
            wr_str = green(f"{wr:.1%}") if wr >= 0.80 else (yellow(f"{wr:.1%}") if wr >= 0.65 else red(f"{wr:.1%}"))
            print(f"  {label:<14} {len(grp):>4} {wr_str:>18} ${pnls.mean():>+10.4f} ${pnls.sum():>+10.4f}")
        # Nota sobre rentabilidad del TP
        if len(tp_trades) >= 3:
            tp_avg   = np.mean([t["pnl"] for t in tp_trades])
            exp_avg  = np.mean([t["pnl"] for t in exp_trades]) if exp_trades else 0
            if tp_avg > exp_avg:
                print(green(f"\n  ✓ TP genera más PnL medio que EXP (+${tp_avg-exp_avg:+.4f}/trade)"))
            else:
                print(yellow(f"\n  ~ TP genera menos PnL medio que EXP (${tp_avg-exp_avg:+.4f}/trade) — normal si TP limita ganancias grandes"))
        else:
            print(yellow(f"\n  ⚠ Solo {len(tp_trades)} trade(s) TP — insuficiente para conclusiones"))

    # ── 9. ROLLING PnL (20 trades) ────────────────────────────────────────────
    subheader("9. Rolling PnL — 20 últimas operaciones (¿edge se mantiene?)")

    if n_trades >= 20:
        window = 20
        rolling_means = [pnl_arr[max(0,i-window):i].mean() for i in range(window, n_trades+1)]
        print(f"  PnL medio (primeros 20 trades) : ${rolling_means[0]:+.4f}")
        print(f"  PnL medio (últimos  20 trades) : ${rolling_means[-1]:+.4f}")
        trend = rolling_means[-1] - rolling_means[0]
        if trend >= 0:
            print(green(f"  ✓ Tendencia positiva: +${trend:+.4f} por trade"))
        elif trend >= -0.02:
            print(yellow(f"  ⚠ Ligera degradación: {trend:+.4f} por trade"))
        else:
            print(red(f"  ✗ Edge degradándose: {trend:+.4f} por trade — investigar"))

        # Mostrar últimas 5 ventanas
        print(f"\n  Últimas ventanas de 20:")
        step = max(1, len(rolling_means) // 5)
        for i in range(0, len(rolling_means), step):
            trade_num = i + window
            val = rolling_means[i]
            bar = "▓" * int(max(0, val) * 200)
            sign = green(f"${val:+.4f}") if val >= 0 else red(f"${val:+.4f}")
            print(f"    Trade #{trade_num:03d}: {sign}  {bar}")
    else:
        print(f"  Insuficientes trades ({n_trades}) para ventana de 20")

    # ── 10. CORRECT_DIRECTION CHECK ───────────────────────────────────────────
    if any("correct_direction" in t for t in trades):
        subheader("10. Dirección Correcta vs Win Rate")
        cd_arr = np.array([1 if t.get("correct_direction") else 0 for t in trades])
        cd_rate = cd_arr.mean()
        gap = cd_rate - win_rate
        print(f"  Win rate          : {win_rate:.3f}")
        print(f"  Correct direction : {cd_rate:.3f}")
        print(f"  Gap               : {gap:+.3f}")
        if abs(gap) < 0.03:
            print(green("  ✓ Dirección y resultado alineados — buen timing de entrada"))
        elif gap > 0.05:
            print(yellow("  ⚠ Aciertas dirección pero pierdes algunas — revisar precio entrada o fees"))
        else:
            print(red("  ✗ Gap grande — ganas sin acertar dirección (posible flip de NO a YES)"))

    # ── 11. ANÁLISIS ADICIONAL ────────────────────────────────────────────────
    subheader("11. Análisis: Settle Source, TTE y Volatilidad")

    # Settle source breakdown
    if any("settle_source" in t for t in trades):
        print("\n  Por settle_source:")
        print(f"  {'Source':<14} {'N':>4} {'WR':>8} {'PnL medio':>12}")
        ss_groups = defaultdict(list)
        for t in trades:
            ss_groups[t.get("settle_source", "?")].append(t)
        for src, grp in sorted(ss_groups.items()):
            wr = sum(1 for t in grp if t["result"]=="WIN") / len(grp)
            avg_pnl = np.mean([t["pnl"] for t in grp])
            wr_str = green(f"{wr:.1%}") if wr >= 0.80 else (yellow(f"{wr:.1%}") if wr >= 0.65 else red(f"{wr:.1%}"))
            print(f"  {src:<14} {len(grp):>4} {wr_str:>18} ${avg_pnl:>+10.4f}")
        if "RTDS-post" in ss_groups and "RTDS-pre" in ss_groups:
            post_wr = sum(1 for t in ss_groups["RTDS-post"] if t["result"]=="WIN") / len(ss_groups["RTDS-post"])
            pre_wr  = sum(1 for t in ss_groups["RTDS-pre"]  if t["result"]=="WIN") / len(ss_groups["RTDS-pre"])
            if post_wr > pre_wr + 0.05:
                print(green(f"\n  ✓ RTDS-post tiene mejor WR que RTDS-pre (+{post_wr-pre_wr:.1%})"))
            elif abs(post_wr - pre_wr) <= 0.05:
                print(yellow(f"\n  ~ Diferencia pequeña entre RTDS-post y RTDS-pre"))
            else:
                print(red(f"\n  ✗ RTDS-pre tiene MEJOR WR — revisar lógica de settlement"))

    # TTE analysis
    if any("tte_entry_s" in t for t in trades):
        tte_arr = np.array([t.get("tte_entry_s", 0) for t in trades])
        print(f"\n  Por ventana de entrada (TTE):")
        print(f"  {'Ventana':<20} {'N':>4} {'WR':>8} {'PnL medio':>12}")
        tte_bins = [(0, 30, "0-30s (muy tardío)"), (30, 60, "30-60s"), (60, 90, "60-90s"), (90, 120, "90-120s"), (120, 180, "120-180s (temprano)")]
        for lo, hi, label in tte_bins:
            mask = (tte_arr >= lo) & (tte_arr < hi)
            if mask.sum() == 0:
                continue
            grp_results = results_bin[mask]
            grp_pnl = pnl_arr[mask]
            wr = grp_results.mean()
            wr_str = green(f"{wr:.1%}") if wr >= 0.80 else (yellow(f"{wr:.1%}") if wr >= 0.65 else red(f"{wr:.1%}"))
            print(f"  {label:<20} {mask.sum():>4} {wr_str:>18} ${grp_pnl.mean():>+10.4f}")

    # Strike confirmed analysis
    if any("strike_confirmed" in t for t in trades):
        confirmed = [t for t in trades if t.get("strike_confirmed")]
        unconfirmed = [t for t in trades if not t.get("strike_confirmed")]
        if confirmed and unconfirmed:
            wr_conf   = sum(1 for t in confirmed   if t["result"]=="WIN") / len(confirmed)
            wr_unconf = sum(1 for t in unconfirmed if t["result"]=="WIN") / len(unconfirmed)
            print(f"\n  Strike confirmado vs estimado:")
            print(f"  Confirmado ({len(confirmed):>3}): WR={green(f'{wr_conf:.1%}') if wr_conf>=0.80 else yellow(f'{wr_conf:.1%}')}")
            print(f"  Estimado   ({len(unconfirmed):>3}): WR={green(f'{wr_unconf:.1%}') if wr_unconf>=0.80 else red(f'{wr_unconf:.1%}')}")
            if wr_conf > wr_unconf + 0.03:
                print(green(f"  ✓ Strike confirmado tiene mejor WR (+{wr_conf-wr_unconf:.1%}) — validado"))
            elif wr_conf < wr_unconf - 0.03:
                print(red(f"  ✗ Strike estimado tiene mejor WR — revisar lógica de captura"))
            else:
                print(yellow(f"  ~ Diferencia pequeña — no concluyente aún"))

    # ── 12. CRITERIOS DE LIVE ─────────────────────────────────────────────────
    subheader("12. Decisión: ¿Listo para Live?")

    score = 0
    max_score = 6
    criteria = []

    # 1. Z-score
    z_ok = z_score >= 2.0
    criteria.append((z_ok, z_score >= 1.5, f"Z-score={z_score:.2f} (necesita ≥2.0)", "Z-score bajo"))

    # 2. Bootstrap CI
    ci_ok = ci_low > 0
    criteria.append((ci_ok, ci_high > 0, f"CI 95% lower=${ci_low:+.4f} (necesita >0)", "CI cruza cero"))

    # 3. Spearman
    try:
        spear_ok  = corr >= 0.15
        spear_war = corr >= 0.05
    except NameError:
        spear_ok = spear_war = False
    criteria.append((spear_ok, spear_war, f"Spearman r={corr:.3f} (necesita ≥0.15)", "Correlación débil"))

    # 4. ECE
    ece_ok  = ece_num <= 0.04
    ece_war = ece_num <= 0.08
    criteria.append((ece_ok, ece_war, f"ECE={ece_num:.4f} (necesita ≤0.04)", "ECE alta"))

    # 5. Fees threshold
    fee_ok  = above_threshold >= 0.70
    fee_war = above_threshold >= 0.50
    criteria.append((fee_ok, fee_war, f"Trades>umbral fees: {above_threshold:.1%} (necesita ≥70%)", "Muchas entradas no cubren fees"))

    # 6. PnL neto positivo
    pnl_ok  = net_pnl.sum() > 0
    pnl_war = net_pnl.sum() > -2
    criteria.append((pnl_ok, pnl_war, f"PnL neto=${net_pnl.sum():+.4f} (necesita >0)", "PnL neto negativo"))

    print(f"\n  {'Criterio':<52} {'Estado':>8}")
    print(f"  {'-'*60}")
    for ok, warn, msg, fail_msg in criteria:
        if ok:
            status = green("   ✓ OK")
            score += 1
        elif warn:
            status = yellow("  ⚠ WARN")
        else:
            status = red("   ✗ NO")
        print(f"  {msg:<52} {status}")

    print(f"\n  Puntuación: {score}/{max_score}")
    if score >= 5:
        print(green(f"\n  ✓ LISTO PARA LIVE — {score}/6 criterios cumplidos"))
        print(green("    Empieza con $1/trade, máx $5 activo. Monitorea 1 hora."))
    elif score >= 3:
        print(yellow(f"\n  ⚠ CONTINÚA EN PAPER — {score}/6 criterios. Necesitas más trades."))
        print(yellow("    Objetivo: 100-150 trades antes de reevaluar."))
    else:
        print(red(f"\n  ✗ NO LISTO — solo {score}/6. Revisa el modelo."))

    print(f"\n  (Total trades actuales: {n_trades}. Objetivo mínimo: 100)")

    # ── 13. VERIFICACIÓN CONTRA POLYMARKET API ───────────────────────────────
    subheader("13. Verificación contra API de Polymarket (resultado real)")

    trades_with_id = [t for t in trades if t.get("market_id") and t.get("token_id")]
    if not trades_with_id:
        print(yellow("  ⚠ Trades sin market_id — solo trades nuevos (post-hoy) se pueden verificar."))
        print(yellow("    Los trades anteriores no guardaban market_id."))
        print(yellow("    A partir de ahora todos los trades incluyen market_id para verificación."))
    else:
        print(f"  Verificando {len(trades_with_id)} trades contra Polymarket API...")
        _verify_against_polymarket(trades_with_id)

    # ── RESUMEN FINAL ─────────────────────────────────────────────────────────
    header("RESUMEN EJECUTIVO")
    print(f"  Trades    : {n_trades} | WR: {win_rate:.1%} | PnL neto: ${net_pnl.sum():+.4f}")
    print(f"  Z-score   : {z_score:.2f} | ECE: {ece_num:.4f} | Brier: {brier:.4f}")
    print(f"  Edge medio: {edges.mean():.3f} | Fees totales: ${total_fees:.4f}")
    print(f"\n  Para ejecutar contra otro log:")
    print(f"    python3 audit.py /ruta/a/trades.log\n")


if __name__ == "__main__":
    log_path = "trades.log"
    from_date = None
    for arg in sys.argv[1:]:
        if arg.startswith("--from="):
            from_date = arg.split("=")[1]
        elif not arg.startswith("--"):
            log_path = arg
    try:
        run_audit(log_path, from_date)
    except FileNotFoundError:
        print(f"No se encontró el archivo: {log_path}")
    except Exception as e:
        import traceback
        print(f"Error en auditoría: {e}")
        traceback.print_exc()
