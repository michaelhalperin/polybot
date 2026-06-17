"""Tests for the performance/calibration report (synthetic resolved trades)."""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polybot.report import build_report, format_text, _wilson
from polybot.store import Store


def fresh_store():
    st = Store(tempfile.mktemp(suffix=".db"))
    st.set_meta("starting_bankroll", 200.0)
    st.set_meta("cash", 200.0)
    return st


def add_closed(st, fair, won, kind="crypto", reason="resolved", cost=10.0,
               ai=1, pnl=None):
    realized = (cost * 0.4 if won else -cost * 0.5) if pnl is None else pnl
    st.add_position(
        condition_id="c" + str(time.time_ns()), token_id="t", market="m",
        side_name="Yes", kind=kind, shares=10.0, avg_cost=cost / 10, cost_usd=cost,
        fair_at_entry=fair, group_id=None, theme="x", ai_verified=ai,
        status="closed", opened_at=time.time(), closed_at=time.time(),
        exit_price=(1.0 if won else 0.0), proceeds_usd=cost + realized,
        realized_pnl=realized, exit_reason=reason)


def test_empty_report():
    r = build_report(fresh_store())
    assert r["resolved"] == 0 and r["stage"] == "too_early"
    assert "No resolved trades" not in format_text(r)  # text still renders


def test_win_rate_and_pnl():
    st = fresh_store()
    for _ in range(6):
        add_closed(st, 0.7, won=True)
    for _ in range(4):
        add_closed(st, 0.7, won=False)
    r = build_report(st)
    assert r["resolved"] == 10
    assert r["wins"] == 6 and r["losses"] == 4
    assert abs(r["win_rate"] - 0.6) < 1e-9
    # 6 wins * +4 - 4 losses * 5 = 24 - 20 = +4
    assert abs(r["realized_pnl"] - 4.0) < 1e-6
    assert r["win_rate_ci"][0] < 0.6 < r["win_rate_ci"][1]


def test_calibration_buckets():
    st = fresh_store()
    # bucket 0.6-0.8: 10 bets at 0.7, 7 win -> actual 70% (well calibrated)
    for i in range(10):
        add_closed(st, 0.7, won=(i < 7))
    r = build_report(st)
    bucket = next(b for b in r["calibration"] if b["lo"] == 0.6)
    assert bucket["n"] == 10
    assert abs(bucket["predicted"] - 0.7) < 1e-9
    assert abs(bucket["actual"] - 0.7) < 1e-9


def test_arbitrage_excluded_from_calibration():
    st = fresh_store()
    add_closed(st, 1.0, won=True, kind="arbitrage")
    r = build_report(st)
    assert r["settled_for_calibration"] == 0  # arb not counted


def test_ai_split():
    st = fresh_store()
    add_closed(st, 0.7, won=True, ai=1)
    add_closed(st, 0.7, won=False, ai=0)
    r = build_report(st)
    assert r["ai_split"]["ai_verified"]["n"] == 1
    assert r["ai_split"]["not_verified"]["n"] == 1


def test_readiness_stages():
    st = fresh_store()
    for _ in range(120):
        add_closed(st, 0.6, won=True)
    assert build_report(st)["stage"] == "meaningful"


def test_wilson_sane():
    lo, hi = _wilson(5, 10)
    assert 0 <= lo < 0.5 < hi <= 1


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
