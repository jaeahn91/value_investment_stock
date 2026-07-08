# CLAUDE.md — Korean Value-Investing Screener & Allocator

Project instructions for Claude Code. Read this at the start of every session. This file is the operational brief; the authoritative design-and-contracts document is `implementation_spec.md` (data flow, artifact schemas, gate logic, acceptance criteria). If the two conflict, `implementation_spec.md` wins — flag the conflict rather than silently diverging. There is no separate `system_design.md`; any reference to it is stale.

---

## What this project is

A semi-automated, **decision-support** pipeline for Korean value investing with monthly dollar-cost averaging (적립식 투자). Four stages run as a connected funnel with **code-enforced gates**:

1. **Sector ID** (top-down, LLM) → 2–3 promising-but-underpriced sectors
2. **Stock screen** (quant filter, KRX/DART) → 3–5 cheap-but-sound candidates per sector
3. **Deep validation** (11-section LLM prompt) → per-stock Undervalued / Trap verdict
4. **Monthly allocation** (rule-based + portfolio state) → DCA buy proposal

**Hard boundary — keep this explicit in every generated report:** This is decision-support, NOT investment advice and NOT auto-trading. It surfaces candidates and proposes allocations against rules the user defined. Final judgment, execution, taxes, and fees are the user's. The author is not a licensed advisor. Never place trades; never write trade-execution code.

---

## Core architectural rules (do not violate)

- **Numbers flow from `collectors/` into prompts — never out of the LLM.** Every LLM stage receives a code-generated data pack as input. Any figure in LLM output not present in its input pack is tagged `[unverified-llm]` and excluded from gate evaluation. The LLM may veto candidates with a reason; it may never resurrect a code-rejected one. (Contracts: `implementation_spec.md`.)
- **State lives in files, never in chat.** `portfolio.yaml` (quantities + cash) and `config.yaml` (rules) are the source of truth. The pipeline is stateful by design — never restructure it into a stateless chatbot.
- **Gates are enforced in code, not left to LLM discretion.** A candidate that fails a gate does not advance. This prevents the "everything looks promising" divergence.
- **Stages hand off via explicit artifacts**, not implicit context. Each stage reads the prior stage's output file/queue and writes its own.
- **Data integrity is mandatory on every figure:** reference date, consolidated vs. standalone, TTM vs. forward vs. last-FY. When sources conflict, record the adopted figure and why. Mark unverifiable figures `[unverified]` — never fabricate precision.
- **Verify data access before building downstream logic.** The whole pipeline depends on KRX/DART working. If `collectors/` fails, stop and fix it before touching allocation logic.

---

## The gates (enforce exactly)

- **Gate 1→2:** Sector advances only if it scores ≥4 on BOTH axes (structural attractiveness AND under-pricing). Reject high-promise/expensive sectors explicitly with a reason.
- **Gate 2→3:** Stock advances only if it shows cheapness signals (low P/E, P/B, EV/EBITDA vs. sector or own history) AND meets minimum fundamentals (positive operating cash flow, non-excessive debt, revenue not in structural decline) AND passes first-pass value-trap exclusion (cut on 3+ consecutive years of simultaneous revenue and margin decline).
- **Gate 3→4:** Only stocks rated "Undervalued" with acceptable trap risk and a defined buy-consideration price level feed the allocator.

---

## Tech stack

- **Language:** Python 3.11+
- **Market data:** `pykrx` — prices and valuation metrics across all listings. It scrapes KRX, so it can break on site changes; wrap calls defensively and surface a clear error rather than failing silently. **pykrx ≥ 1.2 requires a free data.krx.co.kr account: set `KRX_ID` / `KRX_PW` environment variables (never hardcode; never commit).**
- **Financials:** DART OpenAPI — official filings. **Requires a free API key** in the `DART_API_KEY` environment variable (never hardcode it; never commit it).
- **External override:** if the user pastes a ticker list from a paid screener (퀀트킹 / 인텔리퀀트 / HTS / FnGuide), use it as the Stage 2 candidate set instead of self-screening — but still apply trap-exclusion and data-integrity labeling to it.
- **Config/state:** YAML (`config.yaml`, `portfolio.yaml`).
- **Output:** dated Markdown reports in `outputs/`.

---

## Repo layout

Project root = repo root (`value_investment_stock/`). ✅ = exists today; everything else is the build target.

```
value_investment_stock/
├── CLAUDE.md                  # ✅ this file — operational brief
├── implementation_spec.md     # ✅ authoritative design & contracts
├── README.md                  # ✅
├── requirements.txt           # ✅
├── poc.py                     # ✅ Gate 0 acceptance-criteria runner (spec §6)
├── config.yaml                # ✅ screening + allocation rules (set once; schema: spec §4)
├── portfolio.yaml             # current holdings + cash (updated monthly; schema: spec §5)
├── decisions.log.yaml         # append-only record of executed buys + rationale
├── pipeline.py                # orchestration / one-command entry point
├── collectors/                # ✅
│   ├── krx_collector.py       # ✅ pykrx: prices & metrics
│   └── dart_collector.py      # ✅ DART API: financial statements
├── screens/                   # ✅
│   ├── quant_filter.py        # ✅ Stage 2 quant filter + value-trap pre-exclusion
│   └── sector_tagger.py       # ✅ stock → sector mapping (per-run cache)
├── analysis/
│   ├── sector_dashboard.py    # ✅ Stage 1 input: sector_dashboard.yaml generator
│   └── deep_dive_llm.py       # ✅ Stage 3 data-pack assembly + verdict writing
├── allocate/
│   └── allocator.py           # Stage 4 monthly DCA sizing
├── prompts/                   # (discovery.md draft was split per spec §8 and deleted — see git history)
│   ├── stage1_sector.md       # ✅ Stage 1 prompt (input: sector_dashboard.yaml)
│   ├── stage2_review.md       # ✅ Stage 2 label/veto prompt (input: stage2_queue.yaml)
│   └── valuation.md           # ✅ Stage 3 prompt (11-section) — DRAFT, user review pending
├── .claude/commands/          # /screen, /deepdive, /allocate slash commands
├── runs/                      # machine-readable stage artifacts, per run_id (YYYYMM)
│   └── <run_id>/              # universe.csv, sector_dashboard.yaml, stage1_sectors.yaml, stage2_queue.yaml, allocation.yaml
├── verdicts/                  # Stage 3 verdicts, one YAML per ticker (TTL-governed)
└── outputs/                   # human-readable reports only (created at runtime)
    └── report_YYYYMMDD.md     # reports are views; runs/ artifacts are state
```

When adding a module, place it by stage. Collectors only collect (no analysis); screens only filter (no valuation); the allocator only sizes (no ticker recommendation on its own authority).

---

## Slash commands (target workflow)

- `/screen` — run full discovery (Stages 1–2), output the Stage 3 input queue.
- `/deepdive <ticker>` — run Stage 3 (11-section valuation) on one name.
- `/allocate <budget>` — run Stage 4 for this month's budget against current `portfolio.yaml`.

---

## The allocator (Stage 4) — sizing logic

Consumes Stage 3 BUYs + `portfolio.yaml` + this month's budget. Then:

1. **Price-tier weighting** — a name in its "add-position" zone gets more weight than one merely in "watch"; cheaper-vs-estimated-value → larger slice.
2. **Rebalancing pull** — underweight positions (vs. target max weight) get priority; names at/above max weight are skipped or trimmed.
3. **Constraint enforcement** — per-stock max weight, sector cap, cash floor/ceiling, and **whole-share lots** (Korean stocks trade in 1-share units; round down and reallocate the remainder).
4. **Cash-is-a-position** — if nothing is in a buy zone and `allow_hold_cash` is true, recommend holding cash rather than forcing deployment.

Output: per-stock won amount + share count, resulting post-buy weights, leftover cash, a one-line rationale per buy, and a log entry recording *why* each buy was made (needed for future sell decisions). Read thresholds from `config.yaml`; never hardcode them.

---

## Conventions

- Read all thresholds and rules from `config.yaml` — no magic numbers in code.
- Never commit secrets. `DART_API_KEY` comes from the environment.
- Reports are append-only history: never overwrite a prior `report_YYYYMMDD.md`.
- Keep figures labeled with their data-integrity tags through every stage — don't strip them at handoff.
- When in doubt about whether something is "advice," err toward mechanical rule-execution and a stated rationale, not a recommendation.

---

## Build order (current status — mirrors spec §11)

1. ✅ `prompts/discovery.md` draft — Stages 1–2 prompt (since split per spec §8 and deleted).
2. ✅ `CLAUDE.md` + `implementation_spec.md` — schemas, gate contracts, artifact formats.
3. ✅ Gate 0: `collectors/` + `poc.py` — acceptance criteria passed 2026-07-08 (`outputs/poc_report.md`, C5 sector coverage 98.5%).
4. ✅ `screens/sector_tagger.py` + `screens/quant_filter.py` — produces `stage2_queue.yaml` (smoke-tested end-to-end).
5. ✅ Prompts split per spec §8 (`stage1_sector.md`, `stage2_review.md`); `analysis/sector_dashboard.py` generates `sector_dashboard.yaml`.
6. ✅ `analysis/deep_dive_llm.py` — data-pack assembly + verdict writing (`prompts/valuation.md` DRAFTED — user review pending).
7. ◀ **`allocate/allocator.py` — Stage 4 sizing against the `allocation.yaml` schema.**
8. `pipeline.py` + `.claude/commands/` slash commands.
9. (Later) Phase 2 local dashboard (Streamlit). Build only after the report generator proves itself.
