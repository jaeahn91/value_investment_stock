"""dart_collector.py — DART OpenAPI client (Gate 0 scope).

- API key from env var DART_API_KEY only. Never hardcode, never commit.
- Consolidated (CFS) first; fall back to standalone (OFS) per (ticker, year)
  with the basis tag flipped — never mixed silently.
- Every request's latency is recorded (Gate 0 criterion 10).
- Amounts returned in KRW as int; missing values are None, never fabricated.

Endpoints used:
  corpCode.xml          — ticker <-> corp_code master (zip of xml)
  fnlttSinglAcntAll     — full single-company financial statements
Reference: https://opendart.fss.or.kr (rate limits observed empirically, not assumed).
"""

from __future__ import annotations

import io
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

_BASE = "https://opendart.fss.or.kr/api"
_ANNUAL_REPORT = "11011"  # 사업보고서
_POLITE_SLEEP_S = 0.15
_TIMEOUT_S = 20


class DartError(RuntimeError):
    """Typed error for any DART access/parsing failure."""


def _norm(s: str) -> str:
    return (s or "").replace(" ", "")


def _amount(raw: str | None) -> int | None:
    raw = (raw or "").replace(",", "").strip()
    if raw in ("", "-"):
        return None
    try:
        return int(raw)
    except ValueError:
        try:
            return int(float(raw))  # EPS rows can carry decimals
        except ValueError:
            return None


#: key financial items: matched by IFRS account_id first, normalized name second.
KEY_ITEMS: dict[str, dict] = {
    "revenue": {
        "ids": {"ifrs-full_Revenue"},
        "names": {"매출액", "영업수익", "수익(매출액)", "매출"},
        "sj": {"IS", "CIS"},
    },
    "operating_profit": {
        "ids": {"dart_OperatingIncomeLoss"},
        "names": {"영업이익", "영업이익(손실)"},
        "sj": {"IS", "CIS"},
    },
    "net_income": {
        "ids": {"ifrs-full_ProfitLoss"},
        "names": {"당기순이익", "당기순이익(손실)"},
        "sj": {"IS", "CIS"},
    },
    "ocf": {
        "ids": {"ifrs-full_CashFlowsFromUsedInOperatingActivities"},
        "names": {"영업활동현금흐름", "영업활동으로인한현금흐름"},
        "sj": {"CF"},
    },
    "liabilities": {
        "ids": {"ifrs-full_Liabilities"},
        "names": {"부채총계"},
        "sj": {"BS"},
    },
    "equity": {
        "ids": {"ifrs-full_Equity"},
        "names": {"자본총계"},
        "sj": {"BS"},
    },
    "eps_basic": {
        "ids": {"ifrs-full_BasicEarningsLossPerShare"},
        "names": {"기본주당이익", "기본주당순이익", "기본주당이익(손실)"},
        "sj": {"IS", "CIS"},
    },
}


@dataclass
class DartClient:
    api_key: str | None = None
    latencies_s: list[float] = field(default_factory=list)

    def __post_init__(self):
        self.api_key = self.api_key or os.getenv("DART_API_KEY")
        if not self.api_key:
            raise DartError(
                "DART_API_KEY not set. Get a free key at opendart.fss.or.kr "
                "and export it as an environment variable."
            )
        self._sess = requests.Session()

    # ------------------------------------------------------------ transport

    def _get(self, endpoint: str, *, raw: bool = False, **params):
        params["crtfc_key"] = self.api_key
        t0 = time.monotonic()
        try:
            r = self._sess.get(f"{_BASE}/{endpoint}", params=params, timeout=_TIMEOUT_S)
        except requests.RequestException as exc:
            raise DartError(f"DART network failure on {endpoint}: {exc}") from exc
        finally:
            self.latencies_s.append(time.monotonic() - t0)
        time.sleep(_POLITE_SLEEP_S)
        if r.status_code != 200:
            raise DartError(f"DART HTTP {r.status_code} on {endpoint}.")
        if raw:
            return r.content
        payload = r.json()
        return payload

    # ------------------------------------------------------------ corp codes

    def download_corp_codes(self) -> dict[str, str]:
        """Return {6-digit stock ticker -> corp_code} for all listed companies."""
        blob = self._get("corpCode.xml", raw=True)
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                xml_bytes = zf.read(zf.namelist()[0])
        except zipfile.BadZipFile as exc:
            # DART returns a JSON/XML error body (e.g. bad key) instead of a zip
            raise DartError(
                f"corpCode.xml was not a zip — likely an API-key error. "
                f"Body head: {blob[:200]!r}"
            ) from exc
        root = ET.fromstring(xml_bytes)
        mapping: dict[str, str] = {}
        for node in root.iter("list"):
            stock_code = (node.findtext("stock_code") or "").strip()
            corp_code = (node.findtext("corp_code") or "").strip()
            if len(stock_code) == 6 and corp_code:
                mapping[stock_code] = corp_code
        if not mapping:
            raise DartError("corpCode.xml parsed but yielded no listed companies.")
        return mapping

    # ------------------------------------------------------------ financials

    def get_annual_statements(self, corp_code: str, year: int, fs_div: str) -> tuple[list[dict], str]:
        """One fiscal year, one basis. Returns (rows, status).

        status: '000' ok, '013' no data (drives the CFS->OFS fallback).
        """
        payload = self._get(
            "fnlttSinglAcntAll.json",
            corp_code=corp_code,
            bsns_year=str(year),
            reprt_code=_ANNUAL_REPORT,
            fs_div=fs_div,
        )
        status = payload.get("status", "")
        if status == "000":
            return payload.get("list", []), status
        if status == "013":
            return [], status
        raise DartError(
            f"DART error status={status} msg={payload.get('message')!r} "
            f"(corp={corp_code}, year={year}, fs_div={fs_div})."
        )

    @staticmethod
    def extract_key_items(rows: list[dict]) -> dict[str, int | None]:
        out: dict[str, int | None] = {k: None for k in KEY_ITEMS}
        for row in rows:
            acc_id = (row.get("account_id") or "").strip()
            acc_nm = _norm(row.get("account_nm", ""))
            sj = (row.get("sj_div") or "").strip()
            for key, spec in KEY_ITEMS.items():
                if out[key] is not None:
                    continue
                if sj and spec["sj"] and sj not in spec["sj"]:
                    continue
                if acc_id in spec["ids"] or acc_nm in spec["names"]:
                    out[key] = _amount(row.get("thstrm_amount"))
        return out

    def get_financial_history(self, corp_code: str, years: list[int]) -> list[dict]:
        """Annual key items for `years`, CFS-first with per-year OFS fallback.

        Each entry: {year, basis: 'consolidated'|'standalone'|None,
                     items: {...}, status: '000'|'013'}.
        basis None + status '013' means the year is simply unavailable.
        """
        history = []
        for y in years:
            rows, status = self.get_annual_statements(corp_code, y, "CFS")
            basis = "consolidated"
            if status == "013":
                rows, status = self.get_annual_statements(corp_code, y, "OFS")
                basis = "standalone" if status == "000" else None
            history.append(
                {
                    "year": y,
                    "basis": basis if status == "000" else None,
                    "status": status,
                    "items": self.extract_key_items(rows) if rows else {k: None for k in KEY_ITEMS},
                }
            )
        return history

    # ------------------------------------------------------------ telemetry

    def latency_stats(self) -> dict[str, float]:
        if not self.latencies_s:
            return {"n": 0, "avg_s": 0.0, "max_s": 0.0}
        return {
            "n": len(self.latencies_s),
            "avg_s": sum(self.latencies_s) / len(self.latencies_s),
            "max_s": max(self.latencies_s),
        }
