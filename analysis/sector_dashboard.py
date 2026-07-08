"""sector_dashboard.py — Stage 1 input: code-generated sector valuation state.

Numbers flow from collectors into prompts — never out of the LLM (spec §1).
This module writes runs/<run_id>/sector_dashboard.yaml, the ONLY quantitative
source the Stage 1 prompt may cite when scoring Axis B (degree of
under-pricing). Per sector (union of KOSPI/KOSDAQ sector indices):

  - index-level PER/PBR now + own-5y monthly percentile + ~12M index return
  - cross-sectional member medians (PER/PBR) and 1y-return breadth

v1 scope (spec §10.3): own-history percentiles only — global peer multiples
stay qualitative until a data source is chosen.

Usage: python -m analysis.sector_dashboard <run_id>
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import time

import pandas as pd

from collectors import krx_collector as k
from screens import sector_tagger
from screens.quant_filter import _iso, _py, _write_yaml, build_universe, load_config

DASHBOARD_YAML = "sector_dashboard.yaml"


def _index_entry(market: str, code: str, hist: pd.DataFrame, min_obs: int) -> dict:
    """One sector index's valuation state from its monthly KRX history."""

    def stat(col: str) -> dict:
        series = hist[col][hist[col] > 0] if col in hist else pd.Series(dtype=float)
        cur = float(hist[col].iloc[-1]) if col in hist and len(hist) else 0.0
        pct = (round(float((series <= cur).mean()), 2)
               if cur > 0 and len(series) >= min_obs else None)
        return {"value": round(cur, 2) if cur > 0 else None,
                "own_5y_percentile": pct, "monthly_obs": int(len(series))}

    ret_1y = None
    if "종가" in hist and len(hist) >= 13 and float(hist["종가"].iloc[-13]) > 0:
        ret_1y = round(float(hist["종가"].iloc[-1] / hist["종가"].iloc[-13] - 1), 4)
    return {"market": market, "index_code": str(code),
            "per": stat("PER"), "pbr": stat("PBR"), "return_1y": ret_1y}


def build(run_id: str, base_dir: str = ".") -> str:
    t0 = time.monotonic()
    cfg = load_config(base_dir)
    run_dir = os.path.join(base_dir, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    out_path = os.path.join(run_dir, DASHBOARD_YAML)
    if os.path.exists(out_path):
        print(f"dashboard already exists -> {out_path} (delete the file to rebuild)")
        return out_path

    uni, uni_meta = build_universe(run_dir)
    ref_date = str(uni_meta["ref_date"])
    smap, _ = sector_tagger.load_or_build(run_dir, ref_date, uni.index)
    uni = uni.assign(sector=uni.index.map(smap))
    min_obs = int(cfg["data"]["min_history_months_for_percentile"])

    # per-index raw-history cache: 44 sequential ~20s KRX calls — a mid-run
    # failure must not force refetching the indices already pulled
    cache_dir = os.path.join(run_dir, "index_fund_cache")
    os.makedirs(cache_dir, exist_ok=True)

    sectors: dict[str, dict] = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        indices = k.list_sector_indices(ref_date, mkt)
        for i, (code, name) in enumerate(indices, 1):
            cpath = os.path.join(cache_dir, f"{code}.csv")
            if os.path.exists(cpath):
                hist = pd.read_csv(cpath, index_col=0, parse_dates=True, encoding="utf-8")
            else:
                hist = k.get_index_fundamental_history_monthly(code, years=5, end=ref_date)
                hist.to_csv(cpath, encoding="utf-8")
            sec = sectors.setdefault(name, {"name": name, "indices": []})
            sec["indices"].append(_index_entry(mkt, code, hist, min_obs))
            print(f"  {mkt} {i}/{len(indices)} {name}", flush=True)

    for name, sec in sectors.items():
        members = uni[uni["sector"] == name]
        per_pool = members.loc[members["PER"] > 0, "PER"]
        pbr_pool = members.loc[members["PBR"] > 0, "PBR"]
        rets = members["return_pct"].dropna()
        sec["members"] = {
            "count": int(len(members)),
            "median_per": round(float(per_pool.median()), 2) if len(per_pool) else None,
            "median_pbr": round(float(pbr_pool.median()), 2) if len(pbr_pool) else None,
            "breadth_pos_return_1y": round(float((rets > 0).mean()), 2) if len(rets) else None,
            "return_1y_unavailable": int(members["return_pct"].isna().sum()),
        }

    doc = {
        "run_id": run_id,
        "generated": dt.date.today().isoformat(),
        "data_basis": {
            "price_date": _iso(ref_date),
            "valuation_basis": ("index PER/PBR as published by KRX (last-FY earnings basis); "
                                "member medians are cross-sectional last-FY snapshots"),
            "percentile_window": "5y month-end history; null when < min obs (config) or value n/a",
            "index_return_basis": "~12 months, month-end close to month-end close",
            "member_return_window": [_iso(str(d)) for d in uni_meta["return_window"]],
            "sources": ["pykrx"],
        },
        "sectors": [sectors[n] for n in sorted(sectors)],
    }
    _write_yaml(_py(doc), out_path)
    print(f"\nsector_dashboard: {len(sectors)} sectors -> {out_path} "
          f"({(time.monotonic() - t0) / 60:.1f} min)", flush=True)
    return out_path


def main(argv: list[str] | None = None):
    argv = argv if argv is not None else sys.argv[1:]
    run_id = argv[0] if argv else dt.date.today().strftime("%Y%m")
    build(run_id)


if __name__ == "__main__":
    main()
