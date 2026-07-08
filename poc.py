"""poc.py — Gate 0: collectors proof-of-concept (implementation_spec.md §6).

Runs every acceptance criterion, records PASS / FAIL / SKIP / INFO per
criterion, and writes outputs/poc_report.md. The pipeline build proceeds past
collectors ONLY on a passing report (or documented, accepted workarounds).

Usage (PowerShell):
    $env:KRX_ID  = "..."   # free data.krx.co.kr account (pykrx >= 1.2 requires login)
    $env:KRX_PW  = "..."
    $env:DART_API_KEY = "..."
    python poc.py

Criteria numbering follows spec §6. C11 (typed defensive errors) is exercised
inside C6. C3 is informational but its determined basis MUST be copied into
the data-integrity tags used by quant_filter.
"""

from __future__ import annotations

import datetime as dt
import os
import time
import traceback
from dataclasses import dataclass, field

# --- sample tickers (edit freely): large KOSPI / mid KOSPI / KOSDAQ ----------
SAMPLES = {
    "005380": "현대차 (KOSPI large)",
    "000880": "한화 (KOSPI mid)",
    "058470": "리노공업 (KOSDAQ)",
}
FISCAL_YEARS = [2025, 2024, 2023, 2022]          # 4 FYs -> three YoY deltas
REPORT_PATH = os.path.join("outputs", "poc_report.md")

# Criteria whose PASS is required for the overall Gate 0 verdict:
REQUIRED = {"C1", "C2", "C4", "C5", "C6", "C7", "C8"}


@dataclass
class Result:
    cid: str
    name: str
    status: str = "FAIL"          # PASS | FAIL | SKIP | INFO
    details: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def note(self, msg: str):
        self.details.append(msg)


RESULTS: list[Result] = []


def run(cid: str, name: str, required_env: tuple[str, ...] = ()):
    """Decorator: time the criterion, catch everything, record the result."""

    def deco(fn):
        def inner(ctx: dict):
            res = Result(cid, name)
            missing = [e for e in required_env if not os.getenv(e)]
            if missing:
                res.status = "SKIP"
                res.note(f"env not set: {', '.join(missing)}")
                RESULTS.append(res)
                return
            t0 = time.monotonic()
            try:
                fn(ctx, res)
            except Exception as exc:  # noqa: BLE001 — harness boundary
                res.status = "FAIL"
                res.note(f"{type(exc).__name__}: {exc}")
                res.note("traceback (last line): " + traceback.format_exc().strip().splitlines()[-1])
            res.elapsed_s = time.monotonic() - t0
            RESULTS.append(res)

        return inner

    return deco


# ============================================================ P0 — preflight
def preflight(ctx: dict):
    res = Result("P0", "Preflight: credentials & environment", status="INFO")
    for var, why in (
        ("KRX_ID", "pykrx >= 1.2 requires a free data.krx.co.kr login"),
        ("KRX_PW", "pykrx >= 1.2 requires a free data.krx.co.kr login"),
        ("DART_API_KEY", "DART criteria (C3, C7–C10) run only with a key"),
    ):
        res.note(f"{var}: {'set' if os.getenv(var) else 'NOT SET'} — {why}")
    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        res.note("WARNING: without KRX login, ALL pykrx criteria will likely fail.")
    RESULTS.append(res)


# ============================================================ KRX criteria
@run("C1", "Universe: full KOSPI+KOSDAQ listing w/ market cap")
def c1(ctx, res):
    from collectors import krx_collector as k
    date = k.nearest_business_day()
    uni = k.get_universe(date)
    ctx["date"], ctx["universe"] = date, uni
    counts = uni["market"].value_counts().to_dict()
    res.note(f"ref_date={date}; counts={counts}; total={len(uni)}")
    ok = 600 <= counts.get("KOSPI", 0) <= 1200 and 1200 <= counts.get("KOSDAQ", 0) <= 2200
    res.status = "PASS" if ok else "FAIL"
    if not ok:
        res.note("counts outside loose sanity bounds — inspect before trusting universe.")


@run("C2", "Fundamentals snapshot: whole universe, one date")
def c2(ctx, res):
    from collectors import krx_collector as k
    date = ctx.get("date") or k.nearest_business_day()
    t0 = time.monotonic()
    fund = k.get_fundamentals_snapshot(date, market="ALL")
    wall = time.monotonic() - t0
    ctx["fund"] = fund
    res.note(f"rows={len(fund)}; cols={list(fund.columns)}; wall={wall:.1f}s")
    need = {"PER", "PBR", "EPS", "BPS"}
    have_cols = need.issubset(set(fund.columns))
    coverage_ok = "universe" not in ctx or len(fund) >= 0.9 * len(ctx["universe"])
    res.status = "PASS" if (have_cols and coverage_ok) else "FAIL"


@run("C3", "PER/EPS basis determination (last-FY vs TTM)", required_env=("DART_API_KEY",))
def c3(ctx, res):
    """Compare KRX-published EPS to DART basic EPS of the last two FYs."""
    from collectors.dart_collector import DartClient
    fund = ctx.get("fund")
    if fund is None:
        res.status = "SKIP"
        res.note("needs C2 fundamentals frame")
        return
    dart: DartClient = ctx.setdefault("dart", DartClient())
    corp = ctx.get("corp_map") or dart.download_corp_codes()
    ctx["corp_map"] = corp
    verdicts = []
    for t in SAMPLES:
        if t not in fund.index or t not in corp:
            verdicts.append(f"{t}: not comparable")
            continue
        krx_eps = float(fund.loc[t, "EPS"])
        hist = dart.get_financial_history(corp[t], FISCAL_YEARS[:2])
        for h in hist:
            d_eps = h["items"].get("eps_basic")
            if d_eps and krx_eps and abs(krx_eps - d_eps) / abs(d_eps) < 0.05:
                verdicts.append(f"{t}: KRX EPS {krx_eps:.0f} ≈ DART FY{h['year']} basic EPS {d_eps} → basis=FY{h['year']}")
                break
        else:
            verdicts.append(f"{t}: KRX EPS {krx_eps:.0f} matched no FY within 5% — investigate (TTM? adjusted?)")
    res.details += verdicts
    res.status = "INFO"
    res.note("ACTION: copy the determined basis into the `basis` tag quant_filter stamps on PER/PBR.")


@run("C4", "5-year monthly PER/PBR history for 3 samples")
def c4(ctx, res):
    from collectors import krx_collector as k
    ok = True
    for t, label in SAMPLES.items():
        df = k.get_fundamental_history_monthly(t, years=5)
        rows, nn = len(df), int(df["PBR"].notna().sum()) if "PBR" in df else 0
        res.note(f"{t} {label}: {rows} monthly rows, PBR non-null {nn}")
        ok &= rows >= 55 and nn >= 50
    res.status = "PASS" if ok else "FAIL"


@run("C5", "Sector mapping via KRX index constituents (coverage ≥ 95%)")
def c5(ctx, res):
    from collectors import krx_collector as k
    date = ctx.get("date") or k.nearest_business_day()
    uni = ctx.get("universe")
    total_map, sectors_all = {}, {}
    for mkt in ("KOSPI", "KOSDAQ"):
        mapping, used = k.get_sector_map(date, mkt)
        total_map.update(mapping)
        sectors_all[mkt] = used
        res.note(f"{mkt}: {len(used)} sector indices → {sorted(used)}")
    if uni is not None:
        raw_cov = sum(1 for t in uni.index if t in total_map) / len(uni)
        total_map, inherited = k.extend_map_with_preferred(total_map, uni.index)
        cov = sum(1 for t in uni.index if t in total_map) / len(uni)
        res.note(f"index-membership coverage: {raw_cov:.1%}; "
                 f"preferred shares inheriting common-share sector: +{inherited}")
        res.note(f"final coverage vs universe: {cov:.1%} (target ≥ 95%); unmapped go to explicit `unmapped` bucket")
        res.status = "PASS" if cov >= 0.95 else "FAIL"
    else:
        res.status = "FAIL"
        res.note("universe unavailable (C1 failed) — cannot measure coverage")
    ctx["sector_map"] = total_map
    res.note("EYEBALL the sector lists above: extend _NON_SECTOR_HINTS / _NON_SECTOR_EXACT if composites leaked through.")


@run("C6", "Failure modes: typed errors, invalid ticker, holiday fallback (folds in C11)")
def c6(ctx, res):
    from collectors import krx_collector as k
    checks = []
    # (a) invalid/delisted ticker must raise the TYPED error
    try:
        k.get_name("999999")
        checks.append(("invalid ticker -> typed error", False, "no error raised"))
    except k.KRXDataError as e:
        checks.append(("invalid ticker -> typed error", True, str(e)[:80]))
    except Exception as e:  # noqa: BLE001
        checks.append(("invalid ticker -> typed error", False, f"untyped {type(e).__name__}"))
    # (b) holiday / weekend fallback
    try:
        sunday = "20260628"  # a Sunday
        resolved = k.nearest_business_day(sunday)
        checks.append(("weekend -> prev business day", resolved != sunday, f"{sunday} -> {resolved}"))
    except Exception as e:  # noqa: BLE001
        checks.append(("weekend -> prev business day", False, f"{type(e).__name__}: {e}"))
    for name, ok, note in checks:
        res.note(f"{'OK ' if ok else 'FAIL'} {name}: {note}")
    res.note("trading-halt behavior: observational only — note anomalies during real runs.")
    res.status = "PASS" if all(ok for _, ok, _ in checks) else "FAIL"


# ============================================================ DART criteria
@run("C7", "DART key + corp_code mapping coverage", required_env=("DART_API_KEY",))
def c7(ctx, res):
    from collectors.dart_collector import DartClient
    dart: DartClient = ctx.setdefault("dart", DartClient())
    corp = ctx.get("corp_map") or dart.download_corp_codes()
    ctx["corp_map"] = corp
    res.note(f"listed companies mapped: {len(corp)}")
    missing = [t for t in SAMPLES if t not in corp]
    uni = ctx.get("universe")
    if uni is not None:
        cov = sum(1 for t in uni.index if t in corp) / len(uni)
        res.note(f"coverage vs universe: {cov:.1%}")
    res.status = "PASS" if not missing else "FAIL"
    if missing:
        res.note(f"samples missing from corp map: {missing}")


@run("C8", "4 FY annual consolidated financials for samples (key items)", required_env=("DART_API_KEY",))
def c8(ctx, res):
    from collectors.dart_collector import DartClient
    dart: DartClient = ctx.setdefault("dart", DartClient())
    corp = ctx["corp_map"]
    ctx["histories"] = {}
    ok_all = True
    for t, label in SAMPLES.items():
        hist = dart.get_financial_history(corp[t], FISCAL_YEARS)
        ctx["histories"][t] = hist
        good_years = sum(
            1 for h in hist
            if h["basis"] and all(h["items"][k] is not None
                                  for k in ("revenue", "operating_profit", "ocf", "liabilities", "equity"))
        )
        bases = {h["year"]: h["basis"] for h in hist}
        res.note(f"{t} {label}: complete key-item years = {good_years}/4; basis by year = {bases}")
        ok_all &= good_years >= 3
    res.status = "PASS" if ok_all else "FAIL"


@run("C9", "Standalone (OFS) fallback path", required_env=("DART_API_KEY",))
def c9(ctx, res):
    hists = ctx.get("histories", {})
    triggered = [
        (t, h["year"]) for t, hist in hists.items() for h in hist if h["basis"] == "standalone"
    ]
    if triggered:
        res.note(f"fallback exercised on: {triggered}")
    else:
        res.note("fallback path present in code but not triggered by samples "
                 "(all had consolidated statements) — acceptable; will trigger naturally on standalone-only names.")
    res.status = "PASS"


@run("C10", "DART latency / projected Stage-2 wall time", required_env=("DART_API_KEY",))
def c10(ctx, res):
    dart = ctx.get("dart")
    if dart is None:
        res.status = "SKIP"
        res.note("no DART requests were made")
        return
    s = dart.latency_stats()
    per_req = s["avg_s"] + 0.15  # + polite sleep
    projected = 50 * 4 * per_req  # 50 tickers × 4 FYs, CFS-hit assumption
    res.note(f"requests={s['n']}, avg={s['avg_s']:.2f}s, max={s['max_s']:.2f}s")
    res.note(f"projected Stage-2 pull (50 tickers × 4 FYs): ~{projected/60:.1f} min "
             "(double if OFS fallback rate is high)")
    res.note("record any HTTP 429 / quota messages here for the rate-limit ledger — do not assume limits.")
    res.status = "INFO"


# ============================================================ report
def write_report():
    os.makedirs("outputs", exist_ok=True)
    req_status = {r.cid: r.status for r in RESULTS}
    gate_pass = all(req_status.get(c) == "PASS" for c in sorted(REQUIRED))
    lines = [
        "# Gate 0 — Collectors POC Report",
        f"\nGenerated: {dt.datetime.now():%Y-%m-%d %H:%M} · spec: implementation_spec.md §6",
        f"\n## Verdict: **{'GATE 0 PASS — proceed to quant_filter' if gate_pass else 'GATE 0 FAIL — fix collectors before any downstream work'}**",
        f"\nRequired criteria: {', '.join(sorted(REQUIRED))} (C3/C10 informational, C9 conditional)\n",
        "| ID | Criterion | Status | Elapsed |",
        "|---|---|---|---|",
    ]
    for r in RESULTS:
        lines.append(f"| {r.cid} | {r.name} | {r.status} | {r.elapsed_s:.1f}s |")
    lines.append("\n## Details\n")
    for r in RESULTS:
        lines.append(f"### {r.cid} — {r.name} [{r.status}]")
        lines += [f"- {d}" for d in r.details] or ["- (no details)"]
        lines.append("")
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n{'='*60}\nReport written: {REPORT_PATH}")
    print(f"Gate 0: {'PASS' if gate_pass else 'FAIL'}")


def main():
    ctx: dict = {}
    preflight(ctx)
    for fn in (c1, c2, c3, c4, c5, c6, c7, c8, c9, c10):
        fn(ctx)
        last = RESULTS[-1]
        print(f"[{last.status:>4}] {last.cid} {last.name} ({last.elapsed_s:.1f}s)")
    write_report()


if __name__ == "__main__":
    main()
