# Gate 0 — Collectors POC Report

Generated: 2026-07-08 14:47 · spec: implementation_spec.md §6

## Verdict: **GATE 0 PASS — proceed to quant_filter**

Required criteria: C1, C2, C4, C5, C6, C7, C8 (C3/C10 informational, C9 conditional)

| ID | Criterion | Status | Elapsed |
|---|---|---|---|
| P0 | Preflight: credentials & environment | INFO | 0.0s |
| C1 | Universe: full KOSPI+KOSDAQ listing w/ market cap | PASS | 16.0s |
| C2 | Fundamentals snapshot: whole universe, one date | PASS | 13.5s |
| C3 | PER/EPS basis determination (last-FY vs TTM) | INFO | 4.3s |
| C4 | 5-year monthly PER/PBR history for 3 samples | PASS | 113.5s |
| C5 | Sector mapping via KRX index constituents (coverage ≥ 95%) | PASS | 945.2s |
| C6 | Failure modes: typed errors, invalid ticker, holiday fallback (folds in C11) | PASS | 0.1s |
| C7 | DART key + corp_code mapping coverage | PASS | 0.0s |
| C8 | 4 FY annual consolidated financials for samples (key items) | PASS | 4.1s |
| C9 | Standalone (OFS) fallback path | PASS | 0.0s |
| C10 | DART latency / projected Stage-2 wall time | INFO | 0.0s |

## Details

### P0 — Preflight: credentials & environment [INFO]
- KRX_ID: set — pykrx >= 1.2 requires a free data.krx.co.kr login
- KRX_PW: set — pykrx >= 1.2 requires a free data.krx.co.kr login
- DART_API_KEY: set — DART criteria (C3, C7–C10) run only with a key

### C1 — Universe: full KOSPI+KOSDAQ listing w/ market cap [PASS]
- ref_date=20260708; counts={'KOSDAQ': 1820, 'KOSPI': 945}; total=2765

### C2 — Fundamentals snapshot: whole universe, one date [PASS]
- rows=2718; cols=['BPS', 'PER', 'PBR', 'EPS', 'DIV', 'DPS']; wall=13.5s

### C3 — PER/EPS basis determination (last-FY vs TTM) [INFO]
- 005380: KRX EPS 36088 ≈ DART FY2025 basic EPS 36088 → basis=FY2025
- 000880: KRX EPS 4072 ≈ DART FY2025 basic EPS 4072 → basis=FY2025
- 058470: KRX EPS 2002 ≈ DART FY2025 basic EPS 2002 → basis=FY2025
- ACTION: copy the determined basis into the `basis` tag quant_filter stamps on PER/PBR.

### C4 — 5-year monthly PER/PBR history for 3 samples [PASS]
- 005380 현대차 (KOSPI large): 62 monthly rows, PBR non-null 62
- 000880 한화 (KOSPI mid): 62 monthly rows, PBR non-null 62
- 058470 리노공업 (KOSDAQ): 62 monthly rows, PBR non-null 62

### C5 — Sector mapping via KRX index constituents (coverage ≥ 95%) [PASS]
- KOSPI: 23 sector indices → ['IT 서비스', '건설', '금속', '금융', '기계·장비', '보험', '부동산', '비금속', '섬유·의류', '오락·문화', '운송·창고', '운송장비·부품', '유통', '음식료·담배', '의료·정밀기기', '일반서비스', '전기·가스', '전기전자', '제약', '종이·목재', '증권', '통신', '화학']
- KOSDAQ: 21 sector indices → ['IT 서비스', '건설', '금속', '금융', '기계·장비', '기타제조', '비금속', '섬유·의류', '오락·문화', '운송·창고', '운송장비·부품', '유통', '음식료·담배', '의료·정밀기기', '일반서비스', '전기전자', '제약', '종이·목재', '출판·매체복제', '통신', '화학']
- index-membership coverage: 94.4%; preferred shares inheriting common-share sector: +113
- final coverage vs universe: 98.5% (target ≥ 95%); unmapped go to explicit `unmapped` bucket
- EYEBALL the sector lists above: extend _NON_SECTOR_HINTS / _NON_SECTOR_EXACT if composites leaked through.

### C6 — Failure modes: typed errors, invalid ticker, holiday fallback (folds in C11) [PASS]
- OK  invalid ticker -> typed error: Unknown or delisted ticker: '999999'
- OK  weekend -> prev business day: 20260628 -> 20260626
- trading-halt behavior: observational only — note anomalies during real runs.

### C7 — DART key + corp_code mapping coverage [PASS]
- listed companies mapped: 3976
- coverage vs universe: 95.9%

### C8 — 4 FY annual consolidated financials for samples (key items) [PASS]
- 005380 현대차 (KOSPI large): complete key-item years = 4/4; basis by year = {2025: 'consolidated', 2024: 'consolidated', 2023: 'consolidated', 2022: 'consolidated'}
- 000880 한화 (KOSPI mid): complete key-item years = 4/4; basis by year = {2025: 'consolidated', 2024: 'consolidated', 2023: 'consolidated', 2022: 'consolidated'}
- 058470 리노공업 (KOSDAQ): complete key-item years = 4/4; basis by year = {2025: 'standalone', 2024: 'standalone', 2023: 'standalone', 2022: 'standalone'}

### C9 — Standalone (OFS) fallback path [PASS]
- fallback exercised on: [('058470', 2025), ('058470', 2024), ('058470', 2023), ('058470', 2022)]

### C10 — DART latency / projected Stage-2 wall time [INFO]
- requests=25, avg=0.12s, max=0.66s
- projected Stage-2 pull (50 tickers × 4 FYs): ~0.9 min (double if OFS fallback rate is high)
- record any HTTP 429 / quota messages here for the rate-limit ledger — do not assume limits.
