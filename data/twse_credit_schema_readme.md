# TWSE Credit Schema v1 — PostgreSQL DDL Reference

**File**: `twse_credit_schema_v1.sql`  
**Target DB**: PostgreSQL 14+ (uses generated columns, `jsonb`, `enum` types)

---

## Schema Layout

| Schema | Purpose |
|---|---|
| `twse_raw` | Raw API payload archive — one row per endpoint call; immutable audit trail |
| `twse_core` | Normalised company master data (profile, board, shareholders, news) |
| `twse_fin` | Financial statements for general industry (IS/BS/CF, annual Q4 only) |
| `twse_event` | Dividend history; material news events |
| `twse_market` | Daily stock trade / market cap |
| `credit` | Credit report field dictionary mapping TWSE → form field paths |

---

## Endpoint Coverage

| TWSE Endpoint | Tier | Tables populated |
|---|---|---|
| `t187ap03_L` / `t187ap03_P` | P0 | `twse_core.company_profile` |
| `t187ap02_L` | P0 | `twse_core.major_shareholder` |
| `t187ap11_L` | P0 | `twse_core.board_member` |
| `t187ap04_L` | P0 | `twse_event.material_news` |
| `t21sc03_1` / `t21sc03_2` | P0 | `twse_core.monthly_revenue` |
| `t163sb03_1` | P1 | `twse_fin.income_statement_general` |
| `t163sb04_1` | P1 | `twse_fin.balance_sheet_general` |
| `t163sb05_1` | P1 | `twse_fin.cash_flow_general` |
| `t187ap14_L` | P1 | `twse_event.dividend_history` |
| `t22sr01_1` | P2 | `twse_market.daily_trade` |

**P0** = confirmed, no network restrictions  
**P1** = returns 403 in restricted dev; works in production  
**P2** = individual stock daily data; lower refresh priority

---

## Key Design Choices

### Generated Columns (no application logic for margins/ratios)

`twse_fin.income_statement_general`:
```sql
gross_margin_pct NUMERIC(8,4) GENERATED ALWAYS AS
  (CASE WHEN revenue_k_ntd != 0 THEN ROUND(gross_profit_k_ntd / revenue_k_ntd * 100, 4) END) STORED
```

`twse_fin.balance_sheet_general`:
```sql
current_ratio NUMERIC(8,4) GENERATED ALWAYS AS
  (CASE WHEN current_liabilities_k_ntd > 0 THEN ROUND(current_assets_k_ntd / current_liabilities_k_ntd, 4) END) STORED
```

`twse_fin.cash_flow_general`:
```sql
free_cash_flow_k_ntd NUMERIC(20,2) GENERATED ALWAYS AS (ocf_k_ntd - capex_k_ntd) STORED
```

### Units Convention

All monetary amounts are stored in **NTD thousands** (TWSE native unit).  
Transform to credit report units at mapper layer:
- `/ 1000.0` → NTD millions (used in §7A)
- `/ 1_000_000.0` → NTD billions (used in §5F)

### Long-form Fallback for Other Industry Types

Companies that are not "general industry" (e.g., financial holding, insurance, securities)
use `twse_fin.statement_fact_long` — a `(company_code, fiscal_year, quarter, item_name, value_k_ntd)`
long-form table rather than typed column tables.

---

## Credit Field Dictionary

`credit.credit_report_field_dictionary` seeds 65+ rows that map:

```
(source_table, source_column, twse_unit) → (section_no, field_path, credit_unit, transform_formula)
```

Example:
```
income_statement_general.revenue_k_ntd → §7 / 7A_borrower_financials.income_statement.{FY}.revenue
  transform: value / 1000.0  (NTD thousands → NTD millions)
```

This table drives automated gap analysis: compare populated rows against the full
`FORM_FIELD_REGISTRY` (~484 fields) to compute coverage percentages by section.

---

## Running the Schema

```bash
# Create schemas + tables
psql $DATABASE_URL -f data/twse_credit_schema_v1.sql

# Verify
psql $DATABASE_URL -c "\dn"                          # list schemas
psql $DATABASE_URL -c "\dt twse_fin.*"               # financial statement tables
psql $DATABASE_URL -c "SELECT COUNT(*) FROM credit.credit_report_field_dictionary;"
```

Expected: 13 endpoint catalog rows, 65+ field dictionary rows.

---

## Field Coverage Summary (against 484-field registry)

| Coverage Tier | Count | % |
|---|---|---|
| AUTO_DIRECT (P0 endpoints) | ~45 | ~9% |
| AUTO_DERIVED (computed from P0) | ~12 | ~2% |
| AUTO_DIRECT (P1 endpoints, production) | ~35 | ~7% |
| AUTO_DERIVED (ratios from P1) | ~15 | ~3% |
| REQUIRES_EXTERNAL_SOURCE | ~80 | ~17% |
| MANUAL_REVIEW | ~297 | ~61% |

P1 endpoints (IS/BS/CF) roughly double auto-fill coverage when available.
The remaining 61% requires analyst input, LLM generation from uploaded documents,
or external data sources (vessel valuations, charter rates, facility terms).
