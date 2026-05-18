/**
 * Comprehensive field roundtrip tests — all sections 1-10
 * Tests: ETL→expand→form→finalize and user-typed input (｜, 。, key=value)
 *
 * Run: node tests/test_field_roundtrip.js
 */
'use strict';
const vm = require('vm');
const fs = require('fs');
const path = require('path');

// ── Load index.html into vm ────────────────────────────────────────────────
const html = fs.readFileSync(
  path.join(__dirname, '../static/index.html'), 'utf8'
);
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
if (!scriptMatch) { console.error('Cannot find inline script'); process.exit(1); }
const scriptSrc = scriptMatch[1];

const ctx = vm.createContext({
  localStorage: { getItem: () => null, setItem: () => {} },
  document: {
    addEventListener: () => {},
    getElementById: () => null,
    querySelectorAll: () => [],
    querySelector: () => null,
    body: { appendChild: () => {}, removeChild: () => {} },
  },
  window: { location: { href: '' } },
  navigator: { language: 'en' },
  marked: { parse: (s) => s },
  fetch: async () => ({ ok: false, json: async () => ({}) }),
  bootstrap: {},
  console,
  setTimeout: () => {},
  clearTimeout: () => {},
});
vm.runInContext(scriptSrc, ctx);

const { expandPayload, finalizePayload, collectFormData, FIELD_DEFS, _fid } = ctx;

// ── Test helpers ─────────────────────────────────────────────────────────────
let pass = 0, fail = 0;
function ok(label, cond) {
  if (cond) { console.log('  ✅ ', label); pass++; }
  else      { console.error('  ❌ ', label); fail++; }
}

/**
 * Simulate populateForm + collectFormData using DOM-less approach:
 * populate a JS object with field values, then collect them back.
 * This tests the expand→finalize roundtrip without needing a real DOM.
 */
function roundtrip(secNo, inputObj) {
  const expanded = expandPayload(secNo, JSON.parse(JSON.stringify(inputObj)));
  const finalized = finalizePayload(secNo, JSON.parse(JSON.stringify(expanded)));
  return { expanded, finalized };
}

/**
 * Simulate user typing pipe-separated text into a 'lines' field,
 * then saving via the form (collectFormData equiv + finalizePayload).
 *
 * The form stores 'lines' as array-of-strings; we build that array here.
 */
function userTypedLines(secNo, fieldPath, textareaContent) {
  // Mimic collectFormData: split textarea by newline
  const lines = textareaContent.split('\n').map(l => l.trim()).filter(Boolean);
  // Build a minimal data object
  const data = {};
  // setNestedValue equivalent
  const keys = fieldPath.split('.');
  let cur = data;
  for (let i = 0; i < keys.length - 1; i++) {
    cur[keys[i]] = cur[keys[i]] || {};
    cur = cur[keys[i]];
  }
  cur[keys[keys.length - 1]] = lines;
  return finalizePayload(secNo, data);
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §1 Facility Rows (11 cols) ──────────────────────────────────────');

// 1a. ETL object → expand → finalize roundtrip
{
  const etlRow = {
    item_no: 1,
    borrower_full_name: 'Evergreen Marine (Asia) Pte. Ltd.',
    booking_location: 'SG',
    proposed_facility_usd_m: 178.5,   // ETL key
    is_new: true,
    currency: 'USD',
    tenor: '11Y',
    facility_type: 'SLL Term Loan',
    collateral_pre: 'RG (KDB)',        // ETL key
    collateral_post: 'Vessel mortgage',// ETL key
    guarantor: 'EMC',
  };
  const data = { facility_summary: { rows: [etlRow] } };
  const { finalized } = roundtrip(1, data);
  const row = (finalized.facility_summary || {}).rows?.[0];
  ok('§1 rows — ETL proposed_facility_usd_m preserved', row?.proposed_usd_m === 178.5);
  ok('§1 rows — ETL collateral_pre preserved', row?.collateral_pre_delivery === 'RG (KDB)');
  ok('§1 rows — ETL collateral_post preserved', row?.collateral_post_delivery === 'Vessel mortgage');
  ok('§1 rows — borrower_full_name preserved', row?.borrower_full_name === 'Evergreen Marine (Asia) Pte. Ltd.');
  ok('§1 rows — is_new preserved', row?.is_new === true);
}

// 1b. User types with fullwidth ｜ pipe
{
  const text = '1｜長榮海運(亞洲)EMA｜國泰世華新加坡分行｜178.5｜Yes｜USD｜11Y｜SLL Term Loan｜RG (KDB)｜Vessel mortgage｜EMC';
  const fin = userTypedLines(1, 'facility_summary.rows', text);
  const row = fin?.facility_summary?.rows?.[0];
  ok('§1 rows — fullwidth ｜: borrower_full_name not empty', row?.borrower_full_name === '長榮海運(亞洲)EMA');
  ok('§1 rows — fullwidth ｜: proposed_usd_m = 178.5', row?.proposed_usd_m === 178.5);
  ok('§1 rows — fullwidth ｜: is_new = true', row?.is_new === true);
  ok('§1 rows — fullwidth ｜: currency = USD', row?.currency === 'USD');
  ok('§1 rows — fullwidth ｜: collateral_pre_delivery preserved', row?.collateral_pre_delivery === 'RG (KDB)');
}

// 1c. User types with key=value format
{
  const text = '項次=1｜借款人全名=長榮海運(亞洲)EMA｜預訂地點=國泰世華新加坡分行｜提議金額USD M=178.5｜是否新增=Yes｜幣別=USD｜期限=11Y｜授信類型=SLL Term Loan｜交船前擔保品=RG (KDB)｜交船後擔保品=Vessel mortgage｜保證人=EMC';
  const fin = userTypedLines(1, 'facility_summary.rows', text);
  const row = fin?.facility_summary?.rows?.[0];
  ok('§1 rows — key=value: item_no = 1', row?.item_no === 1);
  ok('§1 rows — key=value: borrower_full_name extracted', row?.borrower_full_name === '長榮海運(亞洲)EMA');
  ok('§1 rows — key=value: proposed_usd_m = 178.5', row?.proposed_usd_m === 178.5);
  ok('§1 rows — key=value: is_new = true', row?.is_new === true);
  ok('§1 rows — key=value: guarantor extracted', row?.guarantor === 'EMC');
}

// 1d. User types multiple rows separated by 。
{
  const text = '1｜EMA｜SG｜178.5｜Yes｜USD｜11Y｜SLL Term Loan｜RG｜Mortgage｜EMC。2｜EMA｜SG｜50｜No｜USD｜3Y｜RCF｜NIL｜NIL｜EMC';
  const fin = userTypedLines(1, 'facility_summary.rows', text);
  const rows = fin?.facility_summary?.rows;
  ok('§1 rows — 。separator: 2 rows parsed', rows?.length === 2);
  ok('§1 rows — 。separator: row1 borrower', rows?.[0]?.borrower_full_name === 'EMA');
  ok('§1 rows — 。separator: row2 item_no = 2', rows?.[1]?.item_no === 2);
  ok('§1 rows — 。separator: row2 is_new = false', rows?.[1]?.is_new === false);
}

// 1e. Mix: key=value row1 。 positional row2 and row3
{
  const text = '項次=1｜借款人全名=長榮海運(亞洲)EMA｜預訂地點=國泰世華新加坡分行｜提議金額USD M=178.5｜是否新增=Yes｜幣別=USD｜期限=11Y｜授信類型=SLL Term Loan｜交船前擔保品=母公司及關聯企業連帶保證、造船預付款擔保｜交船後擔保品=交船後第一順位船舶抵押權及租金收款權益轉讓｜保證人=長榮海運(EMC)。2｜長榮海運(亞洲)EMA｜國泰世華新加坡分行｜50｜Yes｜USD｜3Y｜Revolving Credit Facility｜母公司連帶保證｜無特定擔保品，維持原授信條件｜長榮海運(EMC)。3｜長榮海運(亞洲)EMA｜國泰世華新加坡分行｜80｜No｜USD｜7Y｜Refinancing Facility｜既有船舶抵押權｜調整後第一順位船舶抵押權｜長榮海運(EMC)';
  const fin = userTypedLines(1, 'facility_summary.rows', text);
  const rows = fin?.facility_summary?.rows;
  ok('§1 rows — mixed format: 3 rows parsed', rows?.length === 3);
  ok('§1 rows — mixed format: row1 borrower (key=value)', rows?.[0]?.borrower_full_name === '長榮海運(亞洲)EMA');
  ok('§1 rows — mixed format: row1 collateral_pre (key=value)', rows?.[0]?.collateral_pre_delivery === '母公司及關聯企業連帶保證、造船預付款擔保');
  ok('§1 rows — mixed format: row2 proposed = 50', rows?.[1]?.proposed_usd_m === 50);
  ok('§1 rows — mixed format: row3 item_no = 3', rows?.[2]?.item_no === 3);
  ok('§1 rows — mixed format: row3 facility_type', rows?.[2]?.facility_type === 'Refinancing Facility');
}

// 1f. §1 ASCII pipe with spaces (the most common manual input style)
{
  const text = '1 | 長榮海運(亞洲)EMA | 國泰世華新加坡分行 | 178.5 | Yes | USD | 11Y | SLL Term Loan | RG (KDB) | Vessel mortgage | EMC';
  const fin = userTypedLines(1, 'facility_summary.rows', text);
  const row = fin?.facility_summary?.rows?.[0];
  ok('§1 rows — spaced ASCII |: borrower not empty', row?.borrower_full_name === '長榮海運(亞洲)EMA');
  ok('§1 rows — spaced ASCII |: proposed = 178.5', row?.proposed_usd_m === 178.5);
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §1 Deal Comparison (3 cols) ─────────────────────────────────────');
{
  const text1 = 'Guarantor｜EMC｜EMC';
  const text2 = 'Amount | USD178.5m | USD128.75m\nTenor | 11Y | 11Y';
  const fin1 = userTypedLines(1, 'terms_and_conditions.deal_comparison', text1);
  const fin2 = userTypedLines(1, 'terms_and_conditions.deal_comparison', text2);
  ok('§1 deal_comparison — fullwidth ｜: term preserved', fin1?.terms_and_conditions?.deal_comparison?.[0]?.term === 'Guarantor');
  ok('§1 deal_comparison — fullwidth ｜: proposed preserved', fin1?.terms_and_conditions?.deal_comparison?.[0]?.proposed === 'EMC');
  ok('§1 deal_comparison — spaced |: 2 rows parsed', fin2?.terms_and_conditions?.deal_comparison?.length === 2);
  ok('§1 deal_comparison — spaced |: row1 term', fin2?.terms_and_conditions?.deal_comparison?.[0]?.term === 'Amount');
  ok('§1 deal_comparison — spaced |: row2 proposed', fin2?.terms_and_conditions?.deal_comparison?.[1]?.proposed === '11Y');
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §1 SLL KPI Table (6 cols) ────────────────────────────────────────');
{
  const text = 'CO2 Intensity｜<=8.5｜7.2｜2024｜Yes｜-5\nMSCI ESG Rating｜AA｜A｜2024｜No｜0';
  const fin = userTypedLines(1, 'sll_kpi_performance.kpis', text);
  const kpis = fin?.sll_kpi_performance?.kpis;
  ok('§1 KPIs — fullwidth ｜: 2 rows', kpis?.length === 2);
  ok('§1 KPIs — row1 kpi_name', kpis?.[0]?.kpi_name === 'CO2 Intensity');
  ok('§1 KPIs — row1 target_value (contains <=)', kpis?.[0]?.target_value === '<=8.5');
  ok('§1 KPIs — row1 on_track = true', kpis?.[0]?.on_track === true);
  ok('§1 KPIs — row1 ratchet_bps = -5', kpis?.[0]?.ratchet_bps === -5);
  ok('§1 KPIs — row2 on_track = false', kpis?.[1]?.on_track === false);
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §2 Risk & Mitigants (simple lines — no pipe split) ──────────────');
{
  const data = {
    '2E_risk_and_mitigants': {
      risk_1_title: 'Market Risk',
      risk_1_level: 'High',
      risk_1_risk_bullets: 'Charter rates may decline\nExposure to cyclical downturns',
      risk_1_mitigant_bullets: '80% revenue covered by TC\nKDB RG covers pre-delivery',
    }
  };
  const fin = finalizePayload(2, data);
  const risks = fin?.['2E_risk_and_mitigants']?.risk_factors;
  ok('§2 risk_factors array created', Array.isArray(risks) && risks.length === 1);
  ok('§2 risk title preserved', risks?.[0]?.title === 'Market Risk');
  ok('§2 risk bullets parsed', risks?.[0]?.risk_bullets?.length === 2);
  ok('§2 mitigant bullets parsed', risks?.[0]?.mitigant_bullets?.length === 2);
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §3 External Ratings (4 cols) ────────────────────────────────────');
{
  const text1 = 'EMA｜NR｜NR｜NR\nEMC｜BBB-｜Baa3｜NR';
  const text2 = 'EMA | NR | NR | NR\nEMC | BBB- | Baa3 | NR';
  const fin1 = userTypedLines(3, '3A_external_ratings.ratings', text1);
  const fin2 = userTypedLines(3, '3A_external_ratings.ratings', text2);
  ok('§3 ratings — fullwidth ｜: 2 rows', fin1?.['3A_external_ratings']?.ratings?.length === 2);
  ok('§3 ratings — fullwidth ｜: entity = EMA', fin1?.['3A_external_ratings']?.ratings?.[0]?.entity === 'EMA');
  ok('§3 ratings — fullwidth ｜: moodys not NR', fin1?.['3A_external_ratings']?.ratings?.[1]?.moodys === 'Baa3');
  ok('§3 ratings — spaced |: entity = EMA', fin2?.['3A_external_ratings']?.ratings?.[0]?.entity === 'EMA');
  ok('§3 ratings — spaced |: sp = BBB-', fin2?.['3A_external_ratings']?.ratings?.[1]?.sp === 'BBB-');
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §4 Flat fields (no pipe split in finalizePayload) ───────────────');
{
  const data = {
    '4C_management': { ceo_name: 'John Smith', ceo_title: 'CEO', ceo_background: '20 years shipping' },
    '4F_fleet': { owned_vessel_count: 5, owned_total_teu: 50000 },
  };
  const fin = finalizePayload(4, data);
  ok('§4 management CEO preserved', fin?.['4C_management']?.[0]?.name === 'John Smith');
  ok('§4 fleet breakdown created', fin?.['4F_fleet']?.fleet_breakdown?.length >= 1);
  ok('§4 fleet owned teu', fin?.['4F_fleet']?.fleet_breakdown?.[0]?.total_teu === 50000);
}

// §4 shareholders are stored as plain pipe strings (no conversion in finalizePayload)
{
  const text = 'Parent Corp｜100｜TW\nMinority｜20｜SG';
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
  // These remain as pipe strings in the saved data — verify no crash
  const data = { '4B_ownership': { shareholders: lines } };
  const fin = finalizePayload(4, data);
  ok('§4 shareholders stored as pipe strings (no conversion)', Array.isArray(fin?.['4B_ownership']?.shareholders));
  ok('§4 shareholders: 2 entries', fin?.['4B_ownership']?.shareholders?.length === 2);
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §5 Security fields (flat, no pipe split) ────────────────────────');
{
  const data = {
    '5A_security_overview': { is_secured: true, instr_1_instrument: 'Vessel Mortgage', instr_1_description: 'First priority' },
    '5D_insurance': { hm_insurer: 'Skuld P&I', hm_insured_value_usd_m: 180, hm_notes: 'Full value' },
  };
  const fin = finalizePayload(5, data);
  ok('§5 security instruments created', fin?.['5A_security_overview']?.security_instruments?.length === 1);
  ok('§5 instrument type preserved', fin?.['5A_security_overview']?.security_instruments?.[0]?.instrument === 'Vessel Mortgage');
  ok('§5 insurance instruments created', fin?.['5D_insurance']?.instruments?.length === 1);
  ok('§5 insurer preserved', fin?.['5D_insurance']?.instruments?.[0]?.insurer_or_club === 'Skuld P&I');
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §6 Milestones (flat, no pipe split) ─────────────────────────────');
{
  const data = {
    '6D_milestones': {
      m1_name: 'Steel Cutting',
      m1_date: '2025-03-01',
      m1_pct: 10,
      m1_amount_usd_m: 22.3,
    }
  };
  const fin = finalizePayload(6, data);
  ok('§6 milestones created', fin?.['6D_milestones']?.milestones?.length === 1);
  ok('§6 milestone name preserved', fin?.['6D_milestones']?.milestones?.[0]?.milestone === 'Steel Cutting');
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §7 Financial tables (flat structure) ────────────────────────────');
{
  const data = {
    '7A_borrower_financials': {
      reporting_currency: 'USD',
      unit: 'millions',
      revenue_fy2024: 8200,
      ebitda_fy2024: 3100,
      net_income_fy2024: 2200,
    }
  };
  const fin = finalizePayload(7, data);
  ok('§7 income statement created', fin?.['7A_borrower_financials']?.income_statement?.FY2024);
  ok('§7 revenue preserved', fin?.['7A_borrower_financials']?.income_statement?.FY2024?.revenue === 8200);
  ok('§7 ebitda preserved', fin?.['7A_borrower_financials']?.income_statement?.FY2024?.ebitda === 3100);
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §8 Legal charges (flat structure) ───────────────────────────────');
{
  const data = {
    '8A_acra_banking_charges': {
      total_charges: 12,
      active_charges: 8,
      satisfied_charges: 4,
      total_active_usd_m: 350,
      cub_charge_count: 3,
      cub_total_usd_m: 200,
    }
  };
  const fin = finalizePayload(8, data);
  ok('§8 charges summary created', fin?.['8A_acra_banking_charges']?.summary?.total_charges === 12);
  ok('§8 active charges preserved', fin?.['8A_acra_banking_charges']?.summary?.active_charges === 8);
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §9 Checklist items (3 cols) ─────────────────────────────────────');
{
  const text1 = 'AML Check｜Yes｜Completed\nSanctions Check｜Yes｜Cleared';
  const text2 = 'AML Check | Yes | Completed\nSanctions Check | No | Pending';
  const fin1 = userTypedLines(9, '9A_checklist.items', text1);
  const fin2 = userTypedLines(9, '9A_checklist.items', text2);
  ok('§9 checklist — fullwidth ｜: 2 items', fin1?.['9A_checklist']?.items?.length === 2);
  ok('§9 checklist — fullwidth ｜: item name', fin1?.['9A_checklist']?.items?.[0]?.item === 'AML Check');
  ok('§9 checklist — fullwidth ｜: response = Yes', fin1?.['9A_checklist']?.items?.[0]?.response === 'Yes');
  ok('§9 checklist — spaced |: 2 items', fin2?.['9A_checklist']?.items?.length === 2);
  ok('§9 checklist — spaced |: remarks preserved', fin2?.['9A_checklist']?.items?.[0]?.remarks === 'Completed');
}

// §9 conditions_precedent (2 cols)
{
  const text = 'Execute facility docs｜Before drawdown\nKYC completion｜Before drawdown';
  const fin = userTypedLines(9, '9B_conditions_covenants.conditions_precedent', text);
  ok('§9 CP — 2 items', fin?.['9B_conditions_covenants']?.conditions_precedent?.length === 2);
  ok('§9 CP — description preserved', fin?.['9B_conditions_covenants']?.conditions_precedent?.[0]?.description === 'Execute facility docs');
  ok('§9 CP — testing preserved', fin?.['9B_conditions_covenants']?.conditions_precedent?.[0]?.testing === 'Before drawdown');
}

// §9 ongoing covenants (3 cols)
{
  const text = 'ACR Test｜>=120%｜Every 2 years\nDSCR Test｜>=1.15x｜Annual';
  const fin = userTypedLines(9, '9B_conditions_covenants.ongoing_covenants', text);
  ok('§9 covenants — 2 items', fin?.['9B_conditions_covenants']?.ongoing_covenants?.length === 2);
  ok('§9 covenants — description', fin?.['9B_conditions_covenants']?.ongoing_covenants?.[0]?.description === 'ACR Test');
  ok('§9 covenants — threshold (contains >=)', fin?.['9B_conditions_covenants']?.ongoing_covenants?.[0]?.threshold === '>=120%');
  ok('§9 covenants — testing', fin?.['9B_conditions_covenants']?.ongoing_covenants?.[1]?.testing === 'Annual');
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §10 Fleet Growth rows (5 cols) ──────────────────────────────────');
{
  const text1 = '2024｜5.2｜8.5｜650｜61.2\n2025E｜5.5｜8.8｜660｜62.5';
  const text2 = '2024 | 5.2 | 8.5 | 650 | 61.2\n2025E | 5.5 | 8.8 | 660 | 62.5';
  const fin1 = userTypedLines(10, '10B_fleet_growth.rows', text1);
  const fin2 = userTypedLines(10, '10B_fleet_growth.rows', text2);
  ok('§10 fleet rows — fullwidth ｜: 2 rows', fin1?.['10B_fleet_growth']?.rows?.length === 2);
  ok('§10 fleet rows — fullwidth ｜: year_label', fin1?.['10B_fleet_growth']?.rows?.[0]?.year_label === '2024');
  ok('§10 fleet rows — fullwidth ｜: owned_fleet_teu_m', fin1?.['10B_fleet_growth']?.rows?.[0]?.owned_fleet_teu_m === 5.2);
  ok('§10 fleet rows — fullwidth ｜: total_vessels', fin1?.['10B_fleet_growth']?.rows?.[0]?.total_vessels === 650);
  ok('§10 fleet rows — spaced |: 2 rows', fin2?.['10B_fleet_growth']?.rows?.length === 2);
  ok('§10 fleet rows — spaced |: year 2025E', fin2?.['10B_fleet_growth']?.rows?.[1]?.year_label === '2025E');
}

// §10 projections key assumptions (4 cols)
{
  const text = 'Freight rate (USD/TEU)｜2500｜2200｜2000\nFleet utilization%｜92｜91｜90';
  const fin = userTypedLines(10, '10C_projections.key_assumptions', text);
  ok('§10 key_assumptions — 2 rows', fin?.['10C_projections']?.key_assumptions?.length === 2);
  ok('§10 key_assumptions — assumption name', fin?.['10C_projections']?.key_assumptions?.[0]?.assumption === 'Freight rate (USD/TEU)');
  ok('§10 key_assumptions — FY2026E value', fin?.['10C_projections']?.key_assumptions?.[0]?.FY2026E === 2500);
}

// §10 base case P&L (5 cols)
{
  const text = 'Revenue｜8500｜8000｜7500｜No\nEBITDA｜3200｜2900｜2600｜No\nTotal｜11700｜10900｜10100｜Yes';
  const fin = userTypedLines(10, '10C_projections.base_case_pl', text);
  ok('§10 base_case_pl — 3 rows', fin?.['10C_projections']?.base_case_pl?.length === 3);
  ok('§10 base_case_pl — item name', fin?.['10C_projections']?.base_case_pl?.[0]?.item === 'Revenue');
  ok('§10 base_case_pl — FY2026E', fin?.['10C_projections']?.base_case_pl?.[0]?.FY2026E === 8500);
  ok('§10 base_case_pl — subtotal flag', fin?.['10C_projections']?.base_case_pl?.[2]?.is_subtotal === true);
}

// §10 base case DSCR (4 cols)
{
  const text = 'FY2026E｜250｜185｜1.35\nFY2027E｜230｜180｜1.28';
  const fin = userTypedLines(10, '10C_projections.base_case_dscr', text);
  ok('§10 base_case_dscr — 2 rows', fin?.['10C_projections']?.base_case_dscr?.length === 2);
  ok('§10 base_case_dscr — year_label', fin?.['10C_projections']?.base_case_dscr?.[0]?.year_label === 'FY2026E');
  ok('§10 base_case_dscr — dscr value', fin?.['10C_projections']?.base_case_dscr?.[0]?.dscr === 1.35);
}

// §10 stress assumptions (4 cols)
{
  const text = 'Freight rate｜2500｜1500｜-40%\nUtilization｜92%｜85%｜-7pp';
  const fin = userTypedLines(10, '10C_projections.stress_assumptions', text);
  ok('§10 stress_assumptions — 2 rows', fin?.['10C_projections']?.stress_assumptions?.length === 2);
  ok('§10 stress_assumptions — assumption name', fin?.['10C_projections']?.stress_assumptions?.[0]?.assumption === 'Freight rate');
  ok('§10 stress_assumptions — worse_case', fin?.['10C_projections']?.stress_assumptions?.[0]?.worse_case === '1500');
  ok('§10 stress_assumptions — stress_magnitude (contains %)', fin?.['10C_projections']?.stress_assumptions?.[0]?.stress_magnitude === '-40%');
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §1 Footnotes (special [symbol] text format) ─────────────────────');
{
  const text = '[1] Vessel delivery 30 Jun 2028 with 180 days grace period.\n[2] PSR facility to be cancelled if not drawn.';
  const fin = userTypedLines(1, 'facility_summary.footnotes', text);
  ok('§1 footnotes — 2 entries', fin?.facility_summary?.footnotes?.length === 2);
  ok('§1 footnotes — symbol extracted', fin?.facility_summary?.footnotes?.[0]?.symbol === '[1]');
  ok('§1 footnotes — text_verbatim preserved', fin?.facility_summary?.footnotes?.[0]?.text_verbatim === 'Vessel delivery 30 Jun 2028 with 180 days grace period.');
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n── §1–10 expandPayload ETL object → pipe string conversion ─────────');
{
  // §1 rows with ETL keys (proposed_facility_usd_m, collateral_pre, collateral_post)
  const etlData = {
    facility_summary: {
      rows: [{
        item_no: 1,
        borrower_full_name: 'EMA',
        booking_location: 'SG',
        proposed_facility_usd_m: 178.5,
        is_new: true,
        currency: 'USD',
        tenor: '11Y',
        facility_type: 'SLL Term Loan',
        collateral_pre: 'RG (KDB)',
        collateral_post: 'Vessel Mortgage',
        guarantor: 'EMC',
      }]
    }
  };
  const exp = expandPayload(1, etlData);
  const rowStr = exp?.facility_summary?.rows?.[0];
  ok('§1 expand ETL→string: result is string', typeof rowStr === 'string');
  ok('§1 expand ETL→string: proposed_usd_m in string', rowStr?.includes('178.5'));
  ok('§1 expand ETL→string: collateral_pre (ETL key) shown', rowStr?.includes('RG (KDB)'));
  ok('§1 expand ETL→string: is_new=true shown as Yes', rowStr?.includes('Yes'));
}

{
  // §3 ratings with entity_abbrev ETL key
  const etlData3 = {
    '3A_external_ratings': {
      ratings: [{ entity_abbrev: 'EMA', sp: 'NR', moodys: 'NR', fitch: 'NR' }]
    }
  };
  const exp3 = expandPayload(3, etlData3);
  const rStr = exp3?.['3A_external_ratings']?.ratings?.[0];
  ok('§3 expand ETL entity_abbrev → string contains EMA', typeof rStr === 'string' && rStr.includes('EMA'));
}

// ═══════════════════════════════════════════════════════════════════════════════
console.log('\n─────────────────────────────────────────────────────────────────────');
console.log(`  Total: ${pass + fail}  ✅ ${pass} passed  ❌ ${fail} failed`);
console.log('─────────────────────────────────────────────────────────────────────\n');
process.exit(fail > 0 ? 1 : 0);
