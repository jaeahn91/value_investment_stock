# value_investment_stock

A semi-automated **decision-support** pipeline for Korean value investing (KOSPI / KOSDAQ) with monthly dollar-cost averaging (적립식 투자).

> ⚠️ **This is decision-support, NOT investment advice and NOT auto-trading.**
> It surfaces candidates and proposes allocations against rules *you* define. Final
> judgment, execution, taxes, and fees are yours. The author is not a licensed
> advisor. The system never places trades and contains no trade-execution code.

---

## What it does

Four stages run as a connected funnel with **code-enforced gates** — a candidate that
fails a gate does not advance, so the pipeline never drifts into "everything looks promising."

| Stage | What it does | Engine | Output |
|---|---|---|---|
| **1. Sector ID** | Find 2–3 promising-but-underpriced sectors (top-down) | LLM | `stage1_sectors.yaml` |
| **2. Stock screen** | 3–5 cheap-but-sound candidates per sector | Quant filter (KRX/DART) | `stage2_queue.yaml` |
| **3. Deep validation** | Per-stock Undervalued / Trap verdict | 11-section LLM prompt | `verdicts/<ticker>.yaml` |
| **4. Allocation** | Monthly DCA buy proposal | Rule-based + portfolio state | `allocation.yaml` |

### The gates (enforced in code, not left to LLM discretion)

- **Gate 1→2** — a sector advances only if it scores ≥4 on **both** axes: structural attractiveness **and** under-pricing.
- **Gate 2→3** — a stock advances only if it shows cheapness signals **and** meets minimum fundamentals (positive operating cash flow, non-excessive debt, revenue not in structural decline) **and** passes first-pass value-trap exclusion.
- **Gate 3→4** — only "Undervalued" stocks with acceptable trap risk and a defined buy-price level feed the allocator.

### Core architectural rule

**Numbers flow from `collectors/` *into* the LLM prompts — never *out* of the LLM.**
Every LLM stage receives a code-generated data pack as input. Any figure in LLM output
not present in its input pack is tagged `[unverified-llm]` and excluded from gate
evaluation. The LLM may *veto* a candidate with a reason; it may never resurrect a
code-rejected one. **State lives in files** (`config.yaml`, `portfolio.yaml`), never in chat.

---

## Setup

Requires **Python 3.11+**.

```bash
pip install -r requirements.txt   # pykrx, requests, pandas, PyYAML
```

Two free accounts / keys are required, supplied via environment variables (never hardcoded, never committed):

| Variable | For | How to get |
|---|---|---|
| `KRX_ID` / `KRX_PW` | `pykrx` ≥ 1.2 (prices & valuation metrics) | Free account at [data.krx.co.kr](https://data.krx.co.kr) |
| `DART_API_KEY` | DART OpenAPI (financial statements) | Free API key at [opendart.fss.or.kr](https://opendart.fss.or.kr) |

Verify data access before anything downstream:

```bash
python poc.py    # Gate 0 acceptance-criteria runner → outputs/poc_report.md
```

---

## Status

Build order mirrors `implementation_spec.md` §11.

- [x] **Gate 0** — `collectors/` verified (POC passed 2026-07-08; sector coverage 98.5%)
- [x] Stage 2 — `screens/quant_filter.py` + `screens/sector_tagger.py`
- [x] Stage 1 input — `analysis/sector_dashboard.py`; prompts split (`stage1_sector.md`, `stage2_review.md`)
- [x] Stage 3 — `analysis/deep_dive_llm.py` data-pack assembly + verdict writing (`prompts/valuation.md` is a **draft**, user review pending)
- [ ] **Stage 4 — `allocate/allocator.py`** (monthly DCA sizing) ← next
- [ ] `pipeline.py` + `.claude/commands/` slash commands
- [ ] (Later) Phase 2 local dashboard (Streamlit)

### Planned workflow (once Stage 4 + slash commands land)

- `/screen` — run discovery (Stages 1–2), output the Stage 3 input queue
- `/deepdive <ticker>` — run the 11-section valuation on one name
- `/allocate <budget>` — size this month's DCA buys against current `portfolio.yaml`

---

## Repo layout

```
value_investment_stock/
├── CLAUDE.md                  # operational brief (read first each session)
├── implementation_spec.md     # authoritative design & contracts (wins on conflict)
├── config.yaml                # screening + allocation rules (no magic numbers in code)
├── poc.py                     # Gate 0 acceptance runner
├── collectors/                # ✅ collect only, no analysis
│   ├── krx_collector.py       #    pykrx: prices & metrics
│   └── dart_collector.py      #    DART API: financial statements
├── screens/                   # ✅ filter only, no valuation
│   ├── quant_filter.py        #    Stage 2 quant filter + value-trap pre-exclusion
│   └── sector_tagger.py       #    stock → sector mapping
├── analysis/                  # ✅
│   ├── sector_dashboard.py    #    Stage 1 input generator
│   └── deep_dive_llm.py       #    Stage 3 data-pack + verdict writing
├── prompts/                   # ✅ stage1_sector.md, stage2_review.md, valuation.md
├── allocate/allocator.py      # ◻ Stage 4 sizing (build target)
├── pipeline.py                # ◻ one-command orchestration (build target)
├── portfolio.yaml             # ◻ holdings + cash, updated monthly (build target)
├── decisions.log.yaml         # ◻ append-only executed-buy record (build target)
├── runs/<run_id>/             # ◻ machine-readable stage artifacts per run (YYYYMM)
├── verdicts/                  # ◻ Stage 3 verdicts, one YAML per ticker
└── outputs/report_YYYYMMDD.md # human-readable reports (append-only history)
```

Place new modules by stage: collectors only collect; screens only filter; the allocator
only sizes (it recommends no ticker on its own authority). Read all thresholds from
`config.yaml`. Reports are append-only — never overwrite a prior dated report.

---

## Documentation

- **`CLAUDE.md`** — operational brief; read at the start of every session.
- **`implementation_spec.md`** — authoritative data flow, artifact schemas, gate logic, acceptance criteria. If it conflicts with any other doc, it wins.

## External screener override

If you paste a ticker list from a paid screener (퀀트킹 / 인텔리퀀트 / HTS / FnGuide),
it is used as the Stage 2 candidate set instead of self-screening — but code still runs
the fundamentals, trap, and re-rating gates over it, and the LLM still labels survivors.
