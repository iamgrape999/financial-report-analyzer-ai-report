-- =============================================================================
-- twse_credit_schema_v1.sql
-- Cathay United Bank — TWSE-Backed Credit Analysis Platform
-- PostgreSQL 15+   (uses generated columns, gen_random_uuid, pg_trgm)
-- =============================================================================
-- Architecture: 6 schemas, 31 tables, 3 views
--
--   twse_raw    — raw API ingest, data quality, audit
--   twse_core   — normalised company profile, board, shareholders, monthly revenue, dividends
--   twse_fin    — typed IS / BS / CF tables + long-form + derived metrics
--   twse_event  — material news + NLP risk classification
--   twse_market — daily trade, market metrics
--   credit      — 593-field dictionary, canonical facts, evidence cards, field values
--
-- Endpoint coverage seeded below:
--   P0 (confirmed working): t187ap03_L/P, t187ap02_L, t187ap11_L, t187ap04_L, t21sc03_1/2
--   P1 (restricted in dev, works in prod): t163sb03_1 (IS), t163sb04_1 (BS), t163sb05_1 (CF),
--                                          t187ap14_L (dividends)
--   P2 (market data): t22sr01_1 (daily trade)
-- =============================================================================

\set ON_ERROR_STOP on
BEGIN;

-- Enable uuid extension if not already present
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";   -- for full-text search on company names

-- ─────────────────────────────────────────────────────────────────────────────
-- Schemas
-- ─────────────────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS twse_raw;
CREATE SCHEMA IF NOT EXISTS twse_core;
CREATE SCHEMA IF NOT EXISTS twse_fin;
CREATE SCHEMA IF NOT EXISTS twse_event;
CREATE SCHEMA IF NOT EXISTS twse_market;
CREATE SCHEMA IF NOT EXISTS credit;

-- =============================================================================
-- SCHEMA: twse_raw  (layer 0 — raw ingest + audit)
-- =============================================================================

-- ── twse_raw.api_endpoint_catalog ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS twse_raw.api_endpoint_catalog (
    endpoint_id         SERIAL          PRIMARY KEY,
    endpoint_code       TEXT            NOT NULL UNIQUE,
    endpoint_path       TEXT            NOT NULL,
    full_url            TEXT            NOT NULL,
    description_zh      TEXT,
    description_en      TEXT,
    -- 0 = critical (confirmed P0), 1 = important, 2 = nice-to-have, 3 = experimental
    priority_tier       SMALLINT        CHECK (priority_tier BETWEEN 0 AND 3),
    response_format     TEXT            DEFAULT 'json',
    -- "daily" | "monthly" | "quarterly" | "annual" | "realtime"
    update_frequency    TEXT,
    -- "governance" | "financials" | "events" | "market"
    data_domain         TEXT,
    requires_auth       BOOLEAN         DEFAULT false,
    is_active           BOOLEAN         DEFAULT true,
    -- Chinese column name → credit field path lookup (denormalised for quick reference)
    sample_field_map    JSONB,
    last_verified_at    TIMESTAMPTZ,
    notes               TEXT,
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);

-- ── twse_raw.api_field_catalog ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS twse_raw.api_field_catalog (
    field_id            SERIAL          PRIMARY KEY,
    endpoint_id         INT             NOT NULL REFERENCES twse_raw.api_endpoint_catalog(endpoint_id) ON DELETE CASCADE,
    field_name_zh       TEXT            NOT NULL,
    field_name_en       TEXT,
    data_type           TEXT            CHECK (data_type IN ('string','number','date','boolean','list')),
    example_value       TEXT,
    -- dot-path in the 593-field credit report form
    credit_field_path   TEXT,
    -- how the value is mapped
    mapping_type        TEXT            CHECK (mapping_type IN (
                            'AUTO_DIRECT','AUTO_DERIVED','AUTO_NLP_EXTRACTED',
                            'REQUIRES_EXTERNAL_SOURCE','MANUAL_REVIEW')),
    transform_formula   TEXT,           -- e.g. "value / 1000.0" to convert NTD thousands → millions
    notes               TEXT
);

-- ── twse_raw.api_schema_snapshot ─────────────────────────────────────────────
-- Records the swagger.json at each point in time; hash-based dedup
CREATE TABLE IF NOT EXISTS twse_raw.api_schema_snapshot (
    snapshot_id         SERIAL          PRIMARY KEY,
    snapshotted_at      TIMESTAMPTZ     DEFAULT NOW(),
    swagger_url         TEXT            NOT NULL,
    swagger_json        JSONB           NOT NULL,
    endpoint_count      INT,
    schema_hash         TEXT,           -- SHA-256 hex of swagger_json::text
    UNIQUE (schema_hash)
);

-- ── twse_raw.ingestion_job ───────────────────────────────────────────────────
-- One row per import-twse call, recording what was requested and result
CREATE TABLE IF NOT EXISTS twse_raw.ingestion_job (
    job_id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    initiated_by        TEXT            NOT NULL,
    stock_code          TEXT            NOT NULL,
    endpoints_requested TEXT[]          NOT NULL,
    -- "pending" | "running" | "completed" | "failed" | "partial"
    status              TEXT            NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','running','completed','failed','partial')),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    total_rows_fetched  INT             DEFAULT 0,
    fields_written      INT             DEFAULT 0,
    fields_skipped      INT             DEFAULT 0,
    apply_mode          TEXT,           -- "only_empty" | "overwrite"
    error_message       TEXT,
    -- FK to application-layer credit report (UUID as text to avoid cross-DB FK)
    report_id           TEXT,
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_job_stock      ON twse_raw.ingestion_job(stock_code);
CREATE INDEX IF NOT EXISTS idx_job_report     ON twse_raw.ingestion_job(report_id);
CREATE INDEX IF NOT EXISTS idx_job_status     ON twse_raw.ingestion_job(status, created_at DESC);

-- ── twse_raw.raw_response_batch ──────────────────────────────────────────────
-- Full JSON response per endpoint per job; enables re-processing without re-fetching
CREATE TABLE IF NOT EXISTS twse_raw.raw_response_batch (
    batch_id            UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id              UUID            NOT NULL REFERENCES twse_raw.ingestion_job(job_id) ON DELETE CASCADE,
    endpoint_code       TEXT            NOT NULL,
    http_status         SMALLINT,
    response_json       JSONB,
    -- SHA-256 of response_json::text — duplicate API responses skip re-processing
    response_hash       TEXT,
    fetched_at          TIMESTAMPTZ     DEFAULT NOW(),
    rows_in_batch       INT             DEFAULT 0,
    UNIQUE (job_id, endpoint_code)
);
CREATE INDEX IF NOT EXISTS idx_batch_job      ON twse_raw.raw_response_batch(job_id);
CREATE INDEX IF NOT EXISTS idx_batch_endpoint ON twse_raw.raw_response_batch(endpoint_code, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_batch_hash     ON twse_raw.raw_response_batch(response_hash);

-- ── twse_raw.raw_record ──────────────────────────────────────────────────────
-- Individual records extracted from batch (one row per company per endpoint)
CREATE TABLE IF NOT EXISTS twse_raw.raw_record (
    record_id           UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id            UUID            NOT NULL REFERENCES twse_raw.raw_response_batch(batch_id) ON DELETE CASCADE,
    endpoint_code       TEXT            NOT NULL,
    stock_code          TEXT,
    record_json         JSONB           NOT NULL,
    is_processed        BOOLEAN         DEFAULT false,
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_record_stock     ON twse_raw.raw_record(stock_code);
CREATE INDEX IF NOT EXISTS idx_record_endpoint  ON twse_raw.raw_record(endpoint_code);
CREATE INDEX IF NOT EXISTS idx_record_processed ON twse_raw.raw_record(is_processed) WHERE is_processed = false;
CREATE INDEX IF NOT EXISTS idx_record_batch     ON twse_raw.raw_record(batch_id);

-- ── twse_raw.data_quality_issue ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS twse_raw.data_quality_issue (
    issue_id            SERIAL          PRIMARY KEY,
    batch_id            UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    record_id           UUID            REFERENCES twse_raw.raw_record(record_id),
    stock_code          TEXT,
    endpoint_code       TEXT,
    issue_type          TEXT            NOT NULL
                            CHECK (issue_type IN (
                                'missing_field','type_mismatch','out_of_range',
                                'duplicate','stale_data','unexpected_null')),
    field_name          TEXT,
    expected_value      TEXT,
    actual_value        TEXT,
    severity            TEXT            NOT NULL DEFAULT 'warning'
                            CHECK (severity IN ('info','warning','error','critical')),
    resolved            BOOLEAN         DEFAULT false,
    resolved_by         TEXT,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dq_stock    ON twse_raw.data_quality_issue(stock_code);
CREATE INDEX IF NOT EXISTS idx_dq_severity ON twse_raw.data_quality_issue(severity, resolved);

-- =============================================================================
-- SCHEMA: twse_core  (layer 1 — normalised company data)
-- =============================================================================

-- ── twse_core.company_profile ─────────────────────────────────────────────────
-- From t187ap03_L (listed) / t187ap03_P (OTC)
CREATE TABLE IF NOT EXISTS twse_core.company_profile (
    id                          SERIAL      PRIMARY KEY,
    stock_code                  TEXT        NOT NULL,
    source_batch_id             UUID        REFERENCES twse_raw.raw_response_batch(batch_id),
    data_date                   DATE        NOT NULL,
    is_current                  BOOLEAN     DEFAULT true,

    -- Identity
    company_name_zh             TEXT,
    company_name_abbrev_zh      TEXT,
    company_name_en             TEXT,
    company_name_abbrev_en      TEXT,
    isin_code                   TEXT,
    tax_id                      TEXT,

    -- Market
    market_type                 TEXT        CHECK (market_type IN ('上市','上櫃','興櫃')),
    listing_date                DATE,
    industry_zh                 TEXT,

    -- Incorporation
    incorporation_country       TEXT,       -- blank → Taiwan
    incorporation_date          DATE,

    -- Capital
    paid_in_capital_ntd         BIGINT,
    shares_outstanding          BIGINT,
    par_value_ntd               NUMERIC(10,2),

    -- Governance
    chairman                    TEXT,
    ceo                         TEXT,
    spokesperson                TEXT,
    spokesperson_title          TEXT,

    -- Business
    primary_business            TEXT,
    financial_report_type       TEXT        CHECK (financial_report_type IN ('合併','個別')),

    -- Auditor
    auditor_firm                TEXT,
    auditor1                    TEXT,
    auditor2                    TEXT,

    -- Contact
    phone                       TEXT,
    fax                         TEXT,
    address                     TEXT,
    email                       TEXT,
    website                     TEXT,

    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (stock_code, data_date)
);
CREATE INDEX IF NOT EXISTS idx_cp_stock         ON twse_core.company_profile(stock_code);
CREATE INDEX IF NOT EXISTS idx_cp_current       ON twse_core.company_profile(stock_code, is_current) WHERE is_current = true;
CREATE INDEX IF NOT EXISTS idx_cp_name_trgm     ON twse_core.company_profile USING gin(company_name_zh gin_trgm_ops);

-- ── twse_core.board_member ───────────────────────────────────────────────────
-- From t187ap11_L
CREATE TABLE IF NOT EXISTS twse_core.board_member (
    id                  SERIAL      PRIMARY KEY,
    stock_code          TEXT        NOT NULL,
    source_batch_id     UUID        REFERENCES twse_raw.raw_response_batch(batch_id),
    data_date           DATE        NOT NULL,
    title               TEXT,
    name                TEXT,
    shares_current      BIGINT,
    pledged_shares      BIGINT,
    pledge_pct          NUMERIC(8,4),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_board_stock ON twse_core.board_member(stock_code, data_date);

-- ── twse_core.major_shareholder ──────────────────────────────────────────────
-- From t187ap02_L  (TWSE only discloses name for >10% holders, not exact %)
CREATE TABLE IF NOT EXISTS twse_core.major_shareholder (
    id                  SERIAL      PRIMARY KEY,
    stock_code          TEXT        NOT NULL,
    source_batch_id     UUID        REFERENCES twse_raw.raw_response_batch(batch_id),
    data_date           DATE        NOT NULL,
    shareholder_name    TEXT        NOT NULL,
    stake_threshold     TEXT        DEFAULT '>10%',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sh_stock ON twse_core.major_shareholder(stock_code, data_date);

-- ── twse_core.monthly_revenue ────────────────────────────────────────────────
-- From t21sc03_1 (listed) / t21sc03_2 (OTC)
CREATE TABLE IF NOT EXISTS twse_core.monthly_revenue (
    id                      SERIAL          PRIMARY KEY,
    stock_code              TEXT            NOT NULL,
    source_batch_id         UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    gregorian_year          SMALLINT        NOT NULL,
    gregorian_month         SMALLINT        NOT NULL CHECK (gregorian_month BETWEEN 1 AND 12),
    roc_year_month          TEXT,

    -- All in NT$ thousands (千元)
    revenue_k_ntd           NUMERIC(20,2),
    revenue_mom_pct         NUMERIC(10,4),
    revenue_yoy_pct         NUMERIC(10,4),
    ytd_revenue_k_ntd       NUMERIC(20,2),
    ytd_yoy_pct             NUMERIC(10,4),

    created_at              TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (stock_code, gregorian_year, gregorian_month)
);
CREATE INDEX IF NOT EXISTS idx_rev_stock_year ON twse_core.monthly_revenue(stock_code, gregorian_year);

-- ── twse_core.dividend_distribution ─────────────────────────────────────────
-- From t187ap14_L
CREATE TABLE IF NOT EXISTS twse_core.dividend_distribution (
    id                          SERIAL          PRIMARY KEY,
    stock_code                  TEXT            NOT NULL,
    source_batch_id             UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    fiscal_year                 SMALLINT        NOT NULL,
    board_resolution_date       DATE,
    agm_date                    DATE,
    distributable_earnings_ntd  BIGINT,
    cash_dividend_per_share     NUMERIC(10,4),
    stock_dividend_per_share    NUMERIC(10,4),
    resolution_status           TEXT,
    notes                       TEXT,
    created_at                  TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (stock_code, fiscal_year)
);

-- =============================================================================
-- SCHEMA: twse_fin  (layer 2 — typed financial statements + metrics)
-- =============================================================================

-- ── twse_fin.income_statement_general ───────────────────────────────────────
-- From t163sb03_1: 綜合損益表（一般業）
-- Amounts in NT$ thousands; generated columns compute margins automatically.
CREATE TABLE IF NOT EXISTS twse_fin.income_statement_general (
    id                              SERIAL          PRIMARY KEY,
    stock_code                      TEXT            NOT NULL,
    source_batch_id                 UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    fiscal_year                     SMALLINT        NOT NULL,
    -- "Annual" | "Q1" | "Q2" | "Q3"  (TWSE only publishes Q1/Q3 for semi-annual)
    period_type                     TEXT            NOT NULL DEFAULT 'Annual'
                                        CHECK (period_type IN ('Annual','Q1','Q2','Q3')),

    -- NT$ thousands (千元)
    revenue_k_ntd                   NUMERIC(20,2),  -- 營業收入
    cogs_k_ntd                      NUMERIC(20,2),  -- 營業成本
    gross_profit_k_ntd              NUMERIC(20,2),  -- 營業毛利(毛損)
    operating_expense_k_ntd         NUMERIC(20,2),  -- 營業費用合計
    operating_income_k_ntd          NUMERIC(20,2),  -- 營業利益(損失)  ≈ EBIT
    non_op_income_k_ntd             NUMERIC(20,2),  -- 營業外收入及支出
    pre_tax_income_k_ntd            NUMERIC(20,2),  -- 稅前淨利(淨損)
    income_tax_k_ntd                NUMERIC(20,2),  -- 所得稅費用(利益)
    net_income_k_ntd                NUMERIC(20,2),  -- 本期淨利(淨損)
    comprehensive_income_k_ntd      NUMERIC(20,2),  -- 綜合損益總額
    basic_eps                       NUMERIC(10,4),  -- 基本每股盈餘(元)
    diluted_eps                     NUMERIC(10,4),  -- 稀釋每股盈餘(元)

    -- Margins — auto-computed
    gross_margin_pct                NUMERIC(10,4)   GENERATED ALWAYS AS (
                                        CASE WHEN revenue_k_ntd > 0
                                        THEN ROUND(gross_profit_k_ntd / revenue_k_ntd * 100, 4)
                                        END) STORED,
    operating_margin_pct            NUMERIC(10,4)   GENERATED ALWAYS AS (
                                        CASE WHEN revenue_k_ntd > 0
                                        THEN ROUND(operating_income_k_ntd / revenue_k_ntd * 100, 4)
                                        END) STORED,
    net_margin_pct                  NUMERIC(10,4)   GENERATED ALWAYS AS (
                                        CASE WHEN revenue_k_ntd > 0
                                        THEN ROUND(net_income_k_ntd / revenue_k_ntd * 100, 4)
                                        END) STORED,

    created_at                      TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (stock_code, fiscal_year, period_type)
);
CREATE INDEX IF NOT EXISTS idx_is_stock_year ON twse_fin.income_statement_general(stock_code, fiscal_year);

-- ── twse_fin.balance_sheet_general ──────────────────────────────────────────
-- From t163sb04_1: 資產負債表（一般業）
CREATE TABLE IF NOT EXISTS twse_fin.balance_sheet_general (
    id                              SERIAL          PRIMARY KEY,
    stock_code                      TEXT            NOT NULL,
    source_batch_id                 UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    fiscal_year                     SMALLINT        NOT NULL,
    period_type                     TEXT            NOT NULL DEFAULT 'Annual'
                                        CHECK (period_type IN ('Annual','Q1','Q2','Q3')),
    as_of_date                      DATE,           -- 報告日期 (e.g. 2024-12-31)

    -- NT$ thousands
    current_assets_k_ntd            NUMERIC(20,2),  -- 流動資產
    noncurrent_assets_k_ntd         NUMERIC(20,2),  -- 非流動資產
    total_assets_k_ntd              NUMERIC(20,2),  -- 資產總計
    current_liabilities_k_ntd       NUMERIC(20,2),  -- 流動負債
    noncurrent_liabilities_k_ntd    NUMERIC(20,2),  -- 非流動負債
    total_liabilities_k_ntd         NUMERIC(20,2),  -- 負債總計
    share_capital_k_ntd             NUMERIC(20,2),  -- 股本
    capital_surplus_k_ntd           NUMERIC(20,2),  -- 資本公積
    retained_earnings_k_ntd         NUMERIC(20,2),  -- 保留盈餘(或累積虧損)
    other_equity_k_ntd              NUMERIC(20,2),  -- 其他權益
    total_equity_k_ntd              NUMERIC(20,2),  -- 權益總計
    book_value_per_share            NUMERIC(10,4),  -- 每股參考淨值(元)
    -- Cash on balance sheet (populated via JOIN with CF or separately if available)
    ending_cash_k_ntd               NUMERIC(20,2),

    -- Ratios — auto-computed
    current_ratio                   NUMERIC(10,4)   GENERATED ALWAYS AS (
                                        CASE WHEN current_liabilities_k_ntd > 0
                                        THEN ROUND(current_assets_k_ntd / current_liabilities_k_ntd, 4)
                                        END) STORED,
    debt_ratio_pct                  NUMERIC(10,4)   GENERATED ALWAYS AS (
                                        CASE WHEN total_assets_k_ntd > 0
                                        THEN ROUND(total_liabilities_k_ntd / total_assets_k_ntd * 100, 4)
                                        END) STORED,
    equity_ratio_pct                NUMERIC(10,4)   GENERATED ALWAYS AS (
                                        CASE WHEN total_assets_k_ntd > 0
                                        THEN ROUND(total_equity_k_ntd / total_assets_k_ntd * 100, 4)
                                        END) STORED,

    created_at                      TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (stock_code, fiscal_year, period_type)
);
CREATE INDEX IF NOT EXISTS idx_bs_stock_year ON twse_fin.balance_sheet_general(stock_code, fiscal_year);

-- ── twse_fin.cash_flow_general ───────────────────────────────────────────────
-- From t163sb05_1: 現金流量表（一般業）
CREATE TABLE IF NOT EXISTS twse_fin.cash_flow_general (
    id                          SERIAL          PRIMARY KEY,
    stock_code                  TEXT            NOT NULL,
    source_batch_id             UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    fiscal_year                 SMALLINT        NOT NULL,
    period_type                 TEXT            NOT NULL DEFAULT 'Annual'
                                    CHECK (period_type IN ('Annual','Q1','Q2','Q3')),

    -- NT$ thousands
    cfo_k_ntd                   NUMERIC(20,2),  -- 營業活動之淨現金流入(出)
    cfi_k_ntd                   NUMERIC(20,2),  -- 投資活動之淨現金流入(出)
    cff_k_ntd                   NUMERIC(20,2),  -- 籌資活動之淨現金流入(出)
    -- |capex|: 取得不動產廠房及設備 (stored as positive value; outflow sign removed by ETL)
    capex_k_ntd                 NUMERIC(20,2),
    beginning_cash_k_ntd        NUMERIC(20,2),  -- 期初現金及約當現金
    ending_cash_k_ntd           NUMERIC(20,2),  -- 期末現金及約當現金

    -- FCF = CFO - CAPEX (generated)
    free_cash_flow_k_ntd        NUMERIC(20,2)   GENERATED ALWAYS AS (
                                    CASE WHEN cfo_k_ntd IS NOT NULL AND capex_k_ntd IS NOT NULL
                                    THEN cfo_k_ntd - capex_k_ntd
                                    END) STORED,

    created_at                  TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (stock_code, fiscal_year, period_type)
);
CREATE INDEX IF NOT EXISTS idx_cf_stock_year ON twse_fin.cash_flow_general(stock_code, fiscal_year);

-- ── twse_fin.statement_fact_long ─────────────────────────────────────────────
-- Long-form overflow for: financial/insurance/securities/holding industry variants,
-- and any IS/BS/CF line item not captured in the typed tables above.
CREATE TABLE IF NOT EXISTS twse_fin.statement_fact_long (
    id                  SERIAL          PRIMARY KEY,
    stock_code          TEXT            NOT NULL,
    source_batch_id     UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    fiscal_year         SMALLINT        NOT NULL,
    period_type         TEXT,
    -- "general" | "financial" | "insurance" | "securities" | "holding" | "other"
    industry_type       TEXT,
    -- "IS" | "BS" | "CF" | "equity"
    statement_type      TEXT            NOT NULL CHECK (statement_type IN ('IS','BS','CF','equity')),
    account_code        TEXT,
    account_name_zh     TEXT            NOT NULL,
    account_name_en     TEXT,
    amount_k_ntd        NUMERIC(22,4),
    unit                TEXT            DEFAULT 'NT$ thousands',
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fact_long_stock ON twse_fin.statement_fact_long(stock_code, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_fact_long_acct  ON twse_fin.statement_fact_long USING gin(account_name_zh gin_trgm_ops);

-- ── twse_fin.derived_financial_metric ────────────────────────────────────────
-- Cross-statement derived ratios — populated by calculation_engine after IS+BS+CF import
CREATE TABLE IF NOT EXISTS twse_fin.derived_financial_metric (
    id                          SERIAL          PRIMARY KEY,
    stock_code                  TEXT            NOT NULL,
    fiscal_year                 SMALLINT        NOT NULL,

    -- Profitability (NTD thousands)
    ebitda_k_ntd                NUMERIC(20,2),  -- Operating Income + D&A  (D&A from CF adjustments)
    ebitda_margin_pct           NUMERIC(10,4),  -- EBITDA / Revenue × 100
    roe_pct                     NUMERIC(10,4),  -- Net Income / Avg Equity × 100
    roa_pct                     NUMERIC(10,4),  -- Net Income / Avg Assets × 100

    -- Leverage
    net_debt_k_ntd              NUMERIC(20,2),  -- Total Debt − Cash
    net_debt_ebitda             NUMERIC(10,4),  -- Net Debt / EBITDA
    interest_coverage           NUMERIC(10,4),  -- EBIT / Interest Expense
    debt_service_coverage       NUMERIC(10,4),  -- (CFO) / Debt Service

    -- Liquidity
    current_ratio               NUMERIC(10,4),
    quick_ratio                 NUMERIC(10,4),
    cfo_to_debt_pct             NUMERIC(10,4),  -- CFO / Total Debt × 100

    -- Capital structure
    total_debt_k_ntd            NUMERIC(20,2),  -- Short + Long term borrowings
    debt_equity_ratio           NUMERIC(10,4),  -- Total Liabilities / Total Equity
    equity_multiplier           NUMERIC(10,4),  -- Total Assets / Total Equity

    -- Per-share
    book_value_per_share        NUMERIC(10,4),
    eps_basic                   NUMERIC(10,4),

    -- Revenue trend
    revenue_yoy_pct             NUMERIC(10,4),
    revenue_3yr_cagr_pct        NUMERIC(10,4),

    computed_at                 TIMESTAMPTZ     DEFAULT NOW(),
    -- Source row IDs for audit trail
    source_is_id                INT             REFERENCES twse_fin.income_statement_general(id),
    source_bs_id                INT             REFERENCES twse_fin.balance_sheet_general(id),
    source_cf_id                INT             REFERENCES twse_fin.cash_flow_general(id),

    UNIQUE (stock_code, fiscal_year)
);
CREATE INDEX IF NOT EXISTS idx_dfm_stock ON twse_fin.derived_financial_metric(stock_code, fiscal_year);

-- ── View: credit financial ratios (all in NTD millions, ready for credit report) ─
CREATE OR REPLACE VIEW twse_fin.v_credit_financial_ratios_general AS
SELECT
    is_.stock_code,
    is_.fiscal_year,
    is_.period_type,
    ROUND(is_.revenue_k_ntd         / 1000.0, 1)   AS revenue_m_ntd,
    ROUND(is_.gross_profit_k_ntd    / 1000.0, 1)   AS gross_profit_m_ntd,
    ROUND(is_.operating_income_k_ntd/ 1000.0, 1)   AS ebit_m_ntd,
    ROUND(is_.net_income_k_ntd      / 1000.0, 1)   AS net_income_m_ntd,
    is_.gross_margin_pct,
    is_.operating_margin_pct,
    is_.net_margin_pct,
    ROUND(bs.total_assets_k_ntd     / 1000.0, 1)   AS total_assets_m_ntd,
    ROUND(bs.total_liabilities_k_ntd/ 1000.0, 1)   AS total_liabilities_m_ntd,
    ROUND(bs.total_equity_k_ntd     / 1000.0, 1)   AS total_equity_m_ntd,
    bs.current_ratio,
    bs.debt_ratio_pct,
    bs.equity_ratio_pct,
    ROUND(cf.cfo_k_ntd              / 1000.0, 1)   AS cfo_m_ntd,
    ROUND(cf.free_cash_flow_k_ntd   / 1000.0, 1)   AS fcf_m_ntd,
    ROUND(dm.ebitda_k_ntd           / 1000.0, 1)   AS ebitda_m_ntd,
    dm.ebitda_margin_pct,
    dm.net_debt_ebitda,
    dm.interest_coverage,
    dm.roe_pct,
    dm.roa_pct
FROM  twse_fin.income_statement_general  is_
LEFT  JOIN twse_fin.balance_sheet_general bs
       ON  bs.stock_code  = is_.stock_code
      AND  bs.fiscal_year = is_.fiscal_year
      AND  bs.period_type = is_.period_type
LEFT  JOIN twse_fin.cash_flow_general     cf
       ON  cf.stock_code  = is_.stock_code
      AND  cf.fiscal_year = is_.fiscal_year
      AND  cf.period_type = is_.period_type
LEFT  JOIN twse_fin.derived_financial_metric dm
       ON  dm.stock_code  = is_.stock_code
      AND  dm.fiscal_year = is_.fiscal_year;

-- =============================================================================
-- SCHEMA: twse_event  (material news + risk classification)
-- =============================================================================

-- ── twse_event.daily_material_information ────────────────────────────────────
-- From t187ap04_L: 上市公司每日重大訊息
CREATE TABLE IF NOT EXISTS twse_event.daily_material_information (
    id                      SERIAL          PRIMARY KEY,
    stock_code              TEXT            NOT NULL,
    source_batch_id         UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    publication_date        DATE,           -- 出表日期
    statement_date          DATE,           -- 發言日期
    statement_time          TIME,           -- 發言時間
    fact_date               DATE,           -- 事實發生日
    subject                 TEXT,           -- 主旨
    applicable_clause       TEXT,           -- 符合條款
    description             TEXT,           -- 說明
    created_at              TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_event_stock ON twse_event.daily_material_information(stock_code);
CREATE INDEX IF NOT EXISTS idx_event_date  ON twse_event.daily_material_information(publication_date DESC);
CREATE INDEX IF NOT EXISTS idx_event_subj  ON twse_event.daily_material_information USING gin(subject gin_trgm_ops);

-- ── twse_event.event_risk_classification ─────────────────────────────────────
-- Rule-based or NLP classification applied after ingestion
CREATE TABLE IF NOT EXISTS twse_event.event_risk_classification (
    id                      SERIAL          PRIMARY KEY,
    event_id                INT             NOT NULL REFERENCES twse_event.daily_material_information(id) ON DELETE CASCADE,
    stock_code              TEXT            NOT NULL,
    -- "financial" | "litigation" | "guarantee" | "default" | "bankruptcy" | "general" | ...
    risk_category           TEXT            NOT NULL,
    -- "rule_based" | "nlp" | "analyst_manual"
    classification_method   TEXT            CHECK (classification_method IN ('rule_based','nlp','analyst_manual')),
    confidence_score        NUMERIC(5,4),
    analyst_verified        BOOLEAN         DEFAULT false,
    analyst_email           TEXT,
    created_at              TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_risk_event  ON twse_event.event_risk_classification(event_id);
CREATE INDEX IF NOT EXISTS idx_risk_stock  ON twse_event.event_risk_classification(stock_code, risk_category);

-- =============================================================================
-- SCHEMA: twse_market  (stock price + market metrics)
-- =============================================================================

-- ── twse_market.daily_trade ──────────────────────────────────────────────────
-- From t22sr01_1: 個股日成交資訊
CREATE TABLE IF NOT EXISTS twse_market.daily_trade (
    id                      SERIAL          PRIMARY KEY,
    stock_code              TEXT            NOT NULL,
    source_batch_id         UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    trade_date              DATE            NOT NULL,
    open_price              NUMERIC(12,4),
    high_price              NUMERIC(12,4),
    low_price               NUMERIC(12,4),
    close_price             NUMERIC(12,4),
    volume_shares           BIGINT,
    volume_ntd              NUMERIC(20,2),
    shares_outstanding      BIGINT,
    -- Close × shares (auto)
    market_cap_ntd          NUMERIC(22,2)   GENERATED ALWAYS AS
                                (close_price * shares_outstanding) STORED,
    created_at              TIMESTAMPTZ     DEFAULT NOW(),
    UNIQUE (stock_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_trade_stock ON twse_market.daily_trade(stock_code, trade_date DESC);

-- ── twse_market.security_market_metric ───────────────────────────────────────
-- Aggregated metrics computed from daily_trade (e.g. 52-week high/low, P/B)
CREATE TABLE IF NOT EXISTS twse_market.security_market_metric (
    id                          SERIAL      PRIMARY KEY,
    stock_code                  TEXT        NOT NULL,
    computed_as_of              DATE        NOT NULL,
    price_52w_high              NUMERIC(12,4),
    price_52w_low               NUMERIC(12,4),
    price_pct_vs_52w_high       NUMERIC(8,4),   -- % below 52-week high
    avg_daily_vol_30d           BIGINT,
    pb_ratio                    NUMERIC(10,4),  -- Price / Book Value per Share
    pe_ratio                    NUMERIC(10,4),  -- Price / Trailing-12m EPS
    market_cap_ntd_bn           NUMERIC(14,4),  -- NTD billions
    is_low_liquidity            BOOLEAN,        -- avg_daily_vol_30d < threshold
    consecutive_down_days       SMALLINT,       -- streak of daily price declines
    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (stock_code, computed_as_of)
);

-- =============================================================================
-- SCHEMA: credit  (593-field mapping + canonical facts + evidence + audit)
-- =============================================================================

-- ── credit.credit_report_field_dictionary ────────────────────────────────────
-- Reference table: one row per field path in the credit report form (593 rows)
CREATE TABLE IF NOT EXISTS credit.credit_report_field_dictionary (
    field_id            SERIAL          PRIMARY KEY,
    section_no          SMALLINT        NOT NULL,
    field_path          TEXT            NOT NULL UNIQUE,    -- dot-notation path
    label_en            TEXT,
    label_zh            TEXT,
    data_type           TEXT            CHECK (data_type IN ('number','string','boolean','list','date','object')),
    is_required         BOOLEAN         DEFAULT false,
    -- AUTO_DIRECT: filled verbatim from TWSE field
    -- AUTO_DERIVED: computed from one or more TWSE fields
    -- AUTO_NLP_EXTRACTED: extracted via keyword/NLP from text fields
    -- REQUIRES_EXTERNAL_SOURCE: cannot be filled from TWSE (e.g. vessel valuation, LTV)
    -- MANUAL_REVIEW: analyst must fill (e.g. internal credit rating, facility terms)
    source_tier         TEXT            CHECK (source_tier IN (
                            'AUTO_DIRECT','AUTO_DERIVED','AUTO_NLP_EXTRACTED',
                            'REQUIRES_EXTERNAL_SOURCE','MANUAL_REVIEW')),
    twse_endpoint_code  TEXT,           -- which endpoint provides this value
    twse_field_zh       TEXT,           -- exact Chinese column name from API
    transform_formula   TEXT,           -- e.g. "/ 1000.0" to convert thousands → millions
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_dict_section    ON credit.credit_report_field_dictionary(section_no);
CREATE INDEX IF NOT EXISTS idx_dict_tier       ON credit.credit_report_field_dictionary(source_tier);
CREATE INDEX IF NOT EXISTS idx_dict_endpoint   ON credit.credit_report_field_dictionary(twse_endpoint_code);

-- ── credit.twse_to_credit_field_mapping ──────────────────────────────────────
-- Explicit bidirectional mapping: TWSE column → credit form field
CREATE TABLE IF NOT EXISTS credit.twse_to_credit_field_mapping (
    mapping_id          SERIAL          PRIMARY KEY,
    twse_endpoint_code  TEXT            NOT NULL,
    twse_field_zh       TEXT            NOT NULL,
    credit_field_path   TEXT            NOT NULL
                            REFERENCES credit.credit_report_field_dictionary(field_path),
    transform_formula   TEXT,
    mapping_type        TEXT            CHECK (mapping_type IN ('AUTO_DIRECT','AUTO_DERIVED','MANUAL_REVIEW')),
    is_active           BOOLEAN         DEFAULT true,
    notes               TEXT,
    UNIQUE (twse_endpoint_code, twse_field_zh, credit_field_path)
);

-- ── credit.canonical_fact ────────────────────────────────────────────────────
-- Normalised facts from TWSE — source of truth for field suggestions
CREATE TABLE IF NOT EXISTS credit.canonical_fact (
    fact_id                 UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    stock_code              TEXT            NOT NULL,
    report_id               TEXT,           -- linked credit report (app-level)
    metric_name             TEXT            NOT NULL,
    entity                  TEXT            NOT NULL,   -- "BORROWER","GUARANTOR","MARKET"
    period                  TEXT            NOT NULL,   -- "FY2024","Q3-2024","CURRENT"
    fiscal_year             SMALLINT,

    -- Value — exactly one populated
    value_numeric           NUMERIC(22,4),
    value_text              TEXT,
    value_bool              BOOLEAN,

    -- Provenance
    source_type             TEXT            CHECK (source_type IN (
                                'twse_financial_statement','twse_monthly_revenue',
                                'twse_company_profile','twse_material_news',
                                'twse_dividend','twse_market_price',
                                'analyst_manual','derived_calculation')),
    source_batch_id         UUID            REFERENCES twse_raw.raw_response_batch(batch_id),
    source_table            TEXT,           -- e.g. "twse_fin.income_statement_general"
    source_row_id           INT,

    -- Quality
    state                   TEXT            NOT NULL DEFAULT 'extracted'
                                CHECK (state IN ('extracted','validated','conflicted','approved','overridden')),
    -- 1=analyst_manual (highest), 2=manual_override, 3=auto_extracted
    source_priority         SMALLINT        DEFAULT 3,
    confidence_score        NUMERIC(5,4),

    -- Formatting for UI display
    currency                TEXT,
    unit                    TEXT,
    display                 TEXT,

    created_at              TIMESTAMPTZ     DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     DEFAULT NOW(),

    UNIQUE (stock_code, metric_name, entity, period, source_type)
);
CREATE INDEX IF NOT EXISTS idx_cf_stock_metric  ON credit.canonical_fact(stock_code, metric_name, period);
CREATE INDEX IF NOT EXISTS idx_cf_report        ON credit.canonical_fact(report_id);
CREATE INDEX IF NOT EXISTS idx_cf_state         ON credit.canonical_fact(state) WHERE state = 'conflicted';

-- ── credit.evidence_card ─────────────────────────────────────────────────────
-- Per-field suggestion surfaced to analyst for review (field-suggestion endpoint)
CREATE TABLE IF NOT EXISTS credit.evidence_card (
    card_id                 UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id               TEXT            NOT NULL,
    section_no              SMALLINT        NOT NULL,
    field_path              TEXT            NOT NULL,
    fact_id                 UUID            REFERENCES credit.canonical_fact(fact_id),
    suggested_value         JSONB,          -- value from canonical_fact (any type)
    current_value           JSONB,          -- what is currently in SectionInput
    action                  TEXT            CHECK (action IN ('fill','update','no_change','conflict')),
    analyst_confirmed       BOOLEAN         DEFAULT false,
    analyst_email           TEXT,
    confirmed_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ec_report_sec ON credit.evidence_card(report_id, section_no);

-- ── credit.credit_report_field_value ─────────────────────────────────────────
-- Actual field values in submitted credit reports (versioned)
CREATE TABLE IF NOT EXISTS credit.credit_report_field_value (
    value_id            UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id           TEXT            NOT NULL,
    section_no          SMALLINT        NOT NULL,
    field_path          TEXT            NOT NULL,
    value_json          JSONB,
    fill_method         TEXT            CHECK (fill_method IN (
                            'twse_auto','analyst_manual','ai_generated','calculated')),
    fill_source         TEXT,           -- endpoint code or "analyst"
    fact_id             UUID            REFERENCES credit.canonical_fact(fact_id),
    version             INT             DEFAULT 1,
    is_current          BOOLEAN         DEFAULT true,
    created_at          TIMESTAMPTZ     DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_crfv_report_sec  ON credit.credit_report_field_value(report_id, section_no);
CREATE INDEX IF NOT EXISTS idx_crfv_current     ON credit.credit_report_field_value(report_id, is_current) WHERE is_current = true;

-- ── credit.report_period_policy ──────────────────────────────────────────────
-- Guards against mixing fiscal years, quarterly vs annual, or stale prices
CREATE TABLE IF NOT EXISTS credit.report_period_policy (
    policy_id               SERIAL      PRIMARY KEY,
    report_id               TEXT        NOT NULL UNIQUE,
    primary_fiscal_year     SMALLINT,
    -- "Annual" | "Q1" | "Q2" | "Q3"
    financial_period_type   TEXT        DEFAULT 'Annual',
    revenue_cutoff_year     SMALLINT,   -- latest full year for monthly rev aggregation
    stock_price_as_of       DATE,       -- valuation date for market cap metrics
    allow_partial_year      BOOLEAN     DEFAULT false,
    policy_notes            TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ── credit.report_generation_audit ──────────────────────────────────────────
-- Full audit trail of every TWSE import, AI generation, and analyst edit
CREATE TABLE IF NOT EXISTS credit.report_generation_audit (
    audit_id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id               TEXT        NOT NULL,
    section_no              SMALLINT,
    -- "twse_import" | "field_fill" | "ai_generate" | "analyst_edit" | "bulk_suggest_apply"
    event_type              TEXT        NOT NULL,
    triggered_by            TEXT,       -- user email or "system"
    fields_written          INT         DEFAULT 0,
    fields_skipped          INT         DEFAULT 0,
    twse_stock_code         TEXT,
    endpoints_called        TEXT[],
    apply_mode              TEXT,
    duration_ms             INT,
    error_message           TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_report ON credit.report_generation_audit(report_id);
CREATE INDEX IF NOT EXISTS idx_audit_event  ON credit.report_generation_audit(event_type, created_at DESC);

-- ── View: per-report field fill-rate dashboard ────────────────────────────────
CREATE OR REPLACE VIEW credit.v_field_coverage_by_report AS
SELECT
    fv.report_id,
    fv.section_no,
    COUNT(DISTINCT fv.field_path)                                   AS fields_filled,
    COUNT(DISTINCT d.field_path)                                    AS fields_total,
    ROUND(COUNT(DISTINCT fv.field_path)::numeric /
          NULLIF(COUNT(DISTINCT d.field_path), 0) * 100, 1)        AS fill_pct,
    COUNT(DISTINCT fv.field_path) FILTER (
        WHERE fv.fill_method = 'twse_auto')                         AS twse_auto_filled,
    COUNT(DISTINCT fv.field_path) FILTER (
        WHERE fv.fill_method = 'analyst_manual')                    AS manually_filled,
    COUNT(DISTINCT fv.field_path) FILTER (
        WHERE fv.fill_method = 'ai_generated')                      AS ai_generated
FROM  credit.credit_report_field_value fv
JOIN  credit.credit_report_field_dictionary d USING (section_no)
WHERE fv.is_current = true
GROUP BY fv.report_id, fv.section_no;

-- ── View: TWSE auto-fill coverage summary ────────────────────────────────────
CREATE OR REPLACE VIEW credit.v_twse_coverage_summary AS
SELECT
    source_tier,
    twse_endpoint_code,
    COUNT(*)                        AS field_count,
    ROUND(COUNT(*)::numeric /
          SUM(COUNT(*)) OVER () * 100, 1) AS pct_of_total
FROM credit.credit_report_field_dictionary
GROUP BY source_tier, twse_endpoint_code
ORDER BY source_tier, field_count DESC;

-- =============================================================================
-- SEED DATA
-- =============================================================================

-- ── Endpoint catalog ─────────────────────────────────────────────────────────
INSERT INTO twse_raw.api_endpoint_catalog
    (endpoint_code, endpoint_path, full_url,
     description_zh, description_en,
     priority_tier, update_frequency, data_domain, requires_auth, is_active)
VALUES
  -- P0: confirmed working in all environments
  ('t187ap03_L', '/v1/opendata/t187ap03_L',
   'https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
   '上市公司基本資料',
   'Listed company profile: name, ISIN, industry, chairman, CEO, spokesperson, auditor, capital, shares, address',
   0, 'daily', 'governance', false, true),

  ('t187ap03_P', '/v1/opendata/t187ap03_P',
   'https://openapi.twse.com.tw/v1/opendata/t187ap03_P',
   '上櫃公司基本資料',
   'OTC company profile — same schema as t187ap03_L; used as fallback for non-listed companies',
   0, 'daily', 'governance', false, true),

  ('t187ap02_L', '/v1/opendata/t187ap02_L',
   'https://openapi.twse.com.tw/v1/opendata/t187ap02_L',
   '上市公司持股逾10%大股東',
   'Major shareholders holding >10% of listed company (name only; exact % not disclosed)',
   0, 'daily', 'governance', false, true),

  ('t187ap11_L', '/v1/opendata/t187ap11_L',
   'https://openapi.twse.com.tw/v1/opendata/t187ap11_L',
   '上市公司董監事持股明細',
   'Board directors & supervisors: title, name, shares held, pledged shares, pledge %',
   0, 'daily', 'governance', false, true),

  ('t187ap04_L', '/v1/opendata/t187ap04_L',
   'https://openapi.twse.com.tw/v1/opendata/t187ap04_L',
   '上市公司每日重大訊息',
   'Daily material information disclosures: date, subject, applicable clause, description',
   0, 'daily', 'events', false, true),

  ('t21sc03_1', '/v1/opendata/t21sc03_1',
   'https://openapi.twse.com.tw/v1/opendata/t21sc03_1',
   '上市公司每月營業收入彙總表',
   'Monthly revenue — listed companies: MoM %, YoY %, YTD cumulative revenue',
   0, 'monthly', 'financials', false, true),

  ('t21sc03_2', '/v1/opendata/t21sc03_2',
   'https://openapi.twse.com.tw/v1/opendata/t21sc03_2',
   '上櫃公司每月營業收入彙總表',
   'Monthly revenue — OTC companies (fallback for non-listed)',
   0, 'monthly', 'financials', false, true),

  -- P1: works in production; may be restricted to institutional IPs in dev
  ('t163sb03_1', '/v1/opendata/t163sb03_1',
   'https://openapi.twse.com.tw/v1/opendata/t163sb03_1',
   '綜合損益表（一般業）',
   'Income statement — general industry: revenue, COGS, gross profit, operating income, net income, EPS',
   1, 'quarterly', 'financials', false, true),

  ('t163sb04_1', '/v1/opendata/t163sb04_1',
   'https://openapi.twse.com.tw/v1/opendata/t163sb04_1',
   '資產負債表（一般業）',
   'Balance sheet — general industry: current assets, total assets, liabilities, equity, book value per share',
   1, 'quarterly', 'financials', false, true),

  ('t163sb05_1', '/v1/opendata/t163sb05_1',
   'https://openapi.twse.com.tw/v1/opendata/t163sb05_1',
   '現金流量表（一般業）',
   'Cash flow statement — general industry: CFO, CFI, CFF, ending cash',
   1, 'quarterly', 'financials', false, true),

  ('t187ap14_L', '/v1/opendata/t187ap14_L',
   'https://openapi.twse.com.tw/v1/opendata/t187ap14_L',
   '上市公司股利分派情形',
   'Dividend distribution: cash dividend per share, stock dividend, board resolution date, AGM date',
   1, 'annual', 'financials', false, true),

  -- P2: market data
  ('t22sr01_1', '/v1/opendata/t22sr01_1',
   'https://openapi.twse.com.tw/v1/opendata/t22sr01_1',
   '個股日成交資訊',
   'Daily trade data: open, high, low, close, volume, market cap',
   2, 'daily', 'market', false, true)

ON CONFLICT (endpoint_code) DO NOTHING;

-- ── 593-field dictionary seed (auto-fillable fields via TWSE) ────────────────
-- Source tier key:
--   AUTO_DIRECT   = verbatim from TWSE field (45 fields currently)
--   AUTO_DERIVED  = computed from TWSE (e.g. paid_in_capital / 1e9)
--   AUTO_NLP_EXTRACTED = keyword/rule classification of text fields
--   REQUIRES_EXTERNAL_SOURCE = vessel valuations, charter rates, etc.
--   MANUAL_REVIEW = analyst must fill (facility terms, internal ratings, etc.)
INSERT INTO credit.credit_report_field_dictionary
    (section_no, field_path, label_en, label_zh, data_type,
     source_tier, twse_endpoint_code, twse_field_zh, transform_formula)
VALUES
  -- §1: regulatory compliance
  (1,'terms_and_conditions.borrower','Borrower Name','借款人名稱','string',
   'AUTO_DIRECT','t187ap03_L','公司名稱',NULL),
  (1,'regulatory_compliance.china_invested_enterprise','PRC-Invested Enterprise','中資企業','boolean',
   'AUTO_DERIVED','t187ap03_L','外國企業註冊地國','contains("中國") or contains("PRC")'),

  -- §3: borrower entity header
  (3,'3B_internal_ratings.borrower_entity_full_name','Borrower Full Name','借款人英文全名','string',
   'AUTO_DIRECT','t187ap03_L','英文全名',NULL),
  (3,'3B_internal_ratings.borrower_entity_abbrev','Borrower Abbreviation','借款人英文簡稱','string',
   'AUTO_DIRECT','t187ap03_L','英文簡稱',NULL),

  -- §4A: company identity
  (4,'4A_borrower.company_name_zh','Company Name (ZH)','公司中文名稱','string',
   'AUTO_DIRECT','t187ap03_L','公司名稱',NULL),
  (4,'4A_borrower.company_name_en','Company Name (EN)','公司英文名稱','string',
   'AUTO_DIRECT','t187ap03_L','英文全名',NULL),
  (4,'4A_borrower.registration_number','Tax ID / Registration No','統一編號','string',
   'AUTO_DIRECT','t187ap03_L','營利事業統一編號',NULL),
  (4,'4A_borrower.isin_code','ISIN Code','國際證券辨識號碼','string',
   'AUTO_DIRECT','t187ap03_L','國際證券辨識號碼(ISIN Code)',NULL),
  (4,'4A_borrower.listing_date','Listing Date','上市日期','date',
   'AUTO_DIRECT','t187ap03_L','上市日期',NULL),
  (4,'4A_borrower.shares_outstanding','Shares Outstanding','已發行普通股數','string',
   'AUTO_DIRECT','t187ap03_L','已發行普通股數或TDR原股發行股數',NULL),
  (4,'4A_borrower.incorporation_country','Country of Incorporation','設立地國','string',
   'AUTO_DERIVED','t187ap03_L','外國企業註冊地國','blank → Taiwan'),
  (4,'4A_borrower.incorporation_date','Incorporation Date','成立日期','date',
   'AUTO_DIRECT','t187ap03_L','成立日期',NULL),
  (4,'4A_borrower.legal_entity_type','Legal Entity Type','法律型態','string',
   'AUTO_DERIVED','t187ap03_L','市場別','"上市" → "Listed Company"'),
  (4,'4A_borrower.fiscal_year_end','Fiscal Year End','會計年度終結日','string',
   'MANUAL_REVIEW',NULL,NULL,NULL),
  (4,'4A_borrower.group_auditor','Group Auditor','簽證會計師事務所','string',
   'AUTO_DIRECT','t187ap03_L','簽證會計師事務所',NULL),

  -- §4B: ownership
  (4,'4B_ownership.shareholders','Major Shareholders (>10%)','大股東名單','list',
   'AUTO_DIRECT','t187ap02_L','大股東名稱',NULL),
  (4,'4B_ownership.ultimate_beneficial_owner','Ultimate Beneficial Owner','最終受益人','string',
   'AUTO_DIRECT','t187ap02_L','大股東名稱','first()'),

  -- §4C: management
  (4,'4C_management.ceo_name','CEO Name','總經理','string',
   'AUTO_DIRECT','t187ap03_L','總經理',NULL),
  (4,'4C_management.ceo_title','CEO Title','總經理職稱','string',
   'AUTO_DERIVED','t187ap03_L','總經理','"President"'),
  (4,'4C_management.cfo_name','CFO / Spokesperson','發言人','string',
   'AUTO_DIRECT','t187ap03_L','發言人',NULL),
  (4,'4C_management.cfo_title','CFO / Spokesperson Title','發言人職稱','string',
   'AUTO_DIRECT','t187ap03_L','發言人職稱',NULL),

  -- §4D: business profile
  (4,'4D_business.primary_business','Primary Business','主要業務','string',
   'AUTO_DIRECT','t187ap03_L','主要業務',NULL),
  (4,'4D_business.industry_category','Industry Category','產業別','string',
   'AUTO_DIRECT','t187ap03_L','產業別',NULL),
  (4,'4D_business.reporting_type','Financial Report Type','財報類型','string',
   'AUTO_DIRECT','t187ap03_L','財務報告書類型',NULL),

  -- §4E: financials snapshot
  (4,'4E_financials.currency','Currency','幣別','string',
   'AUTO_DERIVED',NULL,NULL,'"NTD"'),
  (4,'4E_financials.paid_in_capital_ntd_bn','Paid-in Capital (NTD bn)','實收資本額（十億元）','number',
   'AUTO_DERIVED','t187ap03_L','實收資本額(元)','/ 1e9'),
  (4,'4E_financials.fiscal_year','Snapshot Fiscal Year','快照財務年度','string',
   'AUTO_DERIVED','t21sc03_1','資料年月','ROC year + 1911'),
  (4,'4E_financials.revenue','YTD Revenue (NTD mn)','累計營收（百萬元）','number',
   'AUTO_DERIVED','t21sc03_1','當月累計營收','/ 1000.0'),
  (4,'4E_financials.unit','Financial Unit','財務單位','string',
   'AUTO_DERIVED',NULL,NULL,'"NTD million"'),
  -- P1: IS-backed financials (available when t163sb03_1 works)
  (4,'4E_financials.ebitda','EBITDA (NTD mn)','EBITDA（百萬元）','number',
   'AUTO_DERIVED','t163sb03_1','營業利益(損失)','operating_income / 1000.0 (EBIT proxy)'),
  (4,'4E_financials.net_income','Net Income (NTD mn)','本期淨利（百萬元）','number',
   'AUTO_DERIVED','t163sb03_1','本期淨利(淨損)','/ 1000.0'),
  -- P2: market-cap from stock price
  (4,'4E_financials.market_cap_ntd_bn','Market Cap (NTD bn)','市值（十億元）','number',
   'AUTO_DERIVED','t22sr01_1','收盤價','close_price × shares_outstanding / 1e9'),

  -- §4G: event risk
  (4,'4G_risk_events.has_material_news','Has Material News','是否有重大訊息','boolean',
   'AUTO_DIRECT','t187ap04_L',NULL,NULL),
  (4,'4G_risk_events.news_count_recent','Recent Material News Count','近期重大訊息數量','number',
   'AUTO_DIRECT','t187ap04_L',NULL,NULL),
  (4,'4G_risk_events.high_risk_categories','High-Risk Event Categories','高風險類別','list',
   'AUTO_NLP_EXTRACTED','t187ap04_L','符合條款',NULL),
  (4,'4G_risk_events.latest_news_summary','Latest Material News Summary','最新重大訊息摘要','string',
   'AUTO_NLP_EXTRACTED','t187ap04_L','主旨',NULL),

  -- §5F: corporate guarantee identity
  (5,'5F_corporate_guarantee.guarantor_full_name','Guarantor Full Name','保證人全名','string',
   'AUTO_DIRECT','t187ap03_L','公司名稱',NULL),
  (5,'5F_corporate_guarantee.guarantor_listed_exchange','Guarantor Listed Exchange','保證人上市交易所','string',
   'AUTO_DERIVED','t187ap03_L','市場別','"上市" → "Taiwan Stock Exchange"'),
  (5,'5F_corporate_guarantee.revenue_twd_bn','Guarantor Revenue (NTD bn)','保證人營收（十億元）','number',
   'AUTO_DERIVED','t21sc03_1','當月累計營收','/ 1e6'),
  -- P1: BS/IS-backed §5F fields
  (5,'5F_corporate_guarantee.net_worth_twd_bn','Guarantor Net Worth (NTD bn)','保證人淨值（十億元）','number',
   'AUTO_DERIVED','t163sb04_1','權益總計','/ 1e6'),
  (5,'5F_corporate_guarantee.total_debt_twd_bn','Guarantor Total Debt (NTD bn)','保證人總負債（十億元）','number',
   'AUTO_DERIVED','t163sb04_1','負債總計','/ 1e6'),
  (5,'5F_corporate_guarantee.cash_twd_bn','Guarantor Cash (NTD bn)','保證人現金（十億元）','number',
   'AUTO_DERIVED','t163sb05_1','期末現金及約當現金','/ 1e6'),
  (5,'5F_corporate_guarantee.ebitda_twd_bn','Guarantor EBITDA (NTD bn)','保證人EBITDA（十億元）','number',
   'AUTO_DERIVED','t163sb03_1','營業利益(損失)','/ 1e6'),
  (5,'5F_corporate_guarantee.net_margin_pct','Guarantor Net Margin %','保證人淨利率','number',
   'AUTO_DERIVED','t163sb03_1','本期淨利(淨損)','net_income / revenue × 100'),
  (5,'5F_corporate_guarantee.roe_pct','Guarantor ROE %','保證人股東報酬率','number',
   'AUTO_DERIVED','t163sb03_1','本期淨利(淨損)','net_income / total_equity × 100'),

  -- §5G: responsible person
  (5,'5G_responsible_person.name','Responsible Person Name','負責人姓名','string',
   'AUTO_DIRECT','t187ap11_L','姓名',NULL),
  (5,'5G_responsible_person.title','Responsible Person Title','負責人職稱','string',
   'AUTO_DERIVED','t187ap11_L','職稱','"Chairman / 董事長"'),

  -- §7A: IS per FY (template — actual rows dynamically keyed FY2022/FY2023/FY2024/FY2025F)
  (7,'7A_borrower_financials.reporting_entity','Reporting Entity','財報主體','string',
   'AUTO_DIRECT','t187ap03_L','公司名稱',NULL),
  (7,'7A_borrower_financials.auditor','Financial Auditor','簽證會計師事務所','string',
   'AUTO_DIRECT','t187ap03_L','簽證會計師事務所',NULL),
  (7,'7A_borrower_financials.reporting_currency','Reporting Currency','財報幣別','string',
   'AUTO_DERIVED',NULL,NULL,'"NTD"'),
  (7,'7A_borrower_financials.unit','Financial Unit','財報單位','string',
   'AUTO_DERIVED',NULL,NULL,'"NTD million"'),
  (7,'7A_borrower_financials.accounting_standard','Accounting Standard','會計準則','string',
   'MANUAL_REVIEW',NULL,NULL,NULL),
  -- IS fields (P0: revenue from monthly data; P1: full IS when t163sb03_1 available)
  (7,'7A_borrower_financials.income_statement.{FY}.revenue','Revenue (NTD mn)','營業收入','number',
   'AUTO_DERIVED','t21sc03_1','當月累計營收','sum(monthly) / 1000.0'),
  (7,'7A_borrower_financials.income_statement.{FY}.gross_profit','Gross Profit (NTD mn)','毛利','number',
   'AUTO_DERIVED','t163sb03_1','營業毛利(毛損)','/ 1000.0'),
  (7,'7A_borrower_financials.income_statement.{FY}.op_profit','Operating Income (NTD mn)','營業利益','number',
   'AUTO_DERIVED','t163sb03_1','營業利益(損失)','/ 1000.0'),
  (7,'7A_borrower_financials.income_statement.{FY}.net_income','Net Income (NTD mn)','本期淨利','number',
   'AUTO_DERIVED','t163sb03_1','本期淨利(淨損)','/ 1000.0'),
  (7,'7A_borrower_financials.income_statement.{FY}.ebitda','EBITDA (NTD mn)','EBITDA','number',
   'AUTO_DERIVED','t163sb03_1','營業利益(損失)','op_profit + D&A from CF'),
  -- BS fields (P1: t163sb04_1)
  (7,'7A_borrower_financials.balance_sheet.{FY}.total_assets','Total Assets (NTD mn)','資產總計','number',
   'AUTO_DERIVED','t163sb04_1','資產總計','/ 1000.0'),
  (7,'7A_borrower_financials.balance_sheet.{FY}.total_equity','Total Equity (NTD mn)','權益總計','number',
   'AUTO_DERIVED','t163sb04_1','權益總計','/ 1000.0'),
  (7,'7A_borrower_financials.balance_sheet.{FY}.total_debt','Total Debt (NTD mn)','負債總計','number',
   'AUTO_DERIVED','t163sb04_1','負債總計','/ 1000.0'),
  (7,'7A_borrower_financials.balance_sheet.{FY}.cash_and_equivalents','Cash (NTD mn)','現金及約當現金','number',
   'AUTO_DERIVED','t163sb05_1','期末現金及約當現金','/ 1000.0'),
  (7,'7A_borrower_financials.balance_sheet.{FY}.net_debt','Net Debt (NTD mn)','淨負債','number',
   'AUTO_DERIVED','t163sb04_1+t163sb05_1','負債總計','total_debt - cash'),
  -- CF fields (P1: t163sb05_1)
  (7,'7A_borrower_financials.cash_flow.{FY}.ocf','Operating CF (NTD mn)','營業活動現金流','number',
   'AUTO_DERIVED','t163sb05_1','營業活動之淨現金流入(出)','/ 1000.0'),
  (7,'7A_borrower_financials.cash_flow.{FY}.capex','Capex (NTD mn)','資本支出','number',
   'AUTO_DERIVED','t163sb05_1','取得不動產廠房及設備','abs() / 1000.0'),
  (7,'7A_borrower_financials.cash_flow.{FY}.fcf','Free Cash Flow (NTD mn)','自由現金流','number',
   'AUTO_DERIVED','t163sb05_1',NULL,'ocf - capex'),
  -- §7B ratios (P1: derived from IS + BS)
  (7,'7B_key_ratios.{FY}.current_ratio','Current Ratio','流動比率','number',
   'AUTO_DERIVED','t163sb04_1',NULL,'current_assets / current_liabilities'),
  (7,'7B_key_ratios.{FY}.gross_margin_pct','Gross Margin %','毛利率','number',
   'AUTO_DERIVED','t163sb03_1',NULL,'gross_profit / revenue × 100'),
  (7,'7B_key_ratios.{FY}.ebitda_margin_pct','EBITDA Margin %','EBITDA率','number',
   'AUTO_DERIVED','t163sb03_1',NULL,'operating_income / revenue × 100'),
  (7,'7B_key_ratios.{FY}.ni_margin_pct','Net Margin %','淨利率','number',
   'AUTO_DERIVED','t163sb03_1',NULL,'net_income / revenue × 100'),
  (7,'7B_key_ratios.{FY}.roe_pct','ROE %','股東報酬率','number',
   'AUTO_DERIVED','t163sb03_1+t163sb04_1',NULL,'net_income / total_equity × 100'),
  (7,'7B_key_ratios.{FY}.debt_equity','Debt/Equity Ratio','負債比率','number',
   'AUTO_DERIVED','t163sb04_1',NULL,'total_liabilities / total_equity'),
  (7,'7B_key_ratios.{FY}.debt_ebitda','Net Debt/EBITDA','淨負債/EBITDA','number',
   'AUTO_DERIVED','t163sb04_1+t163sb03_1',NULL,'net_debt / ebitda'),
  -- §7 entities_to_analyze
  (7,'entities_to_analyze.borrower_name','Borrower Entity for Analysis','分析主體名稱','string',
   'AUTO_DIRECT','t187ap03_L','英文全名',NULL),

  -- §9: ACRA checklist
  (9,'9A_checklist.item16_entity_name','ACRA Entity Name','ACRA查詢實體名稱','string',
   'AUTO_DIRECT','t187ap03_L','英文全名',NULL)

ON CONFLICT (field_path) DO NOTHING;

COMMIT;
