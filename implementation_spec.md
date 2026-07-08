# implementation_spec.md — From Draft Prompts to a Running System (v0.1)

**This is the authoritative design-and-contracts document** for the project; `CLAUDE.md` is the operational brief. This document defines the concrete contracts — data flow, file schemas, gate logic, acceptance criteria, and human checkpoints — required to turn the Stage 1–4 concept into working code. If `CLAUDE.md` and this spec conflict, this spec wins — flag the conflict rather than silently diverging.

**Hard boundary (restated):** decision-support only. Never investment advice, never trade execution.

---

## 1. The one structural correction: invert the data flow

The current `prompts/discovery.md` asks the LLM to *pull* quantitative metrics (Stage 2b: "Pull and label..."). This contradicts two rules already in `CLAUDE.md`:

- *"Gates are enforced in code, not left to LLM discretion."*
- *"Mark unverifiable figures `[unverified]` — never fabricate precision."*

An LLM cannot reliably fetch current P/E, P/B, or OCF for a Korean equity universe. If asked to, it will either produce stale training-data figures or fabricate — which is exactly the failure the data-integrity rules exist to prevent. The same applies to Stage 1: Axis B ("degree of under-pricing") is a quantitative question, and an LLM scoring it from memory produces unauditable vibes.

**System-wide invariant (add to CLAUDE.md):**

> **Numbers flow from `collectors/` into prompts — never out of the LLM.** Every prompt receives a code-generated data pack as input. Any figure appearing in LLM output that is not present in its input pack is tagged `[unverified-llm]` by the pipeline and excluded from gate evaluation.

Division of labor per stage, corrected:

| Stage | Code does | LLM does | Human does |
|---|---|---|---|
| 1 Sector ID | Build `sector_dashboard.yaml` (sector median P/B, P/E percentile vs 5y, 1y sector index return, breadth) | Score Axis A (structural narrative — genuinely qualitative) and Axis B *grounded in the dashboard*; write theses, catalysts, risks | Approve/veto advancing sectors (CP-1) |
| 2 Stock screen | Universe snapshot, all metrics, **all gates** (cheapness, fundamentals, trap pre-exclusion, re-rating guard) | Review the code-passed table; write the one-line "cheap-not-trap" labels; may **veto with reason**, may never resurrect a code-rejected name | — |
| 3 Deep dive | Assemble per-ticker data pack (4y financials, valuation history, shareholder structure, recent DART disclosures list) | Run the 11-section valuation on the pack; emit verdict fields | Approve verdicts into the buy list (CP-2) |
| 4 Allocation | Everything (pure rule execution) | Nothing | Execute trades manually; record fills (CP-3) |

This preserves what LLMs are actually good at (structural narrative, trap-reason articulation, governance reading) and removes what they are bad at (being a data source).

---

## 2. Pipeline as it should actually run

```
collectors (pykrx + DART)
   │
   ├─► runs/<run_id>/universe.csv            (all listings + metrics snapshot)
   ├─► runs/<run_id>/sector_dashboard.yaml   (sector-level valuation state)
   │
   ▼
[LLM: Stage 1 prompt + dashboard] ─► runs/<run_id>/stage1_sectors.yaml
   │                                          │
   │                              CP-1: user approves sectors
   ▼                                          ▼
[code: quant_filter.py]  ◄── config.yaml thresholds
   │        (screens universe within approved sectors; enforces Gate 2→3)
   ▼
runs/<run_id>/stage2_queue.yaml
   │
   ▼
[LLM: Stage 2 review prompt + queue] ─► labels/vetoes written back into queue
   │
   ▼   for each queued ticker:
[code: assemble data pack] ─► [LLM: Stage 3 valuation prompt]
   │
   ▼
verdicts/<ticker>.yaml  +  outputs/deepdive_<ticker>_<date>.md
   │
   │        CP-2: user approves verdicts
   ▼
[code: allocator.py + portfolio.yaml + budget]   (Gate 3→4 in code)
   │
   ▼
runs/<run_id>/allocation.yaml  +  outputs/report_<date>.md
   │
   │        CP-3: user executes manually, records fills
   ▼
portfolio.yaml updated  +  decisions.log.yaml appended
```

`run_id` is `YYYYMM` (monthly cadence). All intermediate artifacts live under `runs/<run_id>/`; `outputs/` holds only human-readable reports generated *from* artifacts. Reports are views; artifacts are state.

**Repo layout additions** (project root = repo root, `value_investment_stock/`):

```
value_investment_stock/
├── runs/                      # NEW: machine-readable stage artifacts, per run
│   └── 202607/
├── verdicts/                  # NEW: Stage 3 verdicts, one file per ticker (TTL-governed)
├── decisions.log.yaml         # NEW: append-only record of every executed buy + rationale
├── .claude/commands/          # NEW: /screen, /deepdive, /allocate as Claude Code commands
└── (rest as in CLAUDE.md)
```

---

## 3. Artifact schemas (stage contracts)

All YAML. Every metric object carries data-integrity tags inline. Schemas below are normative for v1; extend, don't mutate.

### 3.1 `runs/<run_id>/stage1_sectors.yaml`

```yaml
run_id: "202607"
generated: "2026-07-03"
inputs:
  sector_dashboard: "runs/202607/sector_dashboard.yaml"
sectors:
  - name: "example_sector"
    axis_a: 4
    axis_b: 5
    verdict: ADVANCE            # ADVANCE | REJECT
    thesis: "2-3 sentence structural driver + why under-priced"
    catalyst:
      description: "..."
      horizon_months: [6, 18]   # null if no clear timeline — flag it
    key_risk: "..."
    policy_dependency: null      # or explicit note + cycle risk
rejected:
  - { name: "...", axis_a: 5, axis_b: 2, reason: "already re-rated; great but expensive" }
approved_by_user: false          # CP-1: pipeline halts until true
```

### 3.2 `runs/<run_id>/stage2_queue.yaml`

```yaml
run_id: "202607"
generated: "2026-07-05"
data_basis:
  statements: consolidated       # or standalone — per ticker override allowed
  valuation_basis: "per pykrx (verify: last-FY vs TTM in Gate 0)"
  price_date: "2026-07-04"
  sources: [pykrx, dart]
candidates:
  - ticker: "000000"
    name: "..."
    sector: "..."
    metrics:
      per:  { value: 5.2, ref_date: "2026-07-04", basis: last-FY, vs_sector_median_pct: -38, own_5y_percentile: 0.18 }
      pbr:  { value: 0.6, ref_date: "2026-07-04", basis: last-FY, vs_sector_median_pct: -25, own_5y_percentile: 0.22 }
      roe:  { value: 0.11, ref_date: "2025-FY", basis: consolidated }
      ocf_3y: { values_krw_bn: [210, 260, 240], basis: consolidated, source: dart }
      net_debt_to_equity: { value: 0.4, ref_date: "2025-FY", basis: consolidated }
      rev_trend_3y: [+0.04, +0.06, +0.02]
      op_margin_trend_3y: [0.081, 0.088, 0.085]
      price_return_1y: 0.12
    gates:                       # every gate result recorded, pass or fail
      cheapness: pass
      fundamentals: pass
      trap_preexclusion: pass
      rerating_guard: pass
    llm_label: "cheap vs own 5y P/B band; not a trap because revenue and margins are flat-to-up while multiple sits at 20th percentile"
    llm_veto: null               # or { reason: "..." } — veto only, never resurrect
rejected:
  - { ticker: "...", name: "...", failed_gate: trap_preexclusion, reason: "3 consecutive yrs simultaneous rev+margin decline (2023-2025, consolidated)" }
```

### 3.3 `verdicts/<ticker>.yaml`

```yaml
ticker: "000000"
name: "..."
run_id: "202607"
generated: "2026-07-06"
verdict: UNDERVALUED             # UNDERVALUED | WATCH | TRAP
conviction: 4                    # 1-5
trap_risk: low                   # low | medium | high
intrinsic_band_krw: [52000, 68000]
buy_below_krw: 50000             # required if verdict == UNDERVALUED
thesis_1line: "..."
governance_flags: []             # standalone section per design principle
data_basis: { statements: consolidated, valuation: TTM, pack: "runs/202607/packs/000000.yaml" }
expires: "2027-01-06"            # generated + verdict_ttl_months
approved_by_user: false          # CP-2
report: "outputs/deepdive_000000_20260706.md"
```

### 3.4 `runs/<run_id>/allocation.yaml`

```yaml
run_id: "202607"
generated: "2026-07-07"
budget_krw: 1000000
price_date: "2026-07-07"
orders:
  - ticker: "000000"
    shares: 12
    ref_price_krw: 48200
    amount_krw: 578400
    tier: add_zone               # deep_value | add_zone
    rationale: "18% below buy_below; position 6% vs 15% cap; sector 12% vs 30% cap"
post_buy_weights: { "000000": 0.09, cash: 0.11 }
leftover_cash_krw: 421600
constraints_hit: ["whole-share rounding reallocated 21,600 KRW to cash"]
held_cash_reason: null           # populated when nothing is in a buy zone
```

On CP-3 (user records fills), each executed order is appended to `decisions.log.yaml` with fill price, date, and the rationale copied verbatim — this is the raw material for future sell decisions.

---

## 4. `config.yaml` — full schema (v1 defaults)

Defaults below are starting points to tune, not recommendations. Every threshold used anywhere in code must trace to a key here.

```yaml
meta:
  version: 1
  last_reviewed: "2026-07-03"

universe:
  markets: [KOSPI, KOSDAQ]
  min_market_cap_krw: 300000000000        # 3,000억 — avoid microcap illiquidity
  min_avg_daily_value_krw: 1000000000     # 10억/day, 60-trading-day average
  exclude:
    admin_issue: true                     # 관리종목 / 투자주의환기
    trading_halt: true
    spac: true
    preferred_shares: true
    reits: true

sectors:
  advance_min_axis_a: 4
  advance_min_axis_b: 4
  max_advancing: 3

screening:                                # Gate 2→3, all code-enforced
  max_per: 12
  max_pbr: 1.2
  min_roe: 0.07
  require_positive_ocf: true              # latest FY; 3y sum must also be > 0
  max_net_debt_to_equity: 1.0
  candidates_per_sector: 5
  trap_exclusion:
    max_consecutive_rev_and_margin_decline_yrs: 2    # 3+ simultaneous → cut
  rerating_guard:                         # the Hyundai-trap starting-point check
    max_price_return_1y: 0.60             # >60% in 12m → not a discovery candidate
    max_own_5y_pbr_percentile: 0.60       # PBR above own 60th pctile → drop

allocation:
  max_weight_per_stock: 0.15
  max_weight_per_sector: 0.30
  cash_floor: 0.05
  cash_ceiling: 0.30
  allow_hold_cash: true
  tier_weights:
    deep_value: 1.5                       # price <= buy_below * deep_value_discount
    add_zone: 1.0                         # price <= buy_below
  deep_value_discount: 0.90
  rebalance_pull: 0.5                     # 0..1 boost toward underweight names

data:
  annual_years: 4                         # 4 FYs needed to test 3 consecutive declines
  statement_basis: consolidated           # fallback standalone — must be tagged
  price_source: pykrx
  financial_source: dart

review:
  verdict_ttl_months: 6                   # expired verdicts cannot feed Gate 3→4
```

**Notes on specific choices:**

- `rerating_guard` operationalizes discovery.md §2a mechanically instead of leaving it to LLM judgment. Both thresholds are deliberately in config so the guard can be tuned without touching code.
- `annual_years: 4` is the minimum to compute three year-over-year deltas for the trap gate.
- `verdict_ttl_months` prevents a stale Stage 3 verdict from silently feeding the allocator for years — a thesis is re-validated or it dies.

---

## 5. `portfolio.yaml` — schema

```yaml
meta:
  currency: KRW
  as_of: "2026-07-01"
cash: 1250000
positions:
  - ticker: "005380"
    name: "현대차"
    shares: 4
    avg_cost_krw: 415000
    sector: autos
    first_bought: "2025-11-03"
    tag: catalyst_bet             # value_entry | catalyst_bet | legacy — labeling discipline
    notes: "tariff variable dominates near-term band"
```

The `tag` field carries the value-entry vs. catalyst-bet distinction into state, so the allocator and future sell logic can treat the categories differently (e.g., catalyst bets don't receive rebalancing pull by default — a config decision to make explicitly later).

---

## 6. Gate 0 — collectors proof-of-concept acceptance criteria

`CLAUDE.md` says "verify data access before building downstream logic." This section defines what *verified* means. The POC produces `outputs/poc_report.md` with an explicit pass/fail per criterion. **Build proceeds past collectors only on full pass or documented, accepted workaround.**

**pykrx (krx_collector.py):**

1. Full KOSPI+KOSDAQ ticker list with market cap for a given date — completeness sanity check (count vs. known listing counts).
2. Fundamentals snapshot (PER/PBR/EPS/BPS/DIV) for the entire universe on one date; measure wall time.
3. **Determine empirically what basis KRX-published PER/PBR uses** (last-FY vs. TTM) and record it — this becomes the `basis` tag on every valuation figure. Do not assume.
4. 5-year monthly PBR/PER history for 3 sample tickers (large-cap, mid-cap, KOSDAQ) — required for `own_5y_percentile`.
5. Sector mapping: pull KRX sector-index constituents; measure coverage (% of universe mapped). Target ≥95%; unmapped names go to an explicit `unmapped` bucket, never silently dropped.
6. Failure-mode behavior: delisted ticker, trading-halt day, non-trading day (must fall back to last trading day, not crash).

**DART (dart_collector.py):**

7. `DART_API_KEY` read from environment; corp_code master file downloads and maps ticker → corp_code for the universe (measure mapping coverage).
8. 4 fiscal years of annual consolidated IS/BS/CF for the same 3 sample tickers; extract revenue, operating profit, OCF, total debt, equity.
9. Standalone-only company handling: detect absence of consolidated statements, fall back with basis tag flipped.
10. Observe and record actual rate-limit behavior and per-request latency; estimate wall time for a ~50-ticker Stage 2 candidate pull.

**Cross-cutting:**

11. Every collector function wraps its scrape/API call defensively and raises a typed, human-readable error (pykrx scrapes KRX and *will* break on site changes — the failure must be loud).

---

## 7. Human checkpoints — the precise meaning of "semi-automated"

| ID | Where | Mechanism | Rule |
|---|---|---|---|
| CP-1 | After Stage 1 | `approved_by_user: false → true` in `stage1_sectors.yaml` (or `/approve sectors`) | quant_filter refuses to run against an unapproved sector file |
| CP-2 | After Stage 3 | Same flag per verdict file | Allocator's Gate 3→4 requires `verdict == UNDERVALUED && approved_by_user && not expired` |
| CP-3 | After Stage 4 | Manual trade execution; fills recorded back into `portfolio.yaml` + `decisions.log.yaml` | The system never executes; an allocation report that was never executed simply expires with the run |

Approval is per-run and per-artifact. Nothing carries an approval forward implicitly.

---

## 8. Prompt revisions required (`discovery.md` → v2)

The corrected data flow splits `prompts/discovery.md` into two prompts with explicit I/O contracts:

**`prompts/stage1_sector.md`** — *Input:* `sector_dashboard.yaml` (code-generated). *Output:* the fields of `stage1_sectors.yaml`. Changes from current draft: Axis B scoring must cite dashboard figures, not general knowledge; the two-axis table, gate rule (≥4/≥4), rejection-with-reason discipline, and policy-cycle note all carry over unchanged — they are the good bones of the current draft.

**`prompts/stage2_review.md`** — *Input:* `stage2_queue.yaml` candidates (already gate-passed by code). *Output:* `llm_label` and optional `llm_veto` per candidate. Changes: delete Stage 2b entirely (the LLM no longer pulls anything); §2a's starting-point check moves into code as `rerating_guard`; the LLM's remaining job is articulating *why* each survivor is plausibly cheap-not-trap, plus a veto right for qualitative red flags the quant gates can't see (governance news, controlling-shareholder behavior). **Veto-only asymmetry is deliberate:** the LLM can tighten the funnel, never loosen it.

The external-screener override (퀀트킹 / 인텔리퀀트 lists) slots in cleanly: the pasted list replaces the universe→cheapness step, but code still runs fundamentals, trap, and re-rating gates over it, and the LLM still labels survivors.

The Section 0 operating frame, data-integrity rules, and tone/discipline sections of the current draft carry into both prompts verbatim — they are correct as written.

---

## 9. Failure modes & defensive defaults

- **pykrx breakage** (KRX site change): typed error, run halts, no partial artifacts written. A run directory is valid only if complete.
- **Missing consolidated statements:** fall back to standalone with the basis tag flipped and a warning in the report — never mixed silently within one ticker's history.
- **Sector mapping gaps:** `unmapped` bucket surfaced in the Stage 2 report; user decides.
- **Conflicting figures** (pykrx-derived vs. DART-derived): DART wins for statement items, pykrx wins for market data; the adopted figure and the discarded one are both recorded per the data-integrity rule.
- **Expired verdicts:** hard-excluded at Gate 3→4; the allocation report lists them under "expired — re-run /deepdive to reconsider."
- **LLM-introduced figures:** stripped from gate evaluation, tagged `[unverified-llm]` in reports (see §1 invariant).

---

## 10. v1 scope cuts (explicit deferrals)

Deliberately out of v1 so the pipeline reaches end-to-end sooner:

1. **EV/EBITDA** — pykrx doesn't provide it; computing it requires DART-derived EBITDA + net debt per ticker across the universe. v1 gates run on PER/PBR + fundamentals (OCF, debt, trends), which already cover the trap logic. Add in v1.1 for Stage 2 candidates only (~50 tickers, cheap via DART).
2. **TTM statements** — v1 runs annual-basis (4 FYs). Quarterly/TTM normalization is v1.1; every figure is basis-tagged either way, so the upgrade is additive.
3. **Global-peer sector comparison** (Axis B input) — v1 dashboard uses own-history percentiles only; global peer multiples are manual/LLM-qualitative until a data source is chosen.
4. **Streamlit dashboard** — Phase 2, unchanged.
5. **Sell logic** — out of scope entirely for now, but `decisions.log.yaml` and the `tag` field in `portfolio.yaml` are designed as its future inputs.

---

## 11. Updated build order

1. ✅ `prompts/discovery.md` draft, `CLAUDE.md`
2. ✅ **This spec** — schemas + contracts (fold the §1 invariant into `CLAUDE.md`)
3. **Gate 0:** `collectors/` POC against §6 criteria → `poc_report.md`
4. `screens/sector_tagger.py` + `screens/quant_filter.py` → produces `stage2_queue.yaml`
5. Split prompts per §8 (`stage1_sector.md`, `stage2_review.md`); wire sector_dashboard generation
6. `analysis/deep_dive_llm.py` data-pack assembly + verdict writing
7. `allocate/allocator.py` against `allocation.yaml` schema
8. `pipeline.py` + `.claude/commands/` slash commands
9. (Phase 2) dashboard
