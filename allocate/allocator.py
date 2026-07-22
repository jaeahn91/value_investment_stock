"""allocator.py — Stage 4: monthly DCA sizing (Gate 3→4, code-enforced).

Design: implementation_spec.md §12 (finalized 2026-07-15). Consumes approved,
UNDERVALUED, non-expired verdicts + portfolio.yaml + this month's budget_krw;
produces runs/<run_id>/allocation.yaml (schema §3.4) and an append-only report.

Boundary (CLAUDE.md): decision-support only. This sizes a *proposal* against the
user's own config.yaml rules and records the rationale for each buy — it never
places a trade and never touches decisions.log.yaml (that is CP-3, manual fills,
outside the allocator's authority; spec §12.7).

Structure (spec §12.7):
  size()       PURE core — no network, no clock. Deterministic. Testable with
               synthetic verdicts+prices (no Stage-3 BUYs exist yet).
  allocate()   IO wrapper — Gate 3→4 verdict scan, price fetch, artifact writing.

All thresholds are read from config.yaml (CLAUDE.md: no magic numbers).

Usage:
  python -m allocate.allocator <budget_krw> [run_id]
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import glob
import math
import os
import sys

# NOTE: heavy imports (screens.quant_filter → collectors.krx_collector → pykrx, which
# logs in to KRX *at import time*) are deferred into the IO functions below. This keeps
# the pure size() core importable with no network and no credentials (spec §12.7), so
# tests can drive it on synthetic inputs alone.

# The hard boundary, restated on every generated report (CLAUDE.md).
REPORT_DISCLAIMER = (
    "> ⚠️ **의사결정 지원(decision-support)용입니다 — 투자 자문이 아니며, 자동매매가 아닙니다.**\n"
    ">\n"
    "> 이 리포트는 사용자가 `config.yaml`에 직접 정의한 규칙에 따라 후보와 배분을 *제안*할 뿐입니다.\n"
    "> 최종 판단·주문 실행·세금·수수료는 전적으로 사용자 책임이며, 작성 주체는 licensed advisor가\n"
    "> 아닙니다. 시스템은 주문을 실행하지 않습니다 (this system never places trades)."
)

_EPS = 1e-6


def _ascii_console() -> None:
    """Korean Windows consoles default to cp949, which cannot encode ₩ (and mangles
    Hangul). Progress goes to stdout; the authoritative output is the utf-8 report and
    allocation.yaml. Make stdout tolerant so a progress line never crashes a real run."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                      # noqa: BLE001 — best-effort only
        pass


# ============================================================ pure core (§12.2–12.6)


@dataclasses.dataclass
class Order:
    ticker: str
    name: str | None
    sector: str | None
    shares: int
    ref_price_krw: int
    amount_krw: int
    tier: str            # deep_value | add_zone
    rationale: str


@dataclasses.dataclass
class AllocationResult:
    orders: list          # list[Order]
    post_buy_weights: dict
    leftover_cash_krw: int
    constraints_hit: list
    held_cash_reason: str | None
    diagnostics: dict


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _order_key(x: dict):
    """Deterministic tie-break (spec §12.5 step 3): score desc → price asc → ticker asc."""
    return (-x["score"], x["price"], x["ticker"])


def size(candidates: list[dict], portfolio: dict, prices: dict,
         budget_krw: int, cfg: dict) -> AllocationResult:
    """Deterministic monthly sizing. PURE: no network, no clock (spec §12.7).

    candidates : Gate-3→4-passed verdicts, sector already resolved —
                 [{ticker, name, sector, buy_below_krw}, ...]
    portfolio  : parsed portfolio.yaml  {cash, positions:[{ticker,shares,sector,tag}]}
    prices     : {ticker: close_krw} covering every candidate AND every holding
    budget_krw : this month's deployment budget (a subset of cash)
    cfg        : full config dict; allocation rules under cfg['allocation']
    """
    a = cfg["allocation"]
    max_w_stock = float(a["max_weight_per_stock"])
    max_w_sector = float(a["max_weight_per_sector"])
    cash_floor = float(a["cash_floor"])
    tier_w = a["tier_weights"]
    dv_discount = float(a["deep_value_discount"])
    reb_pull = float(a["rebalance_pull"])
    exclude_tags = set(a.get("rebalance_pull_exclude_tags") or [])
    allow_hold = bool(a.get("allow_hold_cash", True))
    budget_krw = int(budget_krw)

    cash = int(portfolio.get("cash", 0) or 0)
    positions = portfolio.get("positions") or []
    held_shares, held_tag, held_sector = {}, {}, {}
    for p in positions:
        t = str(p["ticker"])
        held_shares[t] = int(p.get("shares", 0) or 0)
        held_tag[t] = p.get("tag")
        held_sector[t] = p.get("sector")

    # -- price coverage: V is undefined if any holding's price is missing (spec §12.2)
    need = set(held_shares) | {str(c["ticker"]) for c in candidates}
    missing = sorted(t for t in need if t not in prices or prices[t] is None)
    if missing:
        raise ValueError(
            f"missing prices for {missing} — cannot compute portfolio value V "
            "(the allocator refuses to size on incomplete market data; spec §9).")

    held_value = {t: held_shares[t] * int(prices[t]) for t in held_shares}
    V = sum(held_value.values()) + cash

    def diag(deployable):
        return {
            "V_krw": int(V), "cash_krw": int(cash), "budget_krw": budget_krw,
            "cash_floor_krw": int(math.ceil(cash_floor * V)),
            "deployable_cash_krw": int(deployable),
            "prices_used": {t: int(prices[t]) for t in sorted(need)},
        }

    if V <= 0:
        return _hold_result(cash, max(V, 1), budget_krw, held_value, allow_hold,
                            "portfolio value V ≤ 0 — nothing to size against.", diag(0))

    floor_krw = math.ceil(cash_floor * V)
    deployable = min(budget_krw, cash - floor_krw)

    # -- cash-is-a-position: deployable ≤ 0 (spec §12.6)
    if deployable <= 0:
        reason = (f"deployable cash ≤ 0: cash ₩{cash:,} is at/below the "
                  f"{cash_floor:.0%} floor (₩{floor_krw:,}) of portfolio value ₩{int(V):,}.")
        return _hold_result(cash, V, budget_krw, held_value, allow_hold, reason, diag(deployable))

    # -- tiers (spec §12.3): each candidate is deep_value / add_zone / (not in a buy zone)
    inzone, watch = [], []
    for c in candidates:
        t = str(c["ticker"])
        p = int(prices[t])
        B = float(c["buy_below_krw"])
        if p <= B * dv_discount:
            tier = "deep_value"
        elif p <= B:
            tier = "add_zone"
        else:
            watch.append({"ticker": t, "name": c.get("name"), "price": p, "buy_below": B})
            continue
        inzone.append({"ticker": t, "name": c.get("name"), "sector": c.get("sector"),
                       "price": p, "buy_below": B, "tier": tier,
                       "tier_weight": float(tier_w[tier])})

    d = diag(deployable)
    d["watch_above_buy_below"] = watch
    if not inzone:
        reason = "no candidate in a buy zone this run (every price is above its buy_below)."
        return _hold_result(cash, V, budget_krw, held_value, allow_hold, reason, d)

    # -- score = tier × (1 + rebalancing pull) (spec §12.4)
    for x in inzone:
        t = x["ticker"]
        hv = held_value.get(t, 0)
        w = hv / V
        headroom = _clamp((max_w_stock - w) / max_w_stock, 0.0, 1.0)
        excluded = held_tag.get(t) in exclude_tags        # only held names carry a tag
        pull = 0.0 if excluded else reb_pull * headroom
        x["w"] = w
        x["pull"] = pull
        x["pull_excluded"] = excluded
        x["score"] = x["tier_weight"] * (1.0 + pull)
        x["stock_cap_krw"] = max(0, int(round(max_w_stock * V)) - hv)

    # sector caps aggregate over ALL holdings in the sector, not just candidates (spec §12.5)
    sector_held_value: dict = {}
    for t, hv in held_value.items():
        s = held_sector.get(t)
        sector_held_value[s] = sector_held_value.get(s, 0) + hv
    sector_cap_krw: dict = {}
    for x in inzone:
        s = x["sector"]
        if s not in sector_cap_krw:
            sector_cap_krw[s] = max(0, int(round(max_w_sector * V)) - sector_held_value.get(s, 0))

    ordered = sorted(inzone, key=_order_key)

    # -- step 1: proportional target, water-filled against stock+sector caps (spec §12.5.1)
    alloc = {x["ticker"]: 0.0 for x in inzone}
    stock_remaining = {x["ticker"]: float(x["stock_cap_krw"]) for x in inzone}
    sector_remaining = dict(sector_cap_krw)
    pool = float(deployable)
    for _ in range(10_000):                                # safety bound; converges far sooner
        active = [x for x in ordered
                  if x["score"] > 0
                  and stock_remaining[x["ticker"]] > _EPS
                  and sector_remaining[x["sector"]] > _EPS]
        if pool <= _EPS or not active:
            break
        tot = sum(x["score"] for x in active)
        if tot <= 0:
            break
        distributed = 0.0
        for x in active:                                   # deterministic order
            t, s = x["ticker"], x["sector"]
            give = min(pool * x["score"] / tot, stock_remaining[t], sector_remaining[s])
            if give <= 0:
                continue
            alloc[t] += give
            stock_remaining[t] -= give
            sector_remaining[s] -= give
            distributed += give
        pool -= distributed
        if distributed <= _EPS:
            break

    # -- step 2: floor to whole shares (Korean 1-share lots; spec §12.5.2)
    shares = {x["ticker"]: int(alloc[x["ticker"]] // x["price"]) for x in inzone}
    stock_buy = {x["ticker"]: shares[x["ticker"]] * x["price"] for x in inzone}
    sector_buy: dict = {}
    for x in inzone:
        sector_buy[x["sector"]] = sector_buy.get(x["sector"], 0) + stock_buy[x["ticker"]]
    spent = sum(stock_buy.values())

    # -- step 3: greedy whole-share remainder within D and both caps (spec §12.5.3)
    remaining = deployable - spent
    while True:
        takeable = [x for x in ordered
                    if x["price"] <= remaining
                    and stock_buy[x["ticker"]] + x["price"] <= x["stock_cap_krw"]
                    and sector_buy[x["sector"]] + x["price"] <= sector_cap_krw[x["sector"]]]
        if not takeable:
            break
        best = takeable[0]                                 # 'ordered' already applies the tie-break
        t, s, p = best["ticker"], best["sector"], best["price"]
        shares[t] += 1
        stock_buy[t] += p
        sector_buy[s] += p
        spent += p
        remaining -= p

    # -- assemble orders + rationale (spec §3.4)
    orders, constraints_hit = [], []
    for x in ordered:
        t = x["ticker"]
        n = shares[t]
        if n <= 0:
            continue
        amt = n * x["price"]
        orders.append(Order(
            ticker=t, name=x["name"], sector=x["sector"], shares=n,
            ref_price_krw=x["price"], amount_krw=amt, tier=x["tier"],
            rationale=_rationale(x, amt, V, held_value.get(t, 0), sector_held_value,
                                 sector_buy, max_w_stock, max_w_sector)))

    # -- constraints_hit notes (which caps bound, rounding remainder, floor gap)
    for x in ordered:
        t, s = x["ticker"], x["sector"]
        if x["stock_cap_krw"] == 0:
            constraints_hit.append(
                f"{x['name'] or t} ({t}) already at/above {max_w_stock:.0%} stock cap — no buy.")
        elif stock_buy[t] + x["price"] > x["stock_cap_krw"] and shares[t] > 0:
            constraints_hit.append(
                f"{x['name'] or t} ({t}) capped at {max_w_stock:.0%} stock weight.")
    for s in sorted(sector_cap_krw):
        if sector_cap_krw[s] > 0 and any(shares[x["ticker"]] and x["sector"] == s for x in inzone) \
           and sector_buy[s] + min(x["price"] for x in inzone if x["sector"] == s) > sector_cap_krw[s]:
            constraints_hit.append(f"sector '{s}' capped at {max_w_sector:.0%} weight.")
    rounding_remainder = deployable - spent
    if orders and rounding_remainder > 0:
        constraints_hit.append(
            f"whole-share rounding reallocated ₩{rounding_remainder:,} to cash "
            "(no further whole share fit within budget/caps).")
    if budget_krw > deployable:
        constraints_hit.append(
            f"₩{budget_krw - deployable:,} of budget held back to respect the "
            f"{cash_floor:.0%} cash floor.")

    # -- post-buy weights (spec §3.4)
    final_value = dict(held_value)
    for x in inzone:
        t = x["ticker"]
        final_value[t] = final_value.get(t, 0) + stock_buy[t]
    post = {t: round(v / V, 4) for t, v in sorted(final_value.items()) if v > 0}
    post["cash"] = round((cash - spent) / V, 4)

    held_cash_reason = None
    if not orders:
        held_cash_reason = ("in-zone candidates could not be sized — stock/sector caps at "
                            "limit or share price exceeds deployable cash; holding.")

    d["deployed_krw"] = int(spent)
    return AllocationResult(
        orders=orders,
        post_buy_weights=post,
        leftover_cash_krw=budget_krw - spent,
        constraints_hit=constraints_hit,
        held_cash_reason=held_cash_reason,
        diagnostics=d,
    )


def _rationale(x, amt, V, held_val, sector_held_value, sector_buy,
               max_w_stock, max_w_sector) -> str:
    B, p = x["buy_below"], x["price"]
    disc = (B - p) / B if B else 0.0
    zone = f"{disc:.0%} below buy_below ₩{int(B):,}" if disc > 0 else f"at buy_below ₩{int(B):,}"
    post_w = (held_val + amt) / V
    post_sw = (sector_held_value.get(x["sector"], 0) + sector_buy[x["sector"]]) / V
    tail = " · pull excluded (tag in rebalance_pull_exclude_tags)" if x.get("pull_excluded") else ""
    return (f"{x['tier']}: {zone}; position {post_w:.0%} vs {max_w_stock:.0%} cap; "
            f"sector '{x['sector']}' {post_sw:.0%} vs {max_w_sector:.0%} cap{tail}")


def _hold_result(cash, V, budget_krw, held_value, allow_hold, reason, diag) -> AllocationResult:
    post = {t: round(hv / V, 4) for t, hv in sorted(held_value.items()) if hv > 0}
    post["cash"] = round(cash / V, 4)
    if not allow_hold:
        reason += (" (allow_hold_cash=false, but nothing is buyable within the rules, "
                   "so cash is held rather than forcing a deployment; spec §12.6).")
    return AllocationResult(orders=[], post_buy_weights=post, leftover_cash_krw=int(budget_krw),
                            constraints_hit=[], held_cash_reason=reason, diagnostics=diag)


# ============================================================ IO wrapper (§12.1, §12.7)


def _gate_3to4(verdict_dir: str, run_date: dt.date) -> tuple[list, list, list]:
    """Scan verdicts/ and split into (fed, dropped, expired) per spec §12.1.

    A verdict feeds the allocator iff:
      verdict == UNDERVALUED ∧ approved_by_user ∧ expires >= run_date ∧ buy_below_krw present.
    Nothing that fails is silently discarded — each carries a recorded reason (spec §9).
    """
    from screens.quant_filter import _read_yaml

    fed, dropped, expired = [], [], []
    for path in sorted(glob.glob(os.path.join(verdict_dir, "*.yaml"))):
        v = _read_yaml(path)
        t = str(v.get("ticker"))
        if v.get("verdict") != "UNDERVALUED":
            dropped.append({"ticker": t, "reason": f"verdict is {v.get('verdict')!r}, not UNDERVALUED"})
            continue
        if not v.get("approved_by_user"):
            dropped.append({"ticker": t, "reason": "approved_by_user is false (CP-2 not cleared)"})
            continue
        if v.get("buy_below_krw") in (None, ""):
            dropped.append({"ticker": t, "reason": "no buy_below_krw (required for an UNDERVALUED verdict)"})
            continue
        exp = v.get("expires")
        try:
            exp_date = dt.date.fromisoformat(str(exp))
        except (TypeError, ValueError):
            dropped.append({"ticker": t, "reason": f"unparseable expires {exp!r}"})
            continue
        if exp_date < run_date:
            expired.append({"ticker": t, "name": v.get("name"), "expires": str(exp),
                            "reason": "verdict expired — re-run /deepdive to reconsider"})
            continue
        fed.append(v)
    return fed, dropped, expired


def _resolve_sector(verdict: dict, portfolio: dict, run_dir: str, ref_date: str) -> tuple[str | None, str]:
    """Sector source order (spec §12.7): verdict file → held portfolio sector → sector_tagger."""
    if verdict.get("sector"):
        return str(verdict["sector"]), "verdict"
    t = str(verdict.get("ticker"))
    for p in portfolio.get("positions") or []:
        if str(p["ticker"]) == t and p.get("sector"):
            return str(p["sector"]), "portfolio"
    try:
        from screens import sector_tagger
        smap, _ = sector_tagger.load_or_build(run_dir, ref_date, [t])
        if smap.get(t):
            return str(smap[t]), "sector_tagger"
    except Exception as e:                                  # tagger needs network; degrade loudly-in-record
        return None, f"unresolved ({type(e).__name__})"
    return None, "unresolved"


def allocate(budget_krw: int, run_id: str | None = None, base_dir: str = ".") -> dict:
    """Full Stage 4 run: Gate 3→4 scan → price fetch → size() → write artifacts."""
    from collectors import krx_collector as k     # lazy: keeps size() import network-free
    from screens.quant_filter import _iso, _py, _read_yaml, _write_yaml, load_config

    _ascii_console()
    run_id = run_id or dt.date.today().strftime("%Y%m")
    cfg = load_config(base_dir)
    run_dir = os.path.join(base_dir, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    run_date = dt.date.today()

    # append-only report: refuse to overwrite a prior same-day report before doing any work
    report_path = os.path.join(base_dir, "outputs", f"report_{run_date:%Y%m%d}.md")
    if os.path.exists(report_path):
        raise FileExistsError(
            f"{report_path} already exists — reports are append-only history (CLAUDE.md); "
            "the allocator will not overwrite it. Remove/rename it to re-run today.")

    portfolio = _read_yaml(os.path.join(base_dir, "portfolio.yaml"))
    verdict_dir = os.path.join(base_dir, "verdicts")
    fed, dropped, expired = _gate_3to4(verdict_dir, run_date)

    price_date = k.nearest_business_day()
    candidates, sector_sources = [], {}
    for v in fed:
        sec, src = _resolve_sector(v, portfolio, run_dir, price_date)
        sector_sources[str(v["ticker"])] = src
        candidates.append({"ticker": str(v["ticker"]), "name": v.get("name"),
                           "sector": sec, "buy_below_krw": v.get("buy_below_krw")})

    # one price snapshot for every candidate + holding (market data → pykrx wins, §12.2)
    need = {str(p["ticker"]) for p in portfolio.get("positions") or []}
    need |= {c["ticker"] for c in candidates}
    prices = {t: k.get_close_price(t, price_date) for t in sorted(need)}

    result = size(candidates, portfolio, prices, budget_krw, cfg)

    doc = {
        "run_id": run_id,
        "generated": run_date.isoformat(),
        "budget_krw": int(budget_krw),
        "price_date": _iso(price_date),
        "orders": [dataclasses.asdict(o) for o in result.orders],
        "post_buy_weights": result.post_buy_weights,
        "leftover_cash_krw": result.leftover_cash_krw,
        "constraints_hit": result.constraints_hit,
        "held_cash_reason": result.held_cash_reason,
        "gate_3to4": {
            "fed": [{"ticker": c["ticker"], "name": c["name"], "sector": c["sector"],
                     "sector_source": sector_sources.get(c["ticker"])} for c in candidates],
            "dropped": dropped,
            "expired": expired,
        },
        "diagnostics": result.diagnostics,
        "disclaimer": "decision-support only; not investment advice; never auto-trades (CLAUDE.md).",
    }
    out_path = os.path.join(run_dir, "allocation.yaml")
    _write_yaml(_py(doc), out_path)
    _write_report(report_path, doc)
    print(f"allocation: {len(result.orders)} order(s), deployed "
          f"KRW {result.diagnostics.get('deployed_krw', 0):,} of KRW {int(budget_krw):,} "
          f"-> {out_path}", flush=True)
    if result.held_cash_reason:
        print("  holding cash this run — see the report / allocation.yaml for the reason.",
              flush=True)
    return doc


def _write_report(path: str, doc: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    L = []
    L.append(f"# 월간 배분 제안 — {doc['generated']} (run {doc['run_id']})\n")
    L.append(REPORT_DISCLAIMER + "\n")
    L.append(f"- 예산(budget): ₩{doc['budget_krw']:,}")
    L.append(f"- 가격 기준일(price_date): {doc['price_date']}  ·  source: pykrx")
    dg = doc["diagnostics"]
    L.append(f"- 포트폴리오 평가액 V: ₩{dg.get('V_krw', 0):,}  ·  "
             f"배분가능현금(deployable): ₩{dg.get('deployable_cash_krw', 0):,}")
    L.append("")

    g = doc["gate_3to4"]
    L.append("## Gate 3→4 감사 (verdicts)")
    if g["fed"]:
        L.append("**통과(fed):**")
        for f in g["fed"]:
            L.append(f"- {f.get('name') or f['ticker']} ({f['ticker']}) · sector "
                     f"{f.get('sector')} [{f.get('sector_source')}]")
    else:
        L.append("**통과(fed):** 없음 — 승인된 UNDERVALUED verdict이 없습니다.")
    if g["expired"]:
        L.append("\n**만료(expired — re-run /deepdive):**")
        for e in g["expired"]:
            L.append(f"- {e.get('name') or e['ticker']} ({e['ticker']}) · expired {e.get('expires')}")
    if g["dropped"]:
        L.append("\n**제외(dropped):**")
        for dp in g["dropped"]:
            L.append(f"- {dp['ticker']}: {dp['reason']}")
    L.append("")

    L.append("## 주문 제안 (orders)")
    if doc["orders"]:
        L.append("| 종목 | 티커 | 수량 | 기준가 | 금액 | tier | 근거 |")
        L.append("|---|---|---:|---:|---:|---|---|")
        for o in doc["orders"]:
            L.append(f"| {o['name'] or ''} | {o['ticker']} | {o['shares']} | "
                     f"₩{o['ref_price_krw']:,} | ₩{o['amount_krw']:,} | {o['tier']} | {o['rationale']} |")
    else:
        L.append(f"_주문 없음 — {doc.get('held_cash_reason') or '해당 없음'}_")
    L.append("")

    L.append("## 매수 후 비중 (post-buy weights)")
    for t, w in doc["post_buy_weights"].items():
        L.append(f"- {t}: {w:.1%}")
    L.append(f"\n- 잔여 현금(leftover, budget − 집행): ₩{doc['leftover_cash_krw']:,}")
    if doc["constraints_hit"]:
        L.append("\n## 제약 발생 (constraints_hit)")
        for c in doc["constraints_hit"]:
            L.append(f"- {c}")
    if doc.get("held_cash_reason"):
        L.append(f"\n## 현금 보유 사유\n- {doc['held_cash_reason']}")
    L.append("\n---\n*CP-3: 체결은 사용자가 직접 실행하고 `portfolio.yaml`·`decisions.log.yaml`에 기록합니다. "
             "이 배분기는 체결하지 않으며 `decisions.log.yaml`을 건드리지 않습니다.*\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"report -> {path}", flush=True)


# ============================================================ cli


def main(argv: list[str] | None = None):
    argv = argv if argv is not None else sys.argv[1:]
    usage = "usage: python -m allocate.allocator <budget_krw> [run_id]"
    if not argv:
        raise SystemExit(usage)
    try:
        budget = int(str(argv[0]).replace(",", "").replace("_", ""))
    except ValueError:
        raise SystemExit(f"budget must be an integer KRW amount.\n{usage}")
    run_id = argv[1] if len(argv) > 1 else None
    allocate(budget, run_id)


if __name__ == "__main__":
    main()
