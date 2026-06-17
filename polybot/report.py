"""Performance & calibration report, built from RESOLVED (closed) trades.

The whole point: judge the bot on settled outcomes, not open marks. This
computes win rate (with a confidence interval), realized P&L, ROI, a
significance check, breakdowns by strategy / exit reason / AI-vetting, and a
**calibration table** — for settled probabilistic bets, do the markets the
model rated ~70% actually resolve YES ~70% of the time?

It also reports a *readiness* verdict: how many resolved trades you have and
whether that's enough to trust the result.
"""
from __future__ import annotations

import math
from typing import List

# Calibration buckets over the model's predicted win probability.
_BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001)]


def _wilson(wins: int, n: int, z: float = 1.96):
    """Wilson score interval for a win rate — honest error bars on small n."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _pnl(p) -> float:
    return float(p["realized_pnl"] or 0.0)


def _readiness(n: int):
    if n < 30:
        return ("too_early", "Sanity-check only — far too few resolved trades to judge profit.")
    if n < 100:
        return ("early", "Early read — directionally interesting, but wide error bars.")
    if n < 200:
        return ("meaningful", "Meaningful — a real first report; error bars still notable.")
    return ("solid", "Solid — enough resolved trades to take the result seriously.")


def build_report(store) -> dict:
    closed = [dict(r) for r in store.closed_positions()]
    n = len(closed)
    start = float(store.get_meta("starting_bankroll") or 0) or None
    cash = float(store.get_meta("cash") or (start or 0))
    open_cost = sum(float(p["cost_usd"] or 0) for p in store.open_positions())

    total_pnl = sum(_pnl(p) for p in closed)
    total_cost = sum(float(p["cost_usd"] or 0) for p in closed)
    wins = sum(1 for p in closed if _pnl(p) > 0)
    losses = sum(1 for p in closed if _pnl(p) < 0)
    win_rate = wins / n if n else 0.0
    ci_lo, ci_hi = _wilson(wins, n)
    roi = total_pnl / total_cost if total_cost else 0.0
    avg = total_pnl / n if n else 0.0

    # Is average P&L per trade distinguishable from zero? (rough t-stat)
    sig = None
    if n >= 2:
        var = sum((_pnl(p) - avg) ** 2 for p in closed) / (n - 1)
        se = math.sqrt(var) / math.sqrt(n) if var > 0 else 0.0
        t = avg / se if se > 0 else 0.0
        sig = {"t": t, "distinguishable": abs(t) >= 2.0}

    def group(key, normalize=lambda v: v):
        out = {}
        for p in closed:
            k = normalize(p.get(key) or "unknown")
            d = out.setdefault(k, {"n": 0, "pnl": 0.0, "wins": 0})
            d["n"] += 1
            d["pnl"] += _pnl(p)
            if _pnl(p) > 0:
                d["wins"] += 1
        return out

    by_kind = group("kind")
    by_reason = group("exit_reason", lambda v: str(v).split()[0])
    ai_split = {}
    for p in closed:
        k = "ai_verified" if p.get("ai_verified") else "not_verified"
        d = ai_split.setdefault(k, {"n": 0, "pnl": 0.0, "wins": 0})
        d["n"] += 1
        d["pnl"] += _pnl(p)
        if _pnl(p) > 0:
            d["wins"] += 1

    # Calibration: settled probabilistic bets only (exclude riskless arb).
    settled = [p for p in closed
               if (p.get("exit_reason") or "").startswith("resolved")
               and p.get("kind") in ("crypto", "value")]
    calibration = []
    for lo, hi in _BUCKETS:
        bucket = [p for p in settled if lo <= float(p["fair_at_entry"] or 0) < hi]
        if bucket:
            pred = sum(float(p["fair_at_entry"]) for p in bucket) / len(bucket)
            actual = sum(1 for p in bucket if float(p["exit_price"] or 0) >= 0.5) / len(bucket)
        else:
            pred = actual = None
        calibration.append({"lo": lo, "hi": min(hi, 1.0), "n": len(bucket),
                            "predicted": pred, "actual": actual})

    stage, stage_msg = _readiness(n)
    equity = cash + open_cost

    return {
        "resolved": n,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "win_rate_ci": [ci_lo, ci_hi],
        "realized_pnl": total_pnl,
        "deployed": total_cost,
        "roi": roi,
        "avg_pnl": avg,
        "significance": sig,
        "start_bankroll": start,
        "cash": cash,
        "open_cost": open_cost,
        "equity": equity,
        "equity_pct": ((equity / start - 1) * 100) if start else None,
        "settled_for_calibration": len(settled),
        "by_kind": by_kind,
        "by_reason": by_reason,
        "ai_split": ai_split,
        "calibration": calibration,
        "stage": stage,
        "stage_msg": stage_msg,
    }


def format_text(r: dict) -> str:
    """Render the report as a terminal-friendly string."""
    L = []
    bar = "=" * 66
    L.append(bar)
    L.append("POLYBOT PERFORMANCE REPORT")
    L.append(bar)
    L.append(f"  Readiness: [{r['stage'].upper()}] {r['stage_msg']}")
    L.append(f"  Resolved trades: {r['resolved']}   (wins {r['wins']} / losses {r['losses']})")
    if r["resolved"]:
        lo, hi = r["win_rate_ci"]
        L.append(f"  Win rate:      {r['win_rate']*100:5.1f}%   "
                 f"(95% CI {lo*100:.0f}–{hi*100:.0f}%)")
        L.append(f"  Realized P&L:  ${r['realized_pnl']:+.2f}   on ${r['deployed']:.2f} deployed   "
                 f"(ROI {r['roi']*100:+.1f}%)")
        L.append(f"  Avg / trade:   ${r['avg_pnl']:+.3f}")
        if r["significance"]:
            verdict = ("distinguishable from luck" if r["significance"]["distinguishable"]
                       else "NOT distinguishable from luck yet")
            L.append(f"  Edge vs luck:  {verdict} (t={r['significance']['t']:+.2f})")
    if r["start_bankroll"]:
        L.append(f"  Equity:        ${r['equity']:.2f}   (start ${r['start_bankroll']:.2f}, "
                 f"{r['equity_pct']:+.2f}%)")

    if r["by_kind"]:
        L.append("\n  By strategy:")
        for k, d in r["by_kind"].items():
            L.append(f"    {k:10s}  n={d['n']:4d}  P&L ${d['pnl']:+8.2f}  "
                     f"win {d['wins']/d['n']*100 if d['n'] else 0:4.0f}%")
    if r["by_reason"]:
        L.append("\n  By exit reason:")
        for k, d in r["by_reason"].items():
            L.append(f"    {k:12s}  n={d['n']:4d}  P&L ${d['pnl']:+8.2f}")
    if r["ai_split"]:
        L.append("\n  AI-vetted vs not:")
        for k, d in r["ai_split"].items():
            L.append(f"    {k:13s}  n={d['n']:4d}  P&L ${d['pnl']:+8.2f}  "
                     f"win {d['wins']/d['n']*100 if d['n'] else 0:4.0f}%")

    L.append(f"\n  Calibration (settled probabilistic bets: {r['settled_for_calibration']}):")
    L.append("    predicted   actual    n")
    any_cal = False
    for b in r["calibration"]:
        if b["n"] == 0:
            continue
        any_cal = True
        L.append(f"    {b['lo']*100:3.0f}-{b['hi']*100:3.0f}%   "
                 f"{b['predicted']*100:5.1f}%   {b['actual']*100:5.1f}%   {b['n']:4d}")
    if not any_cal:
        L.append("    (no settled probabilistic bets yet)")
    L.append(bar)
    return "\n".join(L)
