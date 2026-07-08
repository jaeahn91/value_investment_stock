"""krx_collector.py — defensive wrappers around pykrx (Gate 0 scope).

Design rules (CLAUDE.md):
- pykrx scrapes data.krx.co.kr and WILL break on site changes: every call is
  wrapped; failures raise KRXDataError with a human-readable message. Never
  fail silently, never return a partially-fabricated frame.
- pykrx >= 1.2.x requires a free KRX data-portal login. Set env vars
  KRX_ID / KRX_PW before importing this module (pykrx logs in at import time).

Written against pykrx 1.2.8 (verified signatures).
"""

from __future__ import annotations

import datetime as _dt
import functools
import os
import time as _time
import warnings

import pandas as pd

warnings.filterwarnings("ignore")

from pykrx import stock  # noqa: E402  (import after warnings filter)


class KRXDataError(RuntimeError):
    """Typed error for any KRX/pykrx data-access failure."""


def krx_credentials_present() -> bool:
    return bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))


def _defensive(what: str):
    """Wrap a pykrx-touching function: typed error + context on any failure."""

    def deco(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except KRXDataError:
                raise
            except Exception as exc:  # noqa: BLE001 — boundary wrapper
                raise KRXDataError(
                    f"KRX access failed while {what} "
                    f"(args={args}, kwargs={kwargs}): {type(exc).__name__}: {exc}. "
                    "Likely causes: KRX site change broke pykrx, missing KRX_ID/KRX_PW "
                    "login, network egress blocked, or non-trading date."
                ) from exc

        return inner

    return deco


# ---------------------------------------------------------------- dates


@_defensive("resolving nearest business day")
def nearest_business_day(date: str | None = None) -> str:
    """YYYYMMDD of the nearest previous trading day (handles weekends/holidays)."""
    if date is None:
        date = _dt.date.today().strftime("%Y%m%d")
    return stock.get_nearest_business_day_in_a_week(date, prev=True)


# ---------------------------------------------------------------- universe


@_defensive("fetching the ticker/market-cap universe")
def get_universe(date: str, markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")) -> pd.DataFrame:
    """All listings on `date` with market cap and trading value.

    Returns DataFrame indexed by ticker:
        market, market_cap, volume, trading_value, shares_listed
    Data-integrity: ref_date == `date`, source == pykrx/KRX.
    """
    frames = []
    for mkt in markets:
        df = stock.get_market_cap(date, market=mkt)
        if df is None or df.empty:
            raise KRXDataError(f"Empty market-cap frame for {mkt} on {date}.")
        df = df.rename(
            columns={
                "시가총액": "market_cap",
                "거래량": "volume",
                "거래대금": "trading_value",
                "상장주식수": "shares_listed",
            }
        )
        df["market"] = mkt
        frames.append(df)
    out = pd.concat(frames)
    out.index.name = "ticker"
    out.attrs["ref_date"] = date
    return out


@_defensive("fetching ticker name")
def get_name(ticker: str) -> str:
    name = stock.get_market_ticker_name(ticker)
    # pykrx returns an empty frame/str for unknown tickers instead of raising.
    if not isinstance(name, str) or not name.strip():
        raise KRXDataError(f"Unknown or delisted ticker: {ticker!r}")
    return name


# ---------------------------------------------------------------- fundamentals


@_defensive("fetching whole-market fundamentals snapshot")
def get_fundamentals_snapshot(date: str, market: str = "ALL") -> pd.DataFrame:
    """BPS/PER/PBR/EPS/DIV/DPS for every listing on `date`.

    NOTE (Gate 0 criterion 3): the earnings basis of KRX-published PER/EPS
    (last-FY vs TTM) must be determined empirically — see poc.py C3.
    """
    df = stock.get_market_fundamental(date, market=market)
    if df is None or df.empty:
        raise KRXDataError(f"Empty fundamentals frame for {market} on {date}.")
    df.index.name = "ticker"
    df.attrs["ref_date"] = date
    df.attrs["basis"] = "krx-published (basis TBD — see poc C3)"
    return df


@_defensive("fetching monthly fundamental history")
def get_fundamental_history_monthly(ticker: str, years: int = 5, end: str | None = None) -> pd.DataFrame:
    """Monthly PER/PBR/EPS/BPS history for one ticker over `years` years."""
    end = end or nearest_business_day()
    end_dt = _dt.datetime.strptime(end, "%Y%m%d")
    start = (end_dt - _dt.timedelta(days=365 * years + 10)).strftime("%Y%m%d")
    df = stock.get_market_fundamental(start, end, ticker, freq="m")
    if df is None or df.empty:
        raise KRXDataError(f"Empty fundamental history for {ticker} ({start}–{end}).")
    df.attrs.update({"ticker": ticker, "from": start, "to": end, "freq": "m"})
    return df


@_defensive("fetching OHLCV")
def get_close_price(ticker: str, date: str) -> int:
    df = stock.get_market_ohlcv(date, date, ticker)
    if df is None or df.empty:
        raise KRXDataError(f"No OHLCV for {ticker} on {date}.")
    return int(df["종가"].iloc[-1])


@_defensive("listing trading days")
def get_trading_days(fromdate: str, todate: str) -> list[str]:
    """Trading days in [fromdate, todate] as YYYYMMDD (via KOSPI composite index)."""
    df = stock.get_index_ohlcv(fromdate, todate, "1001")
    if df is None or df.empty:
        raise KRXDataError(f"No trading days resolved in {fromdate}–{todate}.")
    return [d.strftime("%Y%m%d") for d in df.index]


@_defensive("fetching whole-market OHLCV snapshot")
def get_ohlcv_snapshot(date: str, markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")) -> pd.DataFrame:
    """Per-ticker OHLCV + trading value on one date (liquidity screen input)."""
    frames = []
    for mkt in markets:
        df = stock.get_market_ohlcv(date, market=mkt)
        if df is None or df.empty:
            raise KRXDataError(f"Empty OHLCV snapshot for {mkt} on {date}.")
        frames.append(df)
    out = pd.concat(frames).rename(
        columns={"시가": "open", "고가": "high", "저가": "low", "종가": "close",
                 "거래량": "volume", "거래대금": "trading_value", "등락률": "change_pct"}
    )
    out.index.name = "ticker"
    out.attrs["ref_date"] = date
    return out


@_defensive("fetching market-wide price change")
def get_price_change(fromdate: str, todate: str, market: str) -> pd.DataFrame:
    """Per-ticker price change over a window, with names (re-rating guard input).

    Names listed mid-window appear with partial-window returns; names delisted
    before `todate` are absent — callers must treat missing rows as unverifiable.
    """
    df = stock.get_market_price_change(fromdate, todate, market=market)
    if df is None or df.empty:
        raise KRXDataError(f"Empty price-change frame for {market} {fromdate}–{todate}.")
    df = df.rename(
        columns={"종목명": "name", "시가": "open", "종가": "close", "변동폭": "change",
                 "등락률": "return_pct", "거래량": "volume", "거래대금": "trading_value"}
    )
    df.index.name = "ticker"
    df.attrs.update({"from": fromdate, "to": todate})
    return df


# ---------------------------------------------------------------- sectors

#: Substrings identifying non-sector (composite/strategy/size) indices to skip.
_NON_SECTOR_HINTS = (
    "200", "150", "100", "50 ", "코스피",  # 코스피/코스피 200/대형주 family
    "코스닥", "KRX", "배당", "우량", "글로벌", "리더", "150",
    "대형주", "중형주", "소형주", "TOP", "혁신", "프리미어", "테마",
)

#: Exact index names that are composites (substring hints can't catch these:
#: "제조" as a substring would also kill the legitimate 기타제조 sector).
#: 제조 spans nearly every manufacturing name; on KOSDAQ it precedes the
#: narrow sectors in code order and would swallow them under first-match.
_NON_SECTOR_EXACT = frozenset({"제조"})


def _fetch_index_members(code: str, name: str, date: str, attempts: int = 3) -> list[str]:
    """Constituent list for one index, with retries — KRX intermittently drops
    connections; a sector index must never be silently omitted (spec §6)."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            members = stock.get_index_portfolio_deposit_file(code, date)
            if members:
                return list(members)
            last = KRXDataError("empty constituent list returned")
        except Exception as exc:  # noqa: BLE001 — retried, re-raised typed below
            last = exc
        _time.sleep(2.0 * (i + 1))
    raise KRXDataError(
        f"Constituent fetch failed for sector index {name!r} ({code}) on {date} "
        f"after {attempts} attempts: {type(last).__name__}: {last}. "
        "Refusing to continue with a silently shrunken sector map."
    ) from (last if isinstance(last, Exception) else None)


@_defensive("building sector map from index constituents")
def get_sector_map(date: str, market: str) -> tuple[dict[str, str], list[str]]:
    """ticker -> sector-index-name via KRX sector index membership.

    Returns (mapping, sector_names_used). The candidate sector list is
    heuristic (exclusion-based); poc.py prints it for human validation and
    quant_filter must surface an explicit `unmapped` bucket downstream.
    Composite indices are skipped via _NON_SECTOR_HINTS (substring) and
    _NON_SECTOR_EXACT (exact name) — review the printed list and extend them
    if new composites leak through.

    When a ticker belongs to several indices, the SMALLEST index wins:
    narrower membership means a more specific sector (e.g. 증권/보험 must beat
    the broader 금융).
    """
    fetched: list[tuple[str, list[str]]] = []
    for code in stock.get_index_ticker_list(date, market=market):
        name = stock.get_index_ticker_name(code)
        if name in _NON_SECTOR_EXACT or any(h in name for h in _NON_SECTOR_HINTS):
            continue
        fetched.append((name, _fetch_index_members(code, name, date)))
    mapping: dict[str, str] = {}
    for name, members in sorted(fetched, key=lambda nm: len(nm[1])):
        for t in members:
            mapping.setdefault(t, name)
    if not mapping:
        raise KRXDataError(f"Sector mapping produced 0 entries for {market} on {date}.")
    return mapping, [name for name, _ in fetched]


def extend_map_with_preferred(mapping: dict[str, str], tickers) -> tuple[dict[str, str], int]:
    """Preferred shares (우선주) are absent from sector indices by design.

    Any unmapped ticker whose last character is not '0' inherits the sector of
    its common share (first 5 digits + '0') when that common share is mapped.
    Returns (extended copy, number of inherited entries).
    """
    out = dict(mapping)
    inherited = 0
    for t in tickers:
        if t in out or t[-1] == "0":
            continue
        common = t[:5] + "0"
        if common in out:
            out[t] = out[common]
            inherited += 1
    return out, inherited
