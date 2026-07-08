"""quant_filter.py — Stage 2 quantitative screen (Gate 2→3), fully code-enforced.

Consumes: config.yaml thresholds, an APPROVED runs/<run_id>/stage1_sectors.yaml
          (CP-1 — refuses to run otherwise), pykrx snapshots, DART financials.
Produces: runs/<run_id>/stage2_queue.yaml  (schema: implementation_spec.md §3.2)
          plus cached inputs under runs/<run_id>/ (universe.csv, liquidity, sector map).

Funnel — cheap checks first, expensive fetches last; the FIRST failing gate is
what a rejected name records:

  0. universe filters   market cap, avg daily value, exclusion rules   [counts only]
  1. sector scope       approved sectors via sector_tagger; unmapped surfaced
  2. cheapness          0 < PER <= max_per  AND  0 < PBR <= max_pbr
  3. rerating_guard     price leg: 1y return <= max_price_return_1y
  4. fundamentals       DART: ROE, positive OCF, debt ratio, revenue not in structural decline
  5. trap_preexclusion  >max consecutive FYs of simultaneous revenue+margin decline
  6. rerating_guard     percentile leg: own 5y PBR percentile <= max_own_5y_pbr_percentile
  7. sector_capacity    top candidates_per_sector by own 5y PBR percentile

Data-integrity rules honored here: every metric carries ref_date/basis tags;
DART wins for statement items, pykrx for market data (spec §9); a name whose
gate inputs are unverifiable is rejected with that reason — never passed on a
fabricated or missing figure.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import yaml

from collectors import krx_collector as k
from collectors.dart_collector import DartClient
from screens import sector_tagger

QUEUE_YAML = "stage2_queue.yaml"


# ---------------------------------------------------------------- io helpers


def _read_yaml(path: str):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_yaml(obj, path: str):
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, allow_unicode=True, sort_keys=False, width=120)


def _py(o):
    """Recursively convert numpy scalars / NaN so yaml.safe_dump accepts them."""
    if isinstance(o, dict):
        return {kk: _py(v) for kk, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_py(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        o = float(o)
    if isinstance(o, float) and math.isnan(o):
        return None
    return o


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


# ---------------------------------------------------------------- config & CP-1


def load_config(base_dir: str = ".") -> dict:
    path = os.path.join(base_dir, "config.yaml")
    if not os.path.exists(path):
        raise FileNotFoundError("config.yaml not found — create it from implementation_spec.md §4.")
    return _read_yaml(path)


def load_approved_sectors(run_dir: str, cfg: dict) -> list[str]:
    """CP-1 enforcement: refuse to run without user-approved Stage 1 output."""
    path = os.path.join(run_dir, "stage1_sectors.yaml")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found — Stage 1 must run (and be approved) before quant_filter (spec §2)."
        )
    doc = _read_yaml(path)
    if not doc.get("approved_by_user"):
        raise PermissionError(
            "CP-1: stage1_sectors.yaml has approved_by_user: false — "
            "quant_filter refuses to run against an unapproved sector file (spec §7)."
        )
    a_min = cfg["sectors"]["advance_min_axis_a"]
    b_min = cfg["sectors"]["advance_min_axis_b"]
    advancing = [s for s in doc.get("sectors", []) if s.get("verdict") == "ADVANCE"]
    below = [s["name"] for s in advancing
             if s.get("axis_a", 0) < a_min or s.get("axis_b", 0) < b_min]
    if below:
        raise ValueError(f"Gate 1→2 violation: ADVANCE sectors below axis minima: {below}.")
    if len(advancing) > cfg["sectors"]["max_advancing"]:
        raise ValueError(
            f"{len(advancing)} sectors advancing exceeds sectors.max_advancing="
            f"{cfg['sectors']['max_advancing']} — fix stage1_sectors.yaml, do not trim silently."
        )
    if not advancing:
        raise ValueError("No ADVANCE sectors in stage1_sectors.yaml — nothing to screen.")
    return [s["name"] for s in advancing]


# ---------------------------------------------------------------- cached inputs


def build_universe(run_dir: str) -> tuple[pd.DataFrame, dict]:
    """Universe snapshot (cap/volume + PER/PBR/EPS/BPS + names + 1y return), cached."""
    cache = os.path.join(run_dir, "universe.csv")
    meta_path = os.path.join(run_dir, "universe_meta.yaml")
    if os.path.exists(cache) and os.path.exists(meta_path):
        df = pd.read_csv(cache, dtype={"ticker": str}, encoding="utf-8").set_index("ticker")
        return df, _read_yaml(meta_path)

    date = k.nearest_business_day()
    print(f"  universe: snapshot for {date}...", flush=True)
    uni = k.get_universe(date)
    fund = k.get_fundamentals_snapshot(date)
    year_ago = k.nearest_business_day(
        (dt.datetime.strptime(date, "%Y%m%d") - dt.timedelta(days=365)).strftime("%Y%m%d")
    )
    change = pd.concat([k.get_price_change(year_ago, date, mkt) for mkt in ("KOSPI", "KOSDAQ")])
    df = (uni
          .join(fund[["PER", "PBR", "EPS", "BPS", "DIV"]], how="left")
          .join(change[["name", "close", "return_pct"]], how="left"))
    meta = {"ref_date": date, "return_window": [year_ago, date], "sources": ["pykrx"]}
    os.makedirs(run_dir, exist_ok=True)
    df.to_csv(cache, encoding="utf-8")
    _write_yaml(meta, meta_path)
    return df, meta


def build_liquidity(run_dir: str, cfg: dict, ref_date: str) -> pd.DataFrame:
    """Average daily trading value over the config window (trading days), cached.

    Heaviest pykrx pull of the screen: one whole-market snapshot per trading day.
    """
    cache = os.path.join(run_dir, "trading_value_avg.csv")
    if os.path.exists(cache):
        return pd.read_csv(cache, dtype={"ticker": str}, encoding="utf-8").set_index("ticker")

    window = int(cfg["universe"]["min_avg_daily_value_window_days"])
    start = (dt.datetime.strptime(ref_date, "%Y%m%d")
             - dt.timedelta(days=int(window * 2.2))).strftime("%Y%m%d")
    days = k.get_trading_days(start, ref_date)[-window:]
    cols = []
    for i, d in enumerate(days, 1):
        cols.append(k.get_ohlcv_snapshot(d)["trading_value"].rename(d))
        if i % 10 == 0 or i == len(days):
            print(f"  liquidity: {i}/{len(days)} daily snapshots", flush=True)
    tv = pd.concat(cols, axis=1)
    out = pd.DataFrame({
        "avg_trading_value": tv.mean(axis=1, skipna=True),
        "days_observed": tv.notna().sum(axis=1),
    })
    out.index.name = "ticker"
    out.to_csv(cache, encoding="utf-8")
    return out


# ---------------------------------------------------------------- gate logic


def fiscal_years(cfg: dict, ref_date: str) -> list[int]:
    """Most recent `annual_years` completed FYs (annual reports due end of March)."""
    y, m = int(ref_date[:4]), int(ref_date[4:6])
    latest = y - 1 if m >= 4 else y - 2
    return list(range(latest, latest - int(cfg["data"]["annual_years"]), -1))


def evaluate_dart_gates(hist: list[dict], cfg: dict) -> dict:
    """Fundamentals + trap gates from a DART financial history (newest-first).

    Returns {fundamentals: (ok, reason), trap: (ok, reason), metrics: {...}}.
    Unverifiable inputs fail the gate with that stated reason — never a pass on
    missing data.
    """
    scr = cfg["screening"]
    years = sorted(h["year"] for h in hist)                    # oldest → newest
    by_year = {h["year"]: h for h in hist}
    need = ("revenue", "operating_profit", "net_income", "ocf", "liabilities", "equity")

    missing = [
        f"FY{y}:{item}" for y in years
        for item in need
        if by_year[y]["basis"] is None or by_year[y]["items"].get(item) is None
    ]
    if missing:
        reason = f"insufficient DART data — missing {', '.join(missing[:6])}" + (
            f" (+{len(missing) - 6} more)" if len(missing) > 6 else "")
        return {"fundamentals": (False, reason), "trap": (False, reason), "metrics": None}

    bases = {by_year[y]["basis"] for y in years}
    if len(bases) > 1:
        reason = ("mixed statement basis across FYs "
                  f"({ {y: by_year[y]['basis'] for y in years} }) — never mixed silently (spec §9); "
                  "manual review required")
        return {"fundamentals": (False, reason), "trap": (False, reason), "metrics": None}
    basis = bases.pop()

    rev = [by_year[y]["items"]["revenue"] for y in years]
    op = [by_year[y]["items"]["operating_profit"] for y in years]
    ocf = [by_year[y]["items"]["ocf"] for y in years]
    liab = [by_year[y]["items"]["liabilities"] for y in years]
    eq = [by_year[y]["items"]["equity"] for y in years]
    ni = [by_year[y]["items"]["net_income"] for y in years]
    latest = years[-1]

    if eq[-1] <= 0:
        reason = f"negative equity in FY{latest}"
        return {"fundamentals": (False, reason), "trap": (False, reason), "metrics": None}
    if any(r <= 0 for r in rev):
        reason = "non-positive revenue in history — margins uncomputable"
        return {"fundamentals": (False, reason), "trap": (False, reason), "metrics": None}

    roe = ni[-1] / eq[-1]
    debt_ratio = liab[-1] / eq[-1]
    rev_growth = [rev[i] / rev[i - 1] - 1 for i in range(1, len(rev))]     # oldest → newest
    margins = [op[i] / rev[i] for i in range(len(rev))]
    margin_decline = [margins[i] < margins[i - 1] for i in range(1, len(margins))]

    fund_fails = []
    if roe < scr["min_roe"]:
        fund_fails.append(f"ROE {roe:.1%} < {scr['min_roe']:.0%} (FY{latest}, {basis})")
    if scr["require_positive_ocf"]:
        if ocf[-1] <= 0:
            fund_fails.append(f"OCF FY{latest} <= 0 ({ocf[-1] / 1e9:.0f}bn)")
        if sum(ocf[-3:]) <= 0:
            fund_fails.append("3y OCF sum <= 0")
    if debt_ratio > scr["max_net_debt_to_equity"]:
        fund_fails.append(
            f"liabilities/equity {debt_ratio:.2f} > {scr['max_net_debt_to_equity']} (FY{latest})")
    if all(g < 0 for g in rev_growth):
        fund_fails.append(
            f"revenue in structural decline ({len(rev_growth)} consecutive YoY declines)")

    simultaneous = [g < 0 and m for g, m in zip(rev_growth, margin_decline)]
    run = longest = 0
    for s in simultaneous:
        run = run + 1 if s else 0
        longest = max(longest, run)
    max_yrs = scr["trap_exclusion"]["max_consecutive_rev_and_margin_decline_yrs"]
    trap_ok = longest <= max_yrs
    trap_reason = (None if trap_ok else
                   f"{longest} consecutive FYs of simultaneous revenue+margin decline "
                   f"(> {max_yrs}; FY{years[0]}–FY{latest}, {basis})")

    metrics = {
        "roe": {"value": round(roe, 4), "ref_date": f"{latest}-FY", "basis": basis},
        "ocf_3y": {"values_krw_bn": [round(v / 1e9, 1) for v in ocf[-3:]],
                   "basis": basis, "source": "dart"},
        "net_debt_to_equity": {
            "value": round(debt_ratio, 3), "ref_date": f"{latest}-FY", "basis": basis,
            "proxy": "total-liabilities/equity (v1 — net-debt line items are a v1.1 upgrade, spec §10)",
        },
        "rev_trend_3y": [round(g, 4) for g in rev_growth[-3:]],
        "op_margin_trend_3y": [round(m, 4) for m in margins[-3:]],
    }
    return {
        "fundamentals": (not fund_fails, "; ".join(fund_fails) or None),
        "trap": (trap_ok, trap_reason),
        "metrics": metrics,
    }


def own_5y_percentiles(ticker: str, cur_per: float, cur_pbr: float, cfg: dict) -> dict:
    """Current PER/PBR percentile within own 5y monthly history (0 excluded = N/A)."""
    min_obs = int(cfg["data"]["min_history_months_for_percentile"])
    hist = k.get_fundamental_history_monthly(ticker, years=5)

    def pct(col: str, value: float):
        if col not in hist:
            return None, 0
        series = hist[col][hist[col] > 0]
        if len(series) < min_obs or not value or value <= 0:
            return None, len(series)
        return round(float((series <= value).mean()), 2), len(series)

    per_pct, per_n = pct("PER", cur_per)
    pbr_pct, pbr_n = pct("PBR", cur_pbr)
    return {"per": per_pct, "pbr": pbr_pct, "obs": {"per": per_n, "pbr": pbr_n}}


# ---------------------------------------------------------------- the screen


def screen(run_id: str, base_dir: str = ".") -> str:
    t0 = time.monotonic()
    cfg = load_config(base_dir)
    run_dir = os.path.join(base_dir, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)

    approved = load_approved_sectors(run_dir, cfg)
    print(f"approved sectors (CP-1 ok): {approved}", flush=True)

    uni, uni_meta = build_universe(run_dir)
    ref_date = str(uni_meta["ref_date"])
    liq = build_liquidity(run_dir, cfg, ref_date)
    smap, smeta = sector_tagger.load_or_build(run_dir, ref_date, uni.index)

    unknown = [s for s in approved if s not in set(smap.values())]
    if unknown:
        raise ValueError(f"Approved sectors not present in sector map: {unknown}. "
                         f"Available: {sorted(set(smap.values()))}")

    df = uni.join(liq, how="left")
    df["sector"] = df.index.map(smap)
    funnel = {"universe_total": len(df)}
    notes = [
        "admin_issue (관리종목/투자주의환기) exclusion is configured but NOT enforceable via "
        "pykrx in v1 — no designation feed; verify candidates manually until a source is wired.",
        "trading_halt proxied as zero volume on the snapshot date (no direct halt feed in pykrx).",
        "net_debt_to_equity is proxied by total-liabilities/equity in v1 (spec §10 deferral).",
        "trend arrays are ordered oldest → newest.",
    ]

    # -- 0. universe filters (counts only; per-name records start at sector scope)
    excl = cfg["universe"]["exclude"]
    m = pd.Series(True, index=df.index)
    m &= df["market_cap"] >= cfg["universe"]["min_market_cap_krw"]
    funnel["after_market_cap"] = int(m.sum())
    m &= df["avg_trading_value"].fillna(0) >= cfg["universe"]["min_avg_daily_value_krw"]
    funnel["after_liquidity"] = int(m.sum())
    name = df["name"].fillna("")
    if excl["preferred_shares"]:
        m &= pd.Series(df.index, index=df.index).str[-1] == "0"
    if excl["spac"]:
        m &= ~name.str.contains("스팩")
    if excl["reits"]:
        m &= ~name.str.contains("리츠")
    if excl["trading_halt"]:
        m &= df["volume"] > 0
    funnel["after_exclusions"] = int(m.sum())

    scoped = df[m & df["sector"].isin(approved)].copy()
    # names absent from the 1y price-change frame (recent listings): backfill so
    # spac/reit exclusion and the queue never carry a nameless ticker
    for t in scoped.index[scoped["name"].isna()]:
        try:
            scoped.loc[t, "name"] = df.loc[t, "name"] = k.get_name(t)
        except k.KRXDataError:
            pass  # stays NaN -> recorded as null, never fabricated
    backfilled_names = scoped["name"].fillna("")
    if excl["spac"]:
        scoped = scoped[~backfilled_names.str.contains("스팩")]
    if excl["reits"]:
        scoped = scoped[~backfilled_names.reindex(scoped.index).str.contains("리츠")]
    funnel["in_approved_sectors"] = len(scoped)
    print(f"universe {funnel['universe_total']} -> filtered {funnel['after_exclusions']} "
          f"-> in approved sectors {len(scoped)}", flush=True)

    # sector medians for context tags, computed on the filtered universe scope
    medians = {}
    for s in approved:
        pool = df[m & (df["sector"] == s)]
        medians[s] = {"per": float(pool.loc[pool["PER"] > 0, "PER"].median()),
                      "pbr": float(pool.loc[pool["PBR"] > 0, "PBR"].median())}

    rejected: list[dict] = []

    def reject(t: str, gate: str, reason: str):
        nm = df.at[t, "name"]
        rejected.append({"ticker": t, "name": None if pd.isna(nm) else str(nm),
                         "sector": str(df.at[t, "sector"]),
                         "failed_gate": gate, "reason": reason})

    # -- 2. cheapness  &  3. re-rating price leg (both from the snapshot, no fetches)
    scr = cfg["screening"]
    survivors = []
    for t, row in scoped.iterrows():
        per, pbr = float(row["PER"]), float(row["PBR"])
        if not (0 < per <= scr["max_per"]) or not (0 < pbr <= scr["max_pbr"]):
            reject(t, "cheapness",
                   f"PER {per or 'n/a'} / PBR {pbr or 'n/a'} vs caps {scr['max_per']}/{scr['max_pbr']} "
                   "(0 = negative earnings or n/a)")
            continue
        ret1y = row["return_pct"]
        if pd.isna(ret1y):
            reject(t, "rerating_guard", "1y price return unverifiable (absent from price-change frame)")
            continue
        if float(ret1y) / 100 > scr["rerating_guard"]["max_price_return_1y"]:
            reject(t, "rerating_guard",
                   f"1y return {float(ret1y):.0f}% > {scr['rerating_guard']['max_price_return_1y']:.0%}")
            continue
        survivors.append(t)
    funnel["after_cheapness_and_price_guard"] = len(survivors)
    print(f"cheapness + price guard -> {len(survivors)}", flush=True)

    # -- 4./5. DART fundamentals + trap pre-exclusion
    dart = DartClient()
    corp = dart.download_corp_codes()
    years = fiscal_years(cfg, ref_date)
    dart_pass: dict[str, dict] = {}
    for i, t in enumerate(survivors, 1):
        if t not in corp:
            reject(t, "fundamentals", "no DART corp_code mapping — financials unverifiable")
            continue
        res = evaluate_dart_gates(dart.get_financial_history(corp[t], years), cfg)
        ok_f, why_f = res["fundamentals"]
        ok_t, why_t = res["trap"]
        if not ok_f:
            reject(t, "fundamentals", why_f)
        elif not ok_t:
            reject(t, "trap_preexclusion", why_t)
        else:
            dart_pass[t] = res["metrics"]
        if i % 10 == 0 or i == len(survivors):
            print(f"  DART gates: {i}/{len(survivors)} (pass so far {len(dart_pass)})", flush=True)
    funnel["after_fundamentals_and_trap"] = len(dart_pass)

    # -- 6. re-rating percentile leg (heaviest per-ticker pykrx pull — done last)
    finalists: dict[str, dict] = {}
    max_pctile = scr["rerating_guard"]["max_own_5y_pbr_percentile"]
    for i, (t, metrics) in enumerate(dart_pass.items(), 1):
        row = scoped.loc[t]
        pcts = own_5y_percentiles(t, float(row["PER"]), float(row["PBR"]), cfg)
        if pcts["pbr"] is None:
            reject(t, "rerating_guard",
                   f"own 5y PBR percentile unverifiable ({pcts['obs']['pbr']} monthly obs < "
                   f"{cfg['data']['min_history_months_for_percentile']})")
            continue
        if pcts["pbr"] > max_pctile:
            reject(t, "rerating_guard",
                   f"PBR at own 5y {pcts['pbr']:.0%} percentile > {max_pctile:.0%}")
            continue
        finalists[t] = {**metrics, "_pcts": pcts}
        print(f"  5y percentile: {i}/{len(dart_pass)} {t} pbr_pct="
              f"{pcts['pbr']}", flush=True)
    funnel["after_percentile_guard"] = len(finalists)

    # -- 7. per-sector capacity: rank by own 5y PBR percentile (cheapest vs own history first)
    per_sector: dict[str, list[str]] = {}
    for t in finalists:
        per_sector.setdefault(str(df.at[t, "sector"]), []).append(t)
    candidates: list[dict] = []
    cap = scr["candidates_per_sector"]
    for s, ts in per_sector.items():
        ranked = sorted(ts, key=lambda t: (finalists[t]["_pcts"]["pbr"], float(df.at[t, "PBR"])))
        for t in ranked[cap:]:
            reject(t, "sector_capacity",
                   f"passed all gates but ranked below top {cap} in {s} "
                   f"(own 5y PBR pctile {finalists[t]['_pcts']['pbr']:.0%})")
        for t in ranked[:cap]:
            row = scoped.loc[t]
            med = medians[s]
            pcts = finalists[t].pop("_pcts")
            candidates.append({
                "ticker": t,
                "name": str(row["name"]),
                "sector": s,
                "metrics": {
                    "per": {"value": round(float(row["PER"]), 2), "ref_date": _iso(ref_date),
                            "basis": "last-FY",
                            "vs_sector_median_pct": round((float(row["PER"]) / med["per"] - 1) * 100),
                            "own_5y_percentile": pcts["per"]},
                    "pbr": {"value": round(float(row["PBR"]), 2), "ref_date": _iso(ref_date),
                            "basis": "last-FY",
                            "vs_sector_median_pct": round((float(row["PBR"]) / med["pbr"] - 1) * 100),
                            "own_5y_percentile": pcts["pbr"]},
                    **finalists[t],
                    "price_return_1y": round(float(row["return_pct"]) / 100, 4),
                },
                "gates": {"cheapness": "pass", "fundamentals": "pass",
                          "trap_preexclusion": "pass", "rerating_guard": "pass"},
                "llm_label": None,
                "llm_veto": None,
            })
    candidates.sort(key=lambda c: (c["sector"], c["metrics"]["pbr"]["own_5y_percentile"]))
    funnel["queued"] = len(candidates)

    unmapped = smeta.get("unmapped", [])
    queue = {
        "run_id": run_id,
        "generated": dt.date.today().isoformat(),
        "data_basis": {
            "statements": f"{cfg['data']['statement_basis']} (per-ticker basis on each metric)",
            "valuation_basis": "last-FY (Gate 0 C3: KRX EPS == DART FY-basic EPS on all samples)",
            "price_date": _iso(ref_date),
            "return_window": [_iso(str(d)) for d in uni_meta["return_window"]],
            "fiscal_years_used": sorted(years),
            "sources": ["pykrx", "dart"],
            "sector_medians": {s: {kk: round(v, 2) for kk, v in mv.items()}
                               for s, mv in medians.items()},
            "enforcement_notes": notes,
            "universe_funnel": funnel,
        },
        "approved_sectors": approved,
        "candidates": candidates,
        "rejected": rejected,
        "unmapped": {"count": len(unmapped),
                     "note": "not screened — surfaced per spec §9; user decides",
                     "tickers": unmapped},
    }
    out_path = os.path.join(run_dir, QUEUE_YAML)
    _write_yaml(_py(queue), out_path)
    print(f"\nstage2_queue: {len(candidates)} candidates, {len(rejected)} rejected "
          f"-> {out_path}  ({(time.monotonic() - t0) / 60:.1f} min)", flush=True)
    return out_path


def main(argv: list[str] | None = None):
    argv = argv if argv is not None else sys.argv[1:]
    run_id = argv[0] if argv else dt.date.today().strftime("%Y%m")
    screen(run_id)


if __name__ == "__main__":
    main()
