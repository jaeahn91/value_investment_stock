"""test_allocator.py — synthetic-input tests for the PURE allocator core (spec §12.7).

No Stage-3 BUYs exist yet, so these drive size() directly with hand-built verdicts
and prices — no network, no clock. Runnable two ways:

    .venv\\Scripts\\python.exe -m tests.test_allocator     # stdlib runner (no pytest)
    pytest tests/test_allocator.py                         # if pytest is installed
"""

from __future__ import annotations

import dataclasses
import sys

from allocate.allocator import size


def base_cfg(**over) -> dict:
    a = {
        "max_weight_per_stock": 0.15,
        "max_weight_per_sector": 0.30,
        "cash_floor": 0.05,
        "cash_ceiling": 0.30,
        "allow_hold_cash": True,
        "tier_weights": {"deep_value": 1.5, "add_zone": 1.0},
        "deep_value_discount": 0.90,
        "rebalance_pull": 0.5,
        "rebalance_pull_exclude_tags": ["catalyst_bet"],
    }
    a.update(over)
    return {"allocation": a}


def _by_ticker(result):
    return {o.ticker: o for o in result.orders}


# ---------------------------------------------------------------- basics


def test_two_addzone_split_floor_and_leftover():
    """Two equal add_zone names, sub-cap: proportional split, whole-share floor, leftover→cash."""
    cands = [
        {"ticker": "000001", "name": "A", "sector": "secA", "buy_below_krw": 45000},
        {"ticker": "000002", "name": "B", "sector": "secB", "buy_below_krw": 32000},
    ]
    pf = {"cash": 1_000_000, "positions": []}
    prices = {"000001": 44000, "000002": 30000}
    r = size(cands, pf, prices, budget_krw=100_000, cfg=base_cfg())

    o = _by_ticker(r)
    assert set(o) == {"000001", "000002"}, o
    assert o["000001"].shares == 1 and o["000001"].tier == "add_zone"
    assert o["000002"].shares == 1
    assert o["000001"].amount_krw == 44000 and o["000002"].amount_krw == 30000
    assert r.leftover_cash_krw == 100_000 - 74_000 == 26_000
    assert r.held_cash_reason is None
    # V = cash = 1,000,000 → cash weight after spending 74,000
    assert abs(r.post_buy_weights["cash"] - (1_000_000 - 74_000) / 1_000_000) < 1e-9


def test_deep_value_tier_gets_more():
    """Same price, but deep_value (1.5×) should receive a larger allocation than add_zone."""
    cands = [
        {"ticker": "000001", "name": "DV", "sector": "secA", "buy_below_krw": 50000},   # 40k ≤ 45k → deep_value
        {"ticker": "000002", "name": "AZ", "sector": "secB", "buy_below_krw": 42000},   # 40k ≤ 42k → add_zone
    ]
    pf = {"cash": 1_000_000, "positions": []}
    prices = {"000001": 40000, "000002": 40000}
    r = size(cands, pf, prices, budget_krw=200_000, cfg=base_cfg())

    o = _by_ticker(r)
    assert o["000001"].tier == "deep_value"
    assert o["000002"].tier == "add_zone"
    assert o["000001"].shares == 3 and o["000002"].shares == 2, o
    assert o["000001"].shares > o["000002"].shares


# ---------------------------------------------------------------- caps


def test_stock_cap_enforced():
    """A single name with a large budget is capped at max_weight_per_stock (15%)."""
    cands = [{"ticker": "000001", "name": "A", "sector": "secA", "buy_below_krw": 11000}]
    pf = {"cash": 2_000_000, "positions": []}
    prices = {"000001": 10000}
    r = size(cands, pf, prices, budget_krw=1_000_000, cfg=base_cfg())

    o = _by_ticker(r)
    assert o["000001"].shares == 30                      # 300,000 / 10,000
    assert o["000001"].amount_krw == 300_000
    assert abs(r.post_buy_weights["000001"] - 0.15) < 1e-9   # exactly at the stock cap
    assert any("capped" in c and "stock" in c for c in r.constraints_hit), r.constraints_hit
    assert r.leftover_cash_krw == 700_000


def test_sector_cap_enforced():
    """Three names in one sector: the sector cap (30%) binds even though stock caps don't."""
    cands = [
        {"ticker": "000001", "name": "A", "sector": "secX", "buy_below_krw": 11000},
        {"ticker": "000002", "name": "B", "sector": "secX", "buy_below_krw": 11000},
        {"ticker": "000003", "name": "C", "sector": "secX", "buy_below_krw": 11000},
    ]
    pf = {"cash": 2_000_000, "positions": []}
    prices = {"000001": 10000, "000002": 10000, "000003": 10000}
    # stock cap raised to 0.50 so ONLY the sector cap can bind
    r = size(cands, pf, prices, budget_krw=1_000_000, cfg=base_cfg(max_weight_per_stock=0.50))

    o = _by_ticker(r)
    total_sector = sum(x.amount_krw for x in r.orders)
    assert total_sector == 600_000                       # 0.30 × 2,000,000 = sector cap
    assert "000003" not in o                             # squeezed out by the sector cap
    assert any("sector" in c for c in r.constraints_hit), r.constraints_hit


# ---------------------------------------------------------------- rebalance pull


def test_catalyst_bet_gets_no_pull():
    """A held catalyst_bet name gets no underweight pull; an identical fresh name outdraws it."""
    cands = [
        {"ticker": "000001", "name": "CAT", "sector": "secA", "buy_below_krw": 11000},
        {"ticker": "000002", "name": "NEW", "sector": "secB", "buy_below_krw": 11000},
    ]
    pf = {"cash": 990_000, "positions": [
        {"ticker": "000001", "name": "CAT", "shares": 1, "sector": "secA", "tag": "catalyst_bet"},
    ]}
    prices = {"000001": 10000, "000002": 10000}          # V = 10,000 + 990,000 = 1,000,000
    r = size(cands, pf, prices, budget_krw=200_000, cfg=base_cfg())

    o = _by_ticker(r)
    assert o["000002"].shares == 12 and o["000001"].shares == 8, o
    assert o["000002"].shares > o["000001"].shares       # fresh (pulled) beats catalyst_bet (not pulled)
    assert "pull excluded" in o["000001"].rationale


# ---------------------------------------------------------------- cash-is-a-position


def test_nothing_in_buy_zone_holds_cash():
    cands = [{"ticker": "000001", "name": "A", "sector": "secA", "buy_below_krw": 50000}]
    pf = {"cash": 1_000_000, "positions": []}
    prices = {"000001": 60000}                            # above buy_below → not in a zone
    r = size(cands, pf, prices, budget_krw=100_000, cfg=base_cfg())

    assert r.orders == []
    assert r.held_cash_reason and "buy zone" in r.held_cash_reason
    assert r.leftover_cash_krw == 100_000
    assert r.diagnostics["watch_above_buy_below"][0]["ticker"] == "000001"


def test_deployable_non_positive_holds_cash():
    """Cash sitting at/below the floor → deployable ≤ 0 → hold, don't force a sale/deploy."""
    cands = [{"ticker": "000001", "name": "A", "sector": "secA", "buy_below_krw": 6000}]
    pf = {"cash": 40_000, "positions": [
        {"ticker": "005930", "name": "X", "shares": 100, "sector": "secZ", "tag": "legacy"},
    ]}
    prices = {"000001": 5000, "005930": 10000}            # V = 1,000,000 + 40,000; floor = 52,000 > cash
    r = size(cands, pf, prices, budget_krw=100_000, cfg=base_cfg())

    assert r.orders == []
    assert r.held_cash_reason and "deployable cash" in r.held_cash_reason
    assert r.leftover_cash_krw == 100_000


def test_allow_hold_cash_false_still_holds_when_unbuyable():
    cands = [{"ticker": "000001", "name": "A", "sector": "secA", "buy_below_krw": 50000}]
    pf = {"cash": 1_000_000, "positions": []}
    prices = {"000001": 60000}
    r = size(cands, pf, prices, budget_krw=100_000, cfg=base_cfg(allow_hold_cash=False))
    assert r.orders == []
    assert "allow_hold_cash=false" in r.held_cash_reason


# ---------------------------------------------------------------- determinism & guards


def test_greedy_tiebreak_is_deterministic():
    """Equal score & price: the remainder share goes to the lower ticker (spec §12.5 step 3)."""
    cands = [
        {"ticker": "000009", "name": "Z", "sector": "secA", "buy_below_krw": 42000},
        {"ticker": "000001", "name": "A", "sector": "secB", "buy_below_krw": 42000},
    ]
    pf = {"cash": 1_000_000, "positions": []}
    prices = {"000009": 40000, "000001": 40000}
    r = size(cands, pf, prices, budget_krw=70_000, cfg=base_cfg())

    o = _by_ticker(r)
    assert o["000001"].shares == 1                       # lower ticker wins the single affordable share
    assert "000009" not in o
    # identical inputs → identical output
    r2 = size(cands, pf, prices, budget_krw=70_000, cfg=base_cfg())
    assert [dataclasses.asdict(x) for x in r.orders] == [dataclasses.asdict(x) for x in r2.orders]


def test_missing_price_is_loud():
    cands = [{"ticker": "000001", "name": "A", "sector": "secA", "buy_below_krw": 11000}]
    pf = {"cash": 1_000_000, "positions": []}
    try:
        size(cands, pf, {}, budget_krw=100_000, cfg=base_cfg())
    except ValueError as e:
        assert "missing prices" in str(e)
    else:
        raise AssertionError("expected ValueError on missing candidate price")


def test_missing_holding_price_is_loud():
    cands = [{"ticker": "000001", "name": "A", "sector": "secA", "buy_below_krw": 11000}]
    pf = {"cash": 500_000, "positions": [
        {"ticker": "005930", "name": "X", "shares": 10, "sector": "secZ", "tag": "legacy"},
    ]}
    try:
        size(cands, pf, {"000001": 10000}, budget_krw=100_000, cfg=base_cfg())
    except ValueError as e:
        assert "005930" in str(e)
    else:
        raise AssertionError("expected ValueError when a holding's price is missing")


# ---------------------------------------------------------------- stdlib runner


def _run() -> int:
    try:                                                  # cp949 consoles choke on ₩ in messages
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        except Exception as e:                            # noqa: BLE001
            print(f"ERROR {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(_run())
