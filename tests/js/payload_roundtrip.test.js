/**
 * Jest tests: finalizePayload ↔ expandPayload roundtrip contracts.
 *
 * These tests actually EXECUTE the JavaScript functions extracted from
 * static/index.html — no browser required.  They catch:
 *
 *  1. finalizePayload output structure for each section (§1-§10)
 *  2. expandPayload inverse correctness — key values survive round-trips
 *  3. Key-name contracts that Python static analysis cannot verify at runtime
 *
 * Test type: 瀏覽器端 JavaScript 單元測試 (Jest) + expandPayload ↔ finalizePayload 雙向 roundtrip
 */

'use strict';

const { finalizePayload, expandPayload } = require('./helpers/extract');

// ── §1 facility_summary & deal_comparison_rows ───────────────────────────────

describe('§1 finalizePayload', () => {
  test('deal_comparison array → deal_comparison_rows objects', () => {
    const data = {
      terms_and_conditions: {
        deal_comparison: ['Principal|USD 10m|USD 8m', 'Tenor|5Y|3Y'],
      },
    };
    const out = finalizePayload(1, data);
    expect(out.terms_and_conditions.deal_comparison_rows).toEqual([
      { term: 'Principal', proposed_deal: 'USD 10m', previous_deal: 'USD 8m' },
      { term: 'Tenor', proposed_deal: '5Y', previous_deal: '3Y' },
    ]);
    expect(out.terms_and_conditions.deal_comparison).toBeUndefined();
  });

  test('facility_summary pipe-separated rows → structured objects', () => {
    const data = {
      facility_summary: {
        rows: ['1|Borrower A|HK|50|Yes|USD|5Y|TL|Refund Guarantee|Ship Mortgage|Parent Co'],
      },
    };
    const out = finalizePayload(1, data);
    const row = out.facility_summary.rows[0];
    expect(row.item_no).toBe(1);
    expect(row.borrower_full_name).toBe('Borrower A');
    expect(row.proposed_usd_m).toBe(50);
    expect(row.is_new).toBe(true);
    expect(row.currency).toBe('USD');
    expect(row.guarantor).toBe('Parent Co');
  });
});

describe('§1 expandPayload roundtrip', () => {
  test('deal_comparison_rows objects → deal_comparison pipe strings → objects again', () => {
    const rows = [
      { term: 'Margin', proposed_deal: '1.50%', previous_deal: '1.75%' },
    ];
    const data = { terms_and_conditions: { deal_comparison_rows: rows } };
    const expanded = expandPayload(1, data);
    // expandPayload converts rows back to pipe strings for the form
    const strings = expanded.terms_and_conditions.deal_comparison;
    expect(Array.isArray(strings)).toBe(true);
    expect(strings[0]).toContain('Margin');
    expect(strings[0]).toContain('1.50%');
    expect(strings[0]).toContain('1.75%');
    // Now run finalizePayload on the expanded form — should produce original structure
    const re = finalizePayload(1, expanded);
    expect(re.terms_and_conditions.deal_comparison_rows[0].term).toBe('Margin');
    expect(re.terms_and_conditions.deal_comparison_rows[0].proposed_deal).toBe('1.50%');
    expect(re.terms_and_conditions.deal_comparison_rows[0].previous_deal).toBe('1.75%');
  });
});

// ── §2 risks ─────────────────────────────────────────────────────────────────

describe('§2 finalizePayload', () => {
  test('risk flat fields → risks array with correct keys', () => {
    const data = {
      '2E_risk_and_mitigants': {
        risk_1_title: 'Market Risk',
        risk_1_level: 'High',
        risk_1_risk_bullets: 'Bullet A\nBullet B',
        risk_1_mitigant_bullets: 'Mitigation 1',
      },
    };
    const out = finalizePayload(2, data);
    const risks = out['2E_risk_and_mitigants'].risks;
    expect(Array.isArray(risks)).toBe(true);
    expect(risks[0].risk_no).toBe(1);
    expect(risks[0].level).toBe('High');
    expect(risks[0].title).toBe('Market Risk');
    expect(risks[0].risk_bullets).toEqual(['Bullet A', 'Bullet B']);
    expect(risks[0].mitigant_bullets).toEqual(['Mitigation 1']);
  });

  test('output key is "risks" not "risk_factors"', () => {
    const data = { '2E_risk_and_mitigants': { risk_1_title: 'T', risk_1_level: 'Low', risk_1_risk_bullets: '', risk_1_mitigant_bullets: '' } };
    const out = finalizePayload(2, data);
    expect(out['2E_risk_and_mitigants'].risks).toBeDefined();
    expect(out['2E_risk_and_mitigants'].risk_factors).toBeUndefined();
  });
});

describe('§2 expandPayload roundtrip', () => {
  test('risks array → flat risk_N_* fields → risks array again', () => {
    const original = {
      '2E_risk_and_mitigants': {
        risks: [
          {
            risk_no: 1, level: 'Medium', title: 'Rate Risk',
            risk_bullets: ['Rate rises', 'Cost pressure'],
            mitigant_bullets: ['Fixed rate', 'Hedge'],
          },
        ],
        additional_risk_factors_from_previous: [],
      },
    };
    const expanded = expandPayload(2, original);
    expect(expanded['2E_risk_and_mitigants']['risk_1_title']).toBe('Rate Risk');
    expect(expanded['2E_risk_and_mitigants']['risk_1_level']).toBe('Medium');
    expect(expanded['2E_risk_and_mitigants']['risk_1_risk_bullets']).toContain('Rate rises');

    const restored = finalizePayload(2, expanded);
    expect(restored['2E_risk_and_mitigants'].risks[0].title).toBe('Rate Risk');
    expect(restored['2E_risk_and_mitigants'].risks[0].level).toBe('Medium');
    expect(restored['2E_risk_and_mitigants'].risks[0].risk_bullets).toContain('Rate rises');
  });
});

// ── §3 external ratings ───────────────────────────────────────────────────────

describe('§3 finalizePayload', () => {
  test('ratings pipe rows → {entity_abbrev, sp, moodys, fitch}', () => {
    const data = {
      '3A_external_ratings': {
        ratings: ['EVERGREEN|BBB+|Baa1|NR'],
      },
    };
    const out = finalizePayload(3, data);
    const row = out['3A_external_ratings'].ratings[0];
    expect(row.entity_abbrev).toBe('EVERGREEN');
    expect(row.sp).toBe('BBB+');
    expect(row.moodys).toBe('Baa1');
    expect(row.fitch).toBe('NR');
  });

  test('3C para_1_msr_mapping_verbatim synced to primary_paragraph_verbatim', () => {
    const data = {
      '3C_mas_612': { para_1_msr_mapping_verbatim: 'The borrower is classified as PASS.' },
    };
    const out = finalizePayload(3, data);
    expect(out['3C_mas_612'].primary_paragraph_verbatim).toBe('The borrower is classified as PASS.');
  });
});

describe('§3 expandPayload roundtrip', () => {
  test('ratings objects → pipe strings → objects again', () => {
    const original = {
      '3A_external_ratings': {
        ratings: [{ entity_abbrev: 'YML', sp: 'BB', moodys: 'Ba2', fitch: 'NR' }],
      },
    };
    const expanded = expandPayload(3, original);
    // After expand the ratings are still objects (expandPayload doesn't convert back to strings)
    // finalizePayload is idempotent on already-object arrays
    const restored = finalizePayload(3, expanded);
    expect(restored['3A_external_ratings'].ratings[0].entity_abbrev).toBe('YML');
    expect(restored['3A_external_ratings'].ratings[0].sp).toBe('BB');
  });

  test('3C primary_paragraph_verbatim restores to form key', () => {
    const original = {
      '3C_mas_612': {
        primary_paragraph_verbatim: 'Pass classification text.',
        grade: 'PASS',
      },
    };
    const expanded = expandPayload(3, original);
    expect(expanded['3C_mas_612'].para_1_msr_mapping_verbatim).toBe('Pass classification text.');
  });
});

// ── §4 management & fleet ─────────────────────────────────────────────────────

describe('§4 finalizePayload', () => {
  test('4C_management flat fields → array', () => {
    const data = {
      '4C_management': {
        ceo_name: 'Alice Tan', ceo_title: 'CEO', ceo_background: '20 years shipping',
        cfo_name: 'Bob Lee', cfo_title: 'CFO', cfo_background: '15 years finance',
      },
    };
    const out = finalizePayload(4, data);
    expect(Array.isArray(out['4C_management'])).toBe(true);
    expect(out['4C_management'][0].name).toBe('Alice Tan');
    expect(out['4C_management'][1].name).toBe('Bob Lee');
  });

  test('4F_fleet flat counts → {fleet_breakdown}', () => {
    const data = {
      '4F_fleet': {
        owned_vessel_count: 10, owned_total_teu: 50000,
        chartered_vessel_count: 5, chartered_total_teu: 20000,
      },
    };
    const out = finalizePayload(4, data);
    expect(out['4F_fleet'].fleet_breakdown).toBeDefined();
    expect(out['4F_fleet'].fleet_breakdown[0].category).toBe('Owned');
    expect(out['4F_fleet'].fleet_breakdown[0].vessel_count).toBe(10);
  });
});

describe('§4 expandPayload roundtrip', () => {
  test('4C_management array → flat → array again', () => {
    const original = {
      '4C_management': [
        { name: 'Alice Tan', title: 'CEO', years_experience: null, background: 'Shipping expert' },
        { name: 'Bob Lee', title: 'CFO', years_experience: null, background: 'Finance expert' },
      ],
    };
    const expanded = expandPayload(4, original);
    expect(expanded['4C_management'].ceo_name).toBe('Alice Tan');
    expect(expanded['4C_management'].cfo_name).toBe('Bob Lee');

    const restored = finalizePayload(4, expanded);
    expect(restored['4C_management'][0].name).toBe('Alice Tan');
    expect(restored['4C_management'][1].name).toBe('Bob Lee');
  });

  test('4F_fleet fleet_breakdown → flat → fleet_breakdown again', () => {
    const original = {
      '4F_fleet': {
        fleet_breakdown: [
          { category: 'Owned', vessel_count: 8, total_teu: 40000 },
          { category: 'Chartered-in', vessel_count: 4, total_teu: 16000 },
        ],
      },
    };
    const expanded = expandPayload(4, original);
    expect(expanded['4F_fleet'].owned_vessel_count).toBe(8);
    expect(expanded['4F_fleet'].chartered_vessel_count).toBe(4);

    const restored = finalizePayload(4, expanded);
    expect(restored['4F_fleet'].fleet_breakdown[0].category).toBe('Owned');
    expect(restored['4F_fleet'].fleet_breakdown[0].vessel_count).toBe(8);
  });
});

// ── §6 milestones ─────────────────────────────────────────────────────────────

describe('§6 finalizePayload', () => {
  test('milestone flat fields → {milestones, commentary_banking_act_33_3}', () => {
    const data = {
      '6D_milestones': {
        m1_name: 'Keel Laying', m1_date: '2025-06-01', m1_pct: 20, m1_amount_usd_m: 10,
        banking_act_commentary: 'Complies with §33(3).',
      },
    };
    const out = finalizePayload(6, data);
    expect(out['6D_milestones'].milestones[0].milestone).toBe('Keel Laying');
    expect(out['6D_milestones'].milestones[0].expected_date).toBe('2025-06-01');
    expect(out['6D_milestones'].commentary_banking_act_33_3).toBe('Complies with §33(3).');
  });

  test('commentary_banking_act_33_3 (ETL key) also accepted on input', () => {
    const data = {
      '6D_milestones': {
        m1_name: 'Steel Cutting', m1_date: '2025-04-01', m1_pct: 10, m1_amount_usd_m: 5,
        commentary_banking_act_33_3: 'ETL populated this.',
      },
    };
    const out = finalizePayload(6, data);
    expect(out['6D_milestones'].commentary_banking_act_33_3).toBe('ETL populated this.');
  });
});

describe('§6 expandPayload roundtrip', () => {
  test('commentary_banking_act_33_3 survives round-trip', () => {
    const original = {
      '6D_milestones': {
        milestones: [
          { no: 1, milestone: 'Keel Laying', expected_date: '2025-06-01',
            pct_of_contract: 20, amount_usd_m: 10, rg_in_force: '✅' },
        ],
        commentary_banking_act_33_3: 'Banking act compliance confirmed.',
      },
    };
    const expanded = expandPayload(6, original);
    expect(expanded['6D_milestones'].banking_act_commentary).toBe('Banking act compliance confirmed.');
    expect(expanded['6D_milestones'].m1_name).toBe('Keel Laying');

    const restored = finalizePayload(6, expanded);
    expect(restored['6D_milestones'].commentary_banking_act_33_3).toBe('Banking act compliance confirmed.');
    expect(restored['6D_milestones'].milestones[0].milestone).toBe('Keel Laying');
  });
});

// ── §7 financials ─────────────────────────────────────────────────────────────

describe('§7 finalizePayload', () => {
  test('entities object → array with Borrower role', () => {
    const data = {
      entities_to_analyze: {
        borrower_name: 'Evergreen Marine', borrower_currency: 'USD',
        borrower_unit: 'millions', guarantor_exists: false,
      },
    };
    const out = finalizePayload(7, data);
    expect(Array.isArray(out.entities_to_analyze)).toBe(true);
    expect(out.entities_to_analyze[0].role).toBe('Borrower');
    expect(out.entities_to_analyze[0].name).toBe('Evergreen Marine');
  });

  test('income_statement fields structured by FY', () => {
    const data = {
      '7A_borrower_financials': {
        reporting_currency: 'USD',
        unit: 'millions',
        revenue_fy2024: 1000,
        ebitda_fy2024: 200,
        net_income_fy2024: 100,
      },
    };
    const out = finalizePayload(7, data);
    const fa = out['7A_borrower_financials'];
    expect(fa.income_statement).toBeDefined();
    expect(fa.income_statement.FY2024).toBeDefined();
    expect(fa.income_statement.FY2024.revenue).toBe(1000);
    expect(fa.income_statement.FY2024.net_income).toBe(100);
  });
});

describe('§7 expandPayload roundtrip', () => {
  test('income_statement FY2024 → flat fields → FY2024 again', () => {
    const original = {
      '7A_borrower_financials': {
        reporting_currency: 'USD',
        unit: 'millions',
        auditor: 'Big 4 CPA',
        income_statement: { FY2024: { revenue: 800, ebitda: 160, net_income: 80 } },
        balance_sheet: { FY2024: { total_assets: 2000, total_equity: 500 } },
        cash_flow: { FY2024: { ocf: 120, capex: 40 } },
      },
    };
    const expanded = expandPayload(7, original);
    expect(expanded['7A_borrower_financials'].revenue_fy2024).toBe(800);
    expect(expanded['7A_borrower_financials'].bs_total_assets).toBe(2000);
    expect(expanded['7A_borrower_financials'].cf_ocf).toBe(120);
    expect(expanded['7A_borrower_financials'].auditor).toBe('Big 4 CPA');

    const restored = finalizePayload(7, expanded);
    expect(restored['7A_borrower_financials'].income_statement.FY2024.revenue).toBe(800);
    expect(restored['7A_borrower_financials'].balance_sheet.FY2024.total_assets).toBe(2000);
    expect(restored['7A_borrower_financials'].cash_flow.FY2024.ocf).toBe(120);
    expect(restored['7A_borrower_financials'].reporting_currency).toBe('USD');
  });
});

// ── §9 checklist ─────────────────────────────────────────────────────────────

describe('§9 finalizePayload', () => {
  test('4-col pipe string → {category, item, response, remarks}', () => {
    const data = {
      '9A_checklist': {
        items: ['KYC|Sanction check|✓ Cleared|Verified 2025-01'],
      },
    };
    const out = finalizePayload(9, data);
    const item = out['9A_checklist'].items[0];
    expect(item.category).toBe('KYC');
    expect(item.item).toBe('Sanction check');
    expect(item.response).toBe('✓ Cleared');
    expect(item.remarks).toBe('Verified 2025-01');
  });

  test('legacy 3-col string → {category:"", item, response, remarks}', () => {
    const data = {
      '9A_checklist': {
        items: ['Sanction check|✓ Cleared|Verified'],
      },
    };
    const out = finalizePayload(9, data);
    const item = out['9A_checklist'].items[0];
    expect(item.category).toBe('');
    expect(item.item).toBe('Sanction check');
    expect(item.response).toBe('✓ Cleared');
  });
});

describe('§9 expandPayload roundtrip', () => {
  test('checklist objects → pipe strings → objects again', () => {
    const original = {
      '9A_checklist': {
        items: [
          { category: 'AML', item: 'Customer DD', response: 'Completed', remarks: '2025-03' },
        ],
      },
    };
    const expanded = expandPayload(9, original);
    expect(typeof expanded['9A_checklist'].items[0]).toBe('string');
    expect(expanded['9A_checklist'].items[0]).toContain('AML');

    const restored = finalizePayload(9, expanded);
    expect(restored['9A_checklist'].items[0].category).toBe('AML');
    expect(restored['9A_checklist'].items[0].item).toBe('Customer DD');
    expect(restored['9A_checklist'].items[0].response).toBe('Completed');
    expect(restored['9A_checklist'].items[0].remarks).toBe('2025-03');
  });
});

// ── §5 security ──────────────────────────────────────────────────────────────

describe('§5 finalizePayload', () => {
  test('security instruments flat → {is_secured, security_instruments}', () => {
    const data = {
      '5A_security_overview': {
        is_secured: true,
        instr_1_instrument: 'Refund Guarantee',
        instr_1_description: 'Pre-delivery RG',
        instr_2_instrument: 'Ship Mortgage',
        instr_2_description: 'Post-delivery',
      },
    };
    const out = finalizePayload(5, data);
    const sa = out['5A_security_overview'];
    expect(sa.is_secured).toBe(true);
    expect(sa.security_instruments).toHaveLength(2);
    expect(sa.security_instruments[0].instrument).toBe('Refund Guarantee');
    expect(sa.security_instruments[1].instrument).toBe('Ship Mortgage');
  });

  test('5D_insurance flat → {applicable, instruments}', () => {
    const data = {
      '5D_insurance': {
        applicable: true,
        hm_insurer: 'Lloyd\'s', hm_insured_value_usd_m: 30,
        pi_insurer: 'Gard', pi_insured_value_usd_m: 0,
      },
    };
    const out = finalizePayload(5, data);
    const ins = out['5D_insurance'];
    expect(ins.applicable).toBe(true);
    expect(ins.instruments).toHaveLength(2);
    expect(ins.instruments[0].type).toBe('Hull & Machinery');
    expect(ins.instruments[1].type).toBe('P&I');
  });
});

// ── §8 banking charges ────────────────────────────────────────────────────────

describe('§8 finalizePayload', () => {
  test('top-level charge fields → summary sub-object', () => {
    const data = {
      '8A_acra_banking_charges': {
        total_charges: 10,
        active_charges: 3,
        satisfied_charges: 7,
        total_active_usd_m: 50,
        cub_charge_count: 2,
        cub_total_usd_m: 30,
      },
    };
    const out = finalizePayload(8, data);
    const a = out['8A_acra_banking_charges'];
    expect(a.summary.total_charges).toBe(10);
    expect(a.summary.active_charges).toBe(3);
    expect(a.summary.cub_total_usd_m).toBe(30);
  });
});

describe('§8 expandPayload roundtrip', () => {
  test('summary object fields promoted to top-level → survive round-trip', () => {
    const original = {
      '8A_acra_banking_charges': {
        summary: { total_charges: 5, active_charges: 2, satisfied_charges: 3,
                   total_active_usd_m: 20, cub_charge_count: 1, cub_total_usd_m: 10 },
      },
    };
    const expanded = expandPayload(8, original);
    // expandPayload promotes summary fields to top-level for form display
    expect(expanded['8A_acra_banking_charges'].total_charges).toBe(5);

    const restored = finalizePayload(8, expanded);
    expect(restored['8A_acra_banking_charges'].summary.total_charges).toBe(5);
  });
});

// ── §10 projections ───────────────────────────────────────────────────────────

describe('§10 finalizePayload', () => {
  test('fleet_growth pipe rows → structured objects', () => {
    const data = {
      '10B_fleet_growth': {
        rows: ['2024|2.5|3.8|45|66'],
      },
    };
    const out = finalizePayload(10, data);
    const row = out['10B_fleet_growth'].rows[0];
    expect(row.year_label).toBe('2024');
    expect(row.owned_fleet_teu_m).toBe(2.5);
    expect(row.total_fleet_teu_m).toBe(3.8);
    expect(row.total_vessels).toBe(45);
  });
});

// ── Idempotency: double-finalizing must not corrupt data ──────────────────────

describe('idempotency', () => {
  test('finalizePayload §2 is safe to call on already-structured data', () => {
    const once = finalizePayload(2, {
      '2E_risk_and_mitigants': {
        risk_1_title: 'T', risk_1_level: 'Low',
        risk_1_risk_bullets: 'B', risk_1_mitigant_bullets: 'M',
      },
    });
    // Second call on already-finalized data should not throw
    expect(() => finalizePayload(2, once)).not.toThrow();
  });

  test('expandPayload then finalizePayload §6 preserves milestone count', () => {
    const original = {
      '6D_milestones': {
        milestones: [
          { no: 1, milestone: 'A', expected_date: '2025-01-01', pct_of_contract: 20, amount_usd_m: 5, rg_in_force: '✅' },
          { no: 2, milestone: 'B', expected_date: '2025-06-01', pct_of_contract: 30, amount_usd_m: 8, rg_in_force: '✅' },
        ],
        commentary_banking_act_33_3: 'OK',
      },
    };
    const expanded = expandPayload(6, original);
    const restored = finalizePayload(6, expanded);
    expect(restored['6D_milestones'].milestones).toHaveLength(2);
    expect(restored['6D_milestones'].milestones[1].milestone).toBe('B');
  });
});
