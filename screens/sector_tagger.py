"""sector_tagger.py — stock → sector mapping with a per-run cache.

KRX serves each sector index's constituent list in ~15-20s (44 indices ≈ 15
minutes per full pull, and the endpoint is flaky), so the map is fetched once
per run_id and cached under runs/<run_id>/ for quant_filter and the sector
dashboard to share. Preferred shares inherit their common share's sector via
the collector helper; everything left over is the explicit `unmapped` bucket
(spec §9: surfaced, never silently dropped).
"""

from __future__ import annotations

import os

import pandas as pd
import yaml

from collectors import krx_collector as k

MAP_CSV = "sector_map.csv"
META_YAML = "sector_map_meta.yaml"


def load_or_build(run_dir: str, date: str, universe_tickers) -> tuple[dict[str, str], dict]:
    """Return ({ticker: sector}, meta). Cached; rebuilds only if cache missing."""
    map_path = os.path.join(run_dir, MAP_CSV)
    meta_path = os.path.join(run_dir, META_YAML)
    if os.path.exists(map_path) and os.path.exists(meta_path):
        df = pd.read_csv(map_path, dtype={"ticker": str}, encoding="utf-8")
        with open(meta_path, encoding="utf-8") as f:
            meta = yaml.safe_load(f)
        return dict(zip(df["ticker"], df["sector"])), meta

    universe_tickers = list(universe_tickers)
    mapping: dict[str, str] = {}
    indices_used: dict[str, list[str]] = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        print(f"  sector map: fetching {mkt} index constituents...", flush=True)
        mkt_map, used = k.get_sector_map(date, mkt)
        mapping.update(mkt_map)
        indices_used[mkt] = used
    raw_cov = sum(1 for t in universe_tickers if t in mapping) / len(universe_tickers)
    mapping, inherited = k.extend_map_with_preferred(mapping, universe_tickers)
    unmapped = [t for t in universe_tickers if t not in mapping]
    coverage = 1 - len(unmapped) / len(universe_tickers)

    meta = {
        "ref_date": date,
        "method": ("KRX sector-index constituents; smallest index wins on multi-membership; "
                   "preferred shares inherit common-share sector"),
        "indices_used": indices_used,
        "coverage_index_membership": round(raw_cov, 4),
        "preferred_inherited": inherited,
        "coverage_final": round(coverage, 4),
        "unmapped": unmapped,
    }
    os.makedirs(run_dir, exist_ok=True)
    pd.DataFrame(sorted(mapping.items()), columns=["ticker", "sector"]).to_csv(
        map_path, index=False, encoding="utf-8"
    )
    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, allow_unicode=True, sort_keys=False)
    print(f"  sector map: coverage {coverage:.1%} ({len(unmapped)} unmapped) -> {map_path}", flush=True)
    return mapping, meta
