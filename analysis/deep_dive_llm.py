"""deep_dive_llm.py — Stage 3: data-pack assembly + verdict writing.

Division of labor (spec §1): code assembles the per-ticker data pack; the LLM
runs prompts/valuation.md over it; the user approves the verdict (CP-2). This
module is the code side:

  pack     assemble runs/<run_id>/packs/<ticker>.yaml for a QUEUED candidate.
           Gate 2→3 is enforced here too: a ticker not in the queue's
           candidates — or vetoed at Stage 2 review — is refused. Nothing
           advances to Stage 3 outside the queue (spec §2).
  verdict  validate the LLM's verdict fields and write verdicts/<ticker>.yaml
           (schema §3.3) with TTL from config; approved_by_user starts false.

Usage:
  python -m analysis.deep_dive_llm pack <run_id> <ticker>
  python -m analysis.deep_dive_llm verdict <run_id> <ticker> <fields.yaml>
"""

from __future__ import annotations

import calendar
import datetime as dt
import os
import sys

import pandas as pd

from collectors import krx_collector as k
from collectors.dart_collector import DartClient
from screens.quant_filter import (QUEUE_YAML, _iso, _py, _read_yaml, _write_yaml,
                                  fiscal_years, load_config)

VERDICTS = ("UNDERVALUED", "WATCH", "TRAP")
TRAP_RISKS = ("low", "medium", "high")
_BN = 1e9


# ---------------------------------------------------------------- gate 2→3


def _require_queued(queue: dict, ticker: str) -> dict:
    """The Stage 3 entry gate: only un-vetoed queue candidates get a pack."""
    for c in queue.get("candidates", []):
        if str(c["ticker"]) == ticker:
            if c.get("llm_veto"):
                raise PermissionError(
                    f"{ticker} was vetoed at Stage 2 review ({c['llm_veto']}) — "
                    "the veto is final for this run (spec §8).")
            return c
    for r in queue.get("rejected", []):
        if str(r["ticker"]) == ticker:
            raise PermissionError(
                f"{ticker} was code-rejected at gate '{r['failed_gate']}' "
                f"({r['reason']}) — rejected names cannot be resurrected (spec §1).")
    raise KeyError(
        f"{ticker} is not in this run's stage2_queue — nothing advances to "
        "Stage 3 outside the queue (spec §2).")


# ---------------------------------------------------------------- pack pieces


def _series_summary(series: pd.Series) -> dict | None:
    s = series[series > 0].astype(float)
    if s.empty:
        return None
    q = s.quantile([0.1, 0.25, 0.5, 0.75, 0.9])
    return {"current": round(float(s.iloc[-1]), 2), "min": round(float(s.min()), 2),
            "p10": round(float(q[0.1]), 2), "p25": round(float(q[0.25]), 2),
            "median": round(float(q[0.5]), 2), "p75": round(float(q[0.75]), 2),
            "p90": round(float(q[0.9]), 2), "max": round(float(s.max()), 2),
            "months": int(len(s))}


def _recent_months(series: pd.Series, n: int = 12) -> dict:
    out = {}
    for idx, v in series.tail(n).items():
        out[str(idx)[:7]] = round(float(v), 2) if v and v > 0 else None
    return out


def _financials_4y(hist: list[dict]) -> list[dict]:
    rows = []
    for h in sorted(hist, key=lambda x: x["year"]):
        items = h["items"]

        def bn(key):
            v = items.get(key)
            return round(v / _BN, 1) if v is not None else None

        rows.append({"year": h["year"], "basis": h["basis"],
                     "revenue_krw_bn": bn("revenue"),
                     "operating_profit_krw_bn": bn("operating_profit"),
                     "net_income_krw_bn": bn("net_income"),
                     "ocf_krw_bn": bn("ocf"),
                     "liabilities_krw_bn": bn("liabilities"),
                     "equity_krw_bn": bn("equity"),
                     "eps_basic_krw": items.get("eps_basic")})
    return rows


def assemble_pack(run_id: str, ticker: str, base_dir: str = ".") -> str:
    cfg = load_config(base_dir)
    run_dir = os.path.join(base_dir, "runs", run_id)
    queue = _read_yaml(os.path.join(run_dir, QUEUE_YAML))
    cand = _require_queued(queue, ticker)
    print(f"gate 2→3 ok: {ticker} {cand['name']} ({cand['sector']})", flush=True)

    ref_date = k.nearest_business_day()
    dart = DartClient()
    corp = dart.download_corp_codes()
    if ticker not in corp:
        raise KeyError(f"{ticker} has no DART corp_code — pack cannot be assembled.")
    years = fiscal_years(cfg, ref_date)          # newest first

    print("  financials (DART)...", flush=True)
    fin = dart.get_financial_history(corp[ticker], years)

    print("  valuation history (pykrx, 5y monthly)...", flush=True)
    vh = k.get_fundamental_history_monthly(ticker, years=5, end=ref_date)

    print("  price context (pykrx)...", flush=True)
    end_dt = dt.datetime.strptime(ref_date, "%Y%m%d")
    d1y = (end_dt - dt.timedelta(days=365)).strftime("%Y%m%d")
    d5y = (end_dt - dt.timedelta(days=365 * 5 + 10)).strftime("%Y%m%d")
    daily_1y = k.get_ohlcv_history(ticker, d1y, ref_date, freq="d")
    monthly_5y = k.get_ohlcv_history(ticker, d5y, ref_date, freq="m")
    current_close = float(daily_1y["close"].iloc[-1])
    price_context = {
        "current_close_krw": current_close,
        "ref_date": _iso(ref_date),
        "w52_high_krw": float(daily_1y["high"].max()),
        "w52_low_krw": float(daily_1y["low"].min()),
        "band_5y_high_krw": float(monthly_5y["close"].max()),
        "band_5y_low_krw": float(monthly_5y["close"].min()),
        "band_5y_basis": "month-end closes (adjusted)",
        "pos_in_52w_range": round((current_close - float(daily_1y["low"].min()))
                                  / max(float(daily_1y["high"].max()) - float(daily_1y["low"].min()), 1e-9), 2),
    }

    print("  shareholders + disclosures (DART)...", flush=True)
    sh_year = years[0]
    holders = dart.get_major_shareholders(corp[ticker], sh_year)
    if not holders and len(years) > 1:
        sh_year = years[1]
        holders = dart.get_major_shareholders(corp[ticker], sh_year)
    disclosures = dart.get_recent_disclosures(corp[ticker], days=180)

    pack = {
        "ticker": ticker,
        "name": cand["name"],
        "sector": cand["sector"],
        "run_id": run_id,
        "generated": dt.date.today().isoformat(),
        "data_basis": {
            "price_date": _iso(ref_date),
            "statements": f"per-year basis tags below (config default {cfg['data']['statement_basis']})",
            "valuation_basis": "last-FY (Gate 0 C3)",
            "fiscal_years": sorted(years),
            "sources": ["pykrx", "dart"],
        },
        "stage2": {"metrics": cand["metrics"], "gates": cand["gates"],
                   "llm_label": cand.get("llm_label")},
        "financials_4y": _financials_4y(fin),
        "valuation_history_5y": {
            "per": _series_summary(vh["PER"]) if "PER" in vh else None,
            "pbr": _series_summary(vh["PBR"]) if "PBR" in vh else None,
            "recent_12m": {"per": _recent_months(vh["PER"]) if "PER" in vh else None,
                           "pbr": _recent_months(vh["PBR"]) if "PBR" in vh else None},
        },
        "price_context": price_context,
        "shareholders": {
            "fiscal_year": sh_year if holders else None,
            "source": "DART hyslrSttus (최대주주 및 특수관계인, annual report)",
            "holders": holders or None,
            "note": None if holders else "no filing retrievable — treat structure as [unverified]",
        },
        "recent_disclosures": {
            "window_days": 180,
            "count": len(disclosures),
            "items": disclosures[:40],
            "truncated": len(disclosures) > 40,
        },
    }
    packs_dir = os.path.join(run_dir, "packs")
    os.makedirs(packs_dir, exist_ok=True)
    out_path = os.path.join(packs_dir, f"{ticker}.yaml")
    _write_yaml(_py(pack), out_path)
    print(f"pack -> {out_path}", flush=True)
    return out_path


# ---------------------------------------------------------------- verdicts


def _add_months(d: dt.date, months: int) -> dt.date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def write_verdict(run_id: str, ticker: str, fields: dict, base_dir: str = ".") -> str:
    """Validate LLM verdict fields (prompts/valuation.md §11) and persist per §3.3."""
    cfg = load_config(base_dir)
    pack_path = os.path.join(base_dir, "runs", run_id, "packs", f"{ticker}.yaml")
    if not os.path.exists(pack_path):
        raise FileNotFoundError(f"{pack_path} missing — assemble the pack before writing a verdict.")
    pack = _read_yaml(pack_path)

    verdict = fields.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}.")
    conviction = fields.get("conviction")
    if not isinstance(conviction, int) or not 1 <= conviction <= 5:
        raise ValueError(f"conviction must be int 1-5, got {conviction!r}.")
    trap_risk = fields.get("trap_risk")
    if trap_risk not in TRAP_RISKS:
        raise ValueError(f"trap_risk must be one of {TRAP_RISKS}, got {trap_risk!r}.")
    if verdict == "UNDERVALUED" and trap_risk == "high":
        raise ValueError("UNDERVALUED with trap_risk high is contradictory (prompt §11) — resolve first.")
    band = fields.get("intrinsic_band_krw")
    if not (isinstance(band, (list, tuple)) and len(band) == 2
            and all(isinstance(x, (int, float)) for x in band) and band[0] <= band[1]):
        raise ValueError(f"intrinsic_band_krw must be [low, high] numbers, got {band!r}.")
    buy_below = fields.get("buy_below_krw")
    if verdict == "UNDERVALUED":
        if not isinstance(buy_below, (int, float)) or buy_below <= 0:
            raise ValueError("buy_below_krw is required (and > 0) for an UNDERVALUED verdict.")
    if not str(fields.get("thesis_1line") or "").strip():
        raise ValueError("thesis_1line is required.")

    today = dt.date.today()
    doc = {
        "ticker": ticker,
        "name": pack["name"],
        "sector": pack.get("sector"),   # persisted for the Stage 4 sector cap (spec §12.7)
        "run_id": run_id,
        "generated": today.isoformat(),
        "verdict": verdict,
        "conviction": conviction,
        "trap_risk": trap_risk,
        "intrinsic_band_krw": [float(band[0]), float(band[1])],
        "buy_below_krw": float(buy_below) if isinstance(buy_below, (int, float)) else None,
        "thesis_1line": str(fields["thesis_1line"]).strip(),
        "governance_flags": list(fields.get("governance_flags") or []),
        "data_basis": {
            "statements": pack["data_basis"]["statements"],
            "valuation": str(fields.get("valuation_basis") or pack["data_basis"]["valuation_basis"]),
            "pack": f"runs/{run_id}/packs/{ticker}.yaml",
        },
        "expires": _add_months(today, int(cfg["review"]["verdict_ttl_months"])).isoformat(),
        "approved_by_user": False,   # CP-2: only the user flips this
        "report": f"outputs/deepdive_{ticker}_{today:%Y%m%d}.md",
    }
    verdict_dir = os.path.join(base_dir, "verdicts")
    os.makedirs(verdict_dir, exist_ok=True)
    out_path = os.path.join(verdict_dir, f"{ticker}.yaml")
    _write_yaml(_py(doc), out_path)
    print(f"verdict ({verdict}, expires {doc['expires']}) -> {out_path}", flush=True)
    return out_path


# ---------------------------------------------------------------- cli


def main(argv: list[str] | None = None):
    argv = argv if argv is not None else sys.argv[1:]
    usage = ("usage: python -m analysis.deep_dive_llm pack <run_id> <ticker>\n"
             "       python -m analysis.deep_dive_llm verdict <run_id> <ticker> <fields.yaml>")
    if len(argv) == 3 and argv[0] == "pack":
        assemble_pack(argv[1], argv[2])
    elif len(argv) == 4 and argv[0] == "verdict":
        fields = _read_yaml(argv[3])
        fields = fields.get("verdict_fields", fields)   # accept prompt output or bare fields
        write_verdict(argv[1], argv[2], fields)
    else:
        raise SystemExit(usage)


if __name__ == "__main__":
    main()
