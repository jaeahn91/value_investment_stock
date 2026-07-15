# value_investment_stock

A **verification-gated decision-support pipeline** — an LLM system kept on a short leash by
code-enforced controls, human approval checkpoints, and strict data-provenance rules —
applied to Korean value investing (KOSPI / KOSDAQ) with monthly dollar-cost averaging (적립식 투자).

The investing domain is the application. The reusable core is the **control design**: a set
of mechanisms that let a language model *reason and veto* but never *fabricate, advance, or
execute*. An LLM proposes; code decides what passes; a human approves each step.

> ⚠️ **This is decision-support, NOT investment advice and NOT auto-trading.**
> It surfaces candidates and proposes allocations against rules *you* define. Final
> judgment, execution, taxes, and fees are yours. The author is not a licensed
> advisor. The system never places trades and contains no trade-execution code.

---

## Control design (the point of the project)

The pipeline is engineered so a language model cannot become the source of a decision. Seven
controls enforce that, and together they are the reusable asset — the investing logic is just
where they are exercised:

| Control | What it guarantees |
|---|---|
| **Data flows in, never out** | Every LLM stage is fed a code-generated data pack. Every number originates in `collectors/` (KRX / DART) — never in the model. |
| **`[unverified-llm]` tagging** | Any figure in LLM output that is absent from its input pack is quarantined with this tag and excluded from gate evaluation. No un-sourced number can move a decision. |
| **Code-enforced gates** | Gates 1→2, 2→3, 3→4 are evaluated in code, not left to LLM discretion. A candidate that fails does not advance — and the LLM cannot resurrect a code-rejected name. |
| **Veto-only asymmetry** | The LLM may *tighten* the funnel (veto a candidate with a stated reason) but never *loosen* it. Model influence is one-directional by design. |
| **Human checkpoints (CP-1 – CP-3)** | Human approval is required — per artifact, per run — after Stage 1 (sectors), Stage 3 (verdicts), and Stage 4 (allocation). Nothing carries an approval forward implicitly. |
| **State in files, not chat** | `config.yaml` (rules) and `portfolio.yaml` (holdings + cash) are the single source of truth — auditable, diff-able, reproducible. The pipeline is stateful by design. |
| **Hard execution boundary** | The system proposes; it never disposes. No trade is ever placed; no execution code exists in the repo. |

Every threshold the code applies traces to a key in `config.yaml` — there are no magic numbers.

---

## What it does

Four stages run as a connected funnel. Each hands off to the next via an explicit artifact
(not implicit chat context), and each **code-enforced gate** stops a candidate that fails —
so the pipeline never drifts into "everything looks promising."

| Stage | What it does | Engine | Output |
|---|---|---|---|
| **1. Sector ID** | Find 2–3 promising-but-underpriced sectors (top-down) | LLM | `stage1_sectors.yaml` |
| **2. Stock screen** | 3–5 cheap-but-sound candidates per sector | Quant filter (KRX / DART) | `stage2_queue.yaml` |
| **3. Deep validation** | Per-stock Undervalued / Trap verdict | 11-section LLM prompt | `verdicts/<ticker>.yaml` |
| **4. Allocation** | Monthly DCA buy proposal | Rule-based + portfolio state | `allocation.yaml` |

**The gates:**

- **Gate 1→2** — a sector advances only if it scores ≥4 on **both** axes: structural attractiveness **and** under-pricing. High-promise-but-expensive sectors are rejected with a reason.
- **Gate 2→3** — a stock advances only if it shows cheapness signals **and** meets minimum fundamentals (positive operating cash flow, non-excessive debt, revenue not in structural decline) **and** passes first-pass value-trap exclusion.
- **Gate 3→4** — only "Undervalued" stocks with acceptable trap risk and a defined buy-price level feed the allocator.

Each stage's role is strict: collectors only collect (no analysis); screens only filter (no
valuation); the allocator only sizes (it recommends no ticker on its own authority).

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

Read all thresholds from `config.yaml`. Reports are append-only — never overwrite a prior dated report.

---

## Documentation

- **`CLAUDE.md`** — operational brief; read at the start of every session.
- **`implementation_spec.md`** — authoritative data flow, artifact schemas, gate logic, human-checkpoint definitions (§7), and acceptance criteria. If it conflicts with any other doc, it wins.

## External screener override

If you paste a ticker list from a paid screener (퀀트킹 / 인텔리퀀트 / HTS / FnGuide),
it is used as the Stage 2 candidate set instead of self-screening — but code still runs
the fundamentals, trap, and re-rating gates over it, and the LLM still labels survivors.
The override can replace *sourcing*; it cannot bypass a *gate*.
