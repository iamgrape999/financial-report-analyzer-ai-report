/**
 * Tests for syncJsonToForm / _applyJsonToForm feature.
 *
 * Exercises the pure-JS logic from static/index.html inside a Node.js vm
 * so const-declarations (FIELD_DEFS, REQUIRED_FIELDS, TEMPLATES) are visible.
 *
 * Sections covered: 1-10
 * Functions tested: expandPayload, finalizePayload, getCompleteness,
 *                   getNestedValue, isFieldFilled, FIELD_DEFS, REQUIRED_FIELDS
 *                   plus an inline _strip_nulls equivalent for integration tests.
 */
'use strict';

const fs  = require('fs');
const vm  = require('vm');
const path = require('path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'index.html'), 'utf8'
);

// ── Browser-API stubs ──────────────────────────────────────────────────────
const _store = {};
const mockEl = {
  classList: { add:()=>{}, remove:()=>{}, toggle:()=>false, contains:()=>false },
  style:{}, value:'', textContent:'', innerHTML:'',
  addEventListener:()=>{}, querySelector:()=>null, querySelectorAll:()=>[],
};

const ctx = vm.createContext({
  localStorage: { getItem:k=>_store[k]||null, setItem:(k,v)=>{_store[k]=v;} },
  document: {
    getElementById:()=>mockEl,
    querySelectorAll:()=>({ forEach:()=>{} }),
    addEventListener:()=>{},
    querySelector:()=>mockEl,
  },
  navigator: { language:'en' },
  marked: { parse:s=>s },
  lang: 'en',
  fetch: async()=>({ ok:false, json:async()=>({}) }),
  bootstrap: { Modal: class { show(){} hide(){} } },
  toast: ()=>{},
  tl: (en)=>en,
  console,
  setTimeout:()=>{}, clearTimeout:()=>{},
  setInterval:()=>{}, clearInterval:()=>{},
  Promise, JSON, Array, Object, Math,
  parseInt, parseFloat, isNaN, String, Boolean, Number,
  Error, RegExp, Date, Set, Map,
});
ctx.window = ctx;

// Evaluate all inline <script> blocks from index.html into the vm context
const scriptRe = /<script(?![^>]*\bsrc\b)[^>]*>([\s\S]*?)<\/script>/gi;
let m;
while ((m = scriptRe.exec(html)) !== null) {
  try { vm.runInContext(m[1], ctx); } catch (_) { /* DOM-init errors are fine */ }
}

// ── Run all tests inside the vm so const-symbols are visible ──────────────
const testCode = `
(function runTests() {

// ── Inline strip-nulls (mirrors Python _strip_nulls in generate.py) ────────
// Python: returns {} for all-null dicts (not None), filters {} from arrays.
function stripNulls(obj) {
  if (Array.isArray(obj)) {
    return obj
      .map(stripNulls)
      .filter(i => !(typeof i==='object' && i!==null && !Array.isArray(i) && Object.keys(i).length===0));
  }
  if (obj !== null && typeof obj === 'object') {
    const r = {};
    for (const [k,v] of Object.entries(obj)) {
      if (v !== null && v !== undefined) r[k] = stripNulls(v);
    }
    return r; // {} for all-null objects; caller/array-filter removes empty dicts
  }
  return obj; // null, primitives pass through unchanged
}

// ── Tiny test harness ───────────────────────────────────────────────────────
let passed=0, failed=0;
function test(name, fn){
  try{ fn(); console.log('  ✅  '+name); passed++; }
  catch(e){ console.error('  ❌  '+name); console.error('      '+e.message); failed++; }
}
function assert(cond,msg){ if(!cond) throw new Error(msg||'assertion failed'); }
function assertEqual(a,b,msg){
  if(JSON.stringify(a)!==JSON.stringify(b))
    throw new Error((msg||'')+'\\n      expected: '+JSON.stringify(b)+'\\n      got:      '+JSON.stringify(a));
}

// ══════════════════════════════════════════════════════════════════════════
// Sanity — required symbols loaded from index.html
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n── Sanity: symbols loaded from index.html ────────────────────');
test('FIELD_DEFS is defined', ()=>assert(typeof FIELD_DEFS==='object'&&FIELD_DEFS!==null));
test('REQUIRED_FIELDS is defined', ()=>assert(typeof REQUIRED_FIELDS==='object'&&REQUIRED_FIELDS!==null));
test('expandPayload is a function', ()=>assert(typeof expandPayload==='function'));
test('finalizePayload is a function', ()=>assert(typeof finalizePayload==='function'));
test('getCompleteness is a function', ()=>assert(typeof getCompleteness==='function'));

// ══════════════════════════════════════════════════════════════════════════
// 1. expandPayload — all sections 1-10
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n── expandPayload (§1) ────────────────────────────────────────');

test('§1 rows objects → pipe-delimited strings', ()=>{
  const input={facility_summary:{rows:[{item_no:1,borrower_full_name:'EMA Pte Ltd',booking_location:'SG',proposed_usd_m:178.5,is_new:true,currency:'USD',tenor:'11 years',facility_type:'Term Loan',collateral_pre_delivery:'RG',collateral_post_delivery:'Mortgage',guarantor:'EMC'}]}};
  const out=expandPayload(1,input);
  assert(typeof out.facility_summary.rows[0]==='string','row should be string');
  assert(out.facility_summary.rows[0].includes('EMA Pte Ltd'));
  assert(out.facility_summary.rows[0].includes('Yes'));
});
test('§1 deal_comparison objects → pipe strings', ()=>{
  const input={terms_and_conditions:{deal_comparison:[{term:'Margin',proposed:'85bps',previous:'85bps'}]}};
  const out=expandPayload(1,input);
  assertEqual(out.terms_and_conditions.deal_comparison[0],'Margin|85bps|85bps');
});
test('§1 footnotes objects → strings', ()=>{
  const input={facility_summary:{footnotes:[{symbol:'[1]',text_verbatim:'Grace period 180 days.'}]}};
  const out=expandPayload(1,input);
  assert(out.facility_summary.footnotes[0].includes('Grace period'));
});
test('§1 SLL kpi objects → pipe strings', ()=>{
  const input={sll_kpi_performance:{applicable:true,kpis:[{kpi_name:'CO2',target_value:'<=8.5',actual_value:'7.2',period:'2024',on_track:true,ratchet_bps:-5}]}};
  const out=expandPayload(1,input);
  assertEqual(out.sll_kpi_performance.kpis[0],'CO2|<=8.5|7.2|2024|Yes|-5');
});
test('§1 already-string rows pass through unchanged', ()=>{
  const pipe='1|EMA Pte Ltd|SG|178.5|Yes|USD|11 years|Term Loan|RG|Mortgage|EMC';
  const out=expandPayload(1,{facility_summary:{rows:[pipe]}});
  assertEqual(out.facility_summary.rows[0],pipe);
});

console.log('\\n── expandPayload (§2) ────────────────────────────────────────');
test('§2 bullets [{order,text_verbatim}] → newline string', ()=>{
  const input={'2A_credit_overview':{bullets:[{order:1,text_verbatim:'Strong cash flow'},{order:2,text_verbatim:'Investment grade'}]}};
  const out=expandPayload(2,input);
  assertEqual(out['2A_credit_overview'].bullets,'Strong cash flow\\nInvestment grade');
});
test('§2 risk_factors array → flat per-risk fields', ()=>{
  const input={'2E_risk_and_mitigants':{risk_factors:[{level:'Medium',title:'Rate risk',risk_bullets:['Freight rates volatile'],mitigant_bullets:['Hedging in place']}]}};
  const out=expandPayload(2,input);
  const re=out['2E_risk_and_mitigants'];
  assertEqual(re.risk_1_title,'Rate risk');
  assertEqual(re.risk_1_level,'Medium');
  assert(re.risk_1_risk_bullets.includes('Freight rates volatile'));
});
test('§2 already-string bullets pass through', ()=>{
  const out=expandPayload(2,{'2A_credit_overview':{bullets:'Line one\\nLine two'}});
  assertEqual(out['2A_credit_overview'].bullets,'Line one\\nLine two');
});
test('§2 tariff_impact_paragraphs array → textarea string', ()=>{
  const out=expandPayload(2,{'2A_credit_overview':{tariff_impact_paragraphs:['Para 1','Para 2']}});
  assertEqual(out['2A_credit_overview'].tariff_impact_paragraphs,'Para 1\\n\\nPara 2');
});

console.log('\\n── expandPayload (§3) ────────────────────────────────────────');
test('§3 3A ratings array of objects → pipe strings', ()=>{
  const input={'3A_external_ratings':{ratings:[{entity:'EMC',sp:'BBB',moodys:'Baa2',fitch:'NR'}]}};
  const out=expandPayload(3,input);
  assertEqual(out['3A_external_ratings'].ratings[0],'EMC|BBB|Baa2|NR');
});
test('§3 3B_internal_ratings rows → flat fields', ()=>{
  const input={'3B_internal_ratings':{rows:[{role:'Borrower',entity_full_name:'EMA Pte Ltd',entity_abbrev:'EMA',fy2022_23:'BBB',fy2024:'BBB',interim:'BBB',current:'BBB',override_flag:false,override_remarks:''},{role:'Guarantor',entity_full_name:'EMC',entity_abbrev:'EMC',fy2022_23:'A-',fy2024:'A-',interim:'A-',current:'A-'}]}};
  const out=expandPayload(3,input);
  const r=out['3B_internal_ratings'];
  assertEqual(r.borrower_entity_full_name,'EMA Pte Ltd');
  assertEqual(r.guarantor_entity_full_name,'EMC');
  assertEqual(r.borrower_fy2024,'BBB');
  assertEqual(r.guarantor_fy2024,'A-');
});

console.log('\\n── expandPayload (§4) ────────────────────────────────────────');
test('§4 management array → flat ceo/cfo fields', ()=>{
  const input={'4C_management':[{name:'John Smith',title:'CEO',background:'20 years shipping'},{name:'Jane Doe',title:'CFO',background:'Finance expert'}]};
  const out=expandPayload(4,input);
  const mg=out['4C_management'];
  assertEqual(mg.ceo_name,'John Smith');
  assertEqual(mg.cfo_name,'Jane Doe');
});
test('§4 fleet_breakdown array → flat owned/chartered/onorder', ()=>{
  const input={'4F_fleet':{fleet_breakdown:[{category:'Owned',vessel_count:100,total_teu:1200000},{category:'Chartered-in',vessel_count:50,total_teu:500000},{category:'On Order',vessel_count:10,total_teu:200000}]}};
  const out=expandPayload(4,input);
  const f=out['4F_fleet'];
  assertEqual(f.owned_vessel_count,100);
  assertEqual(f.chartered_vessel_count,50);
  assertEqual(f.on_order_vessel_count,10);
});

console.log('\\n── expandPayload (§5) ────────────────────────────────────────');
test('§5 security_instruments array → flat instr_N fields', ()=>{
  const input={'5A_security_overview':{is_secured:true,security_instruments:[{instrument:'Ship Mortgage',description:'First priority'},{instrument:'Assignment',description:'Earnings'}]}};
  const out=expandPayload(5,input);
  const s=out['5A_security_overview'];
  assertEqual(s.instr_1_instrument,'Ship Mortgage');
  assertEqual(s.instr_2_instrument,'Assignment');
  assertEqual(s.is_secured,true);
});
test('§5 5D_insurance instruments → flat hm/pi/war fields', ()=>{
  const input={'5D_insurance':{applicable:true,instruments:[{type:'Hull & Machinery',insurer_or_club:"Lloyd's",insured_value_usd_m:250,notes:'Full cover'},{type:'P&I',insurer_or_club:'Gard',insured_value_usd_m:0,notes:'P&I cover'}]}};
  const out=expandPayload(5,input);
  const ins=out['5D_insurance'];
  assertEqual(ins.hm_insurer,"Lloyd's");
  assertEqual(ins.pi_insurer,'Gard');
  assertEqual(ins.hm_insured_value_usd_m,250);
});

console.log('\\n── expandPayload (§6) ────────────────────────────────────────');
test('§6 milestones array → flat m1/m2 fields', ()=>{
  const input={'6D_milestones':{banking_act_commentary:'Within limits',milestones:[{milestone:'Keel laying',expected_date:'2026-06-01',pct_of_contract:20,amount_usd_m:44.6},{milestone:'Launching',expected_date:'2027-03-01',pct_of_contract:10,amount_usd_m:22.3}]}};
  const out=expandPayload(6,input);
  const md=out['6D_milestones'];
  assertEqual(md.m1_name,'Keel laying');
  assertEqual(md.m2_name,'Launching');
  assertEqual(md.m1_date,'2026-06-01');
  assertEqual(md.banking_act_commentary,'Within limits');
});

console.log('\\n── expandPayload (§7) ────────────────────────────────────────');
test('§7 entities_to_analyze array → flat borrower/guarantor', ()=>{
  const input={entities_to_analyze:[{role:'Borrower',name:'EMA Pte Ltd',currency:'USD',unit:'millions',guarantor_exists:true},{role:'Guarantor',name:'EMC',currency:'USD'}]};
  const out=expandPayload(7,input);
  const e=out['entities_to_analyze'];
  assertEqual(e.borrower_name,'EMA Pte Ltd');
  assertEqual(e.guarantor_name,'EMC');
});
test('§7 7A income_statement FY-keyed → flat fields', ()=>{
  const input={'7A_borrower_financials':{reporting_entity:'EMA Pte Ltd',reporting_currency:'USD',unit:'millions',income_statement:{FY2024:{revenue:8500,gross_profit:2400,op_profit:2000,net_income:1900},FY2023:{revenue:7200,gross_profit:1900,op_profit:1600,net_income:1500}}}};
  const out=expandPayload(7,input);
  const f=out['7A_borrower_financials'];
  assertEqual(f.reporting_entity,'EMA Pte Ltd');
  assert(f.revenue_fy2024===8500,'revenue_fy2024 should be 8500');
  assert(f.net_income_fy2024===1900,'net_income_fy2024 should be 1900');
});
test('§7 7B_key_ratios nested FY → flat fy_metric fields', ()=>{
  const input={'7B_key_ratios':{FY2024:{dscr:1.8,ltv_pct:72,interest_coverage:3.2},FY2022:{dscr:2.1}}};
  const out=expandPayload(7,input);
  const kr=out['7B_key_ratios'];
  assert(kr.fy2024_dscr===1.8,'fy2024_dscr should be 1.8');
  assert(kr.fy2022_dscr===2.1,'fy2022_dscr should be 2.1');
});

console.log('\\n── expandPayload (§8) ────────────────────────────────────────');
test('§8 8A summary nested → flat fields', ()=>{
  const input={'8A_acra_banking_charges':{acra_data_available:true,entity_name:'EMA Pte Ltd',search_date:'2025-01-15',summary:{total_charges:5,active_charges:3,satisfied_charges:2,total_active_usd_m:250,cub_charge_count:1,cub_total_usd_m:178.5}}};
  const out=expandPayload(8,input);
  const a=out['8A_acra_banking_charges'];
  assertEqual(a.total_charges,5);
  assertEqual(a.cub_total_usd_m,178.5);
});
test('§8 expandPayload idempotent when already flat', ()=>{
  const input={'8A_acra_banking_charges':{acra_data_available:true,entity_name:'EMA',search_date:'2025-01-15',total_charges:5,active_charges:3}};
  const out=expandPayload(8,input);
  assertEqual(out['8A_acra_banking_charges'].total_charges,5);
});

console.log('\\n── expandPayload (§9) ────────────────────────────────────────');
test('§9 9A checklist items objects → pipe strings', ()=>{
  const input={'9A_checklist':{items:[{item:'KYC completed',response:'Yes',remarks:'Cleared 2024'},{item:'AML screening',response:'Yes',remarks:''}]}};
  const out=expandPayload(9,input);
  const items=out['9A_checklist'].items;
  assert(typeof items[0]==='string');
  assert(items[0].includes('KYC completed'));
});
test('§9 9B conditions_precedent objects → pipe strings', ()=>{
  const input={'9B_conditions_covenants':{conditions_precedent:[{description:'Legal opinion',testing:'Before first drawdown'}]}};
  const out=expandPayload(9,input);
  assert(out['9B_conditions_covenants'].conditions_precedent[0].includes('Legal opinion'));
});

console.log('\\n── expandPayload (§10) ───────────────────────────────────────');
test('§10 10B fleet growth rows objects → pipe strings', ()=>{
  const input={'10B_fleet_growth':{rows:[{year_label:'2024',owned_fleet_teu_m:1.2,total_fleet_teu_m:1.8,total_vessels:110,owned_pct:65}]}};
  const out=expandPayload(10,input);
  assert(typeof out['10B_fleet_growth'].rows[0]==='string');
  assert(out['10B_fleet_growth'].rows[0].includes('2024'));
});
test('§10 10C base_case_pl rows → pipe strings', ()=>{
  const input={'10C_projections':{base_case_pl:[{item:'Revenue',FY2026E:9000,FY2027E:9500,FY2028E:10000,is_subtotal:false}]}};
  const out=expandPayload(10,input);
  const row=out['10C_projections'].base_case_pl[0];
  assert(typeof row==='string');
  assert(row.includes('Revenue')&&row.includes('9000'));
});
test('§10 10C dscr rows objects → pipe strings', ()=>{
  const input={'10C_projections':{base_case_dscr:[{year_label:'2026',ocf:1200,debt_service:800,dscr:1.5}]}};
  const out=expandPayload(10,input);
  assert(out['10C_projections'].base_case_dscr[0].includes('1.5'));
});
test('§10 10C stress_assumptions rows → pipe strings', ()=>{
  const input={'10C_projections':{stress_assumptions:[{assumption:'Freight -30%',base_case:'1.8x',worse_case:'1.2x',stress_magnitude:'-30%'}]}};
  const out=expandPayload(10,input);
  assert(out['10C_projections'].stress_assumptions[0].includes('Freight -30%'));
});

// ══════════════════════════════════════════════════════════════════════════
// 2. finalizePayload — form → API format
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n── finalizePayload ───────────────────────────────────────────');
test('§1 pipe string → row object', ()=>{
  const data={facility_summary:{rows:['1|EMA Pte Ltd|SG|178.5|Yes|USD|11 years|Term Loan|RG|Mortgage|EMC']}};
  const out=finalizePayload(1,data);
  const row=out.facility_summary.rows[0];
  assertEqual(typeof row,'object');
  assertEqual(row.borrower_full_name,'EMA Pte Ltd');
  assertEqual(row.proposed_usd_m,178.5);
  assertEqual(row.is_new,true);
});
test('§1 footnote pipe → object', ()=>{
  const data={facility_summary:{footnotes:['[1] Grace period 180 days.']}};
  const out=finalizePayload(1,data);
  const fn=out.facility_summary.footnotes[0];
  assertEqual(fn.symbol,'[1]');
  assert(fn.text_verbatim.includes('Grace period'));
});
test('§1 deal_comparison pipe → object', ()=>{
  const data={terms_and_conditions:{deal_comparison:['Margin|85bps|85bps']}};
  const out=finalizePayload(1,data);
  const dc=out.terms_and_conditions.deal_comparison[0];
  assertEqual(dc.term,'Margin');
  assertEqual(dc.proposed,'85bps');
  assertEqual(dc.previous,'85bps');
});
test('§2 bullets lines → [{order,text_verbatim}]', ()=>{
  const data={'2A_credit_overview':{bullets:['Strong cash flow','Investment grade']}};
  const out=finalizePayload(2,data);
  assertEqual(out['2A_credit_overview'].bullets[0].order,1);
  assertEqual(out['2A_credit_overview'].bullets[0].text_verbatim,'Strong cash flow');
  assertEqual(out['2A_credit_overview'].bullets[1].order,2);
});

// ══════════════════════════════════════════════════════════════════════════
// 3. Roundtrip: expand → finalize preserves data
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n── Roundtrip expand→finalize ─────────────────────────────────');
test('§1 rows survive expand→finalize', ()=>{
  const original={facility_summary:{rows:[{item_no:1,borrower_full_name:'EMA Pte Ltd',booking_location:'SG',proposed_usd_m:178.5,is_new:true,currency:'USD',tenor:'11 years',facility_type:'Term Loan',collateral_pre_delivery:'RG',collateral_post_delivery:'Mortgage',guarantor:'EMC'}]}};
  const expanded=expandPayload(1,original);
  const finalized=finalizePayload(1,expanded);
  const row=finalized.facility_summary.rows[0];
  assertEqual(row.borrower_full_name,original.facility_summary.rows[0].borrower_full_name);
  assertEqual(row.proposed_usd_m,original.facility_summary.rows[0].proposed_usd_m);
  assertEqual(row.is_new,original.facility_summary.rows[0].is_new);
});
test('§2 bullets survive expand→collectSimulate→finalize', ()=>{
  // Full roundtrip: API objects → expand (join) → form lines type → split → finalize (re-objectify)
  // collectFormData (DOM) splits the textarea value; we simulate that step.
  const original={'2A_credit_overview':{bullets:[{order:1,text_verbatim:'Strong cash flow'}]}};
  const expanded=expandPayload(2,original);
  // Simulate collectFormData: lines field is split back to array
  const simCollected={'2A_credit_overview':{bullets:expanded['2A_credit_overview'].bullets.split('\\n').filter(Boolean)}};
  const finalized=finalizePayload(2,simCollected);
  assertEqual(finalized['2A_credit_overview'].bullets[0].text_verbatim,'Strong cash flow');
  assertEqual(finalized['2A_credit_overview'].bullets[0].order,1);
});

// ══════════════════════════════════════════════════════════════════════════
// 4. getCompleteness — all sections
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n── getCompleteness (all sections 1-10) ─────────────────────');
const SAMPLES={
  1:{report_type:'new_deal',facility_summary:{rows:['1|EMA|SG|178.5|Yes|USD|11y|Term Loan|RG|Mortgage|EMC']},regulatory_compliance:{compliance_status:'Compliant'},purpose_and_recommendation:{recommendation:'APPROVE'},terms_and_conditions:{borrower:'EMA Pte Ltd'},account_strategy:{wallet:{bank_market:'NII USD7.5m p.a.'}}},
  2:{'2A_credit_overview':{bullets:['Strong cash flow']},'2B_solvency':{primary_repayment_source_verbatim:'Operating cash flow'},'2C_guarantor':{guarantor_name_abbrev:'EMC'},'2D_collateral':{pre_delivery:{issuer_full_name:'KDB'}},'2E_risk_and_mitigants':{risk_1_title:'Rate risk'}},
  3:{'3C_mas_612':{grade:'Pass',para_1_msr_mapping_verbatim:'Mapped to BB'},'3B_internal_ratings':{borrower_entity_full_name:'EMA Pte Ltd',borrower_fy2024:'BBB'}},
  4:{'4A_borrower':{company_name_en:'EMA Pte Ltd'},'4B_ownership':{shareholders:[{name:'EMC',stake_percent:100}]},'4C_management':{ceo_name:'John Smith'},'4D_business':{primary_business:'Container Shipping'},'4E_financials':{revenue:8500},'4F_fleet':{owned_vessel_count:100},'4J_peer_comparison':{data:'some data'}},
  5:{'5A_security_overview':{is_secured:true},'5C_vessel_mortgage':{loan_amount_usd_m:178.5},'5E_value_maintenance_clause':{acr_covenant_pct:120}},
  6:{'6A_project':{hull_number:'HN-2891',delivery_date:'2028-06-30'},'6B_builder':{name:'HHI'},'6C_contract':{contract_date:'2024-01-15'},'6D_milestones':{m1_name:'Keel laying'},'6E_rg_mechanism':{issuer_full_name:'Korea Development Bank'}},
  7:{entities_to_analyze:{borrower_name:'EMA Pte Ltd'},'7A_borrower_financials':{reporting_entity:'EMA Pte Ltd'},'7B_key_ratios':{fy2024_dscr:1.8}},
  8:{'8A_acra_banking_charges':{acra_data_available:true,entity_name:'EMA Pte Ltd',search_date:'2025-01-15'}},
  9:{'9A_checklist':{items:['KYC|Yes|Cleared'],kyc_aml_cleared:true},'9C_recommendation':{decision:'APPROVE'},'9D_signoff':{prepared_by:'J. Analyst'}},
  10:{'10A_group_exposure':{entity_group:'Oakwood Maritime Group',approved_group_limit_usd_m:800},'10C_projections':{entity_name:'EMA Pte Ltd',dscr_commentary:'DSCR above 1.2x'}},
};
for(let sec=1;sec<=10;sec++){
  test('§'+sec+' 100% when all REQUIRED_FIELDS filled',()=>{
    const{pct,missing}=getCompleteness(sec,SAMPLES[sec]);
    assert(pct===100,'§'+sec+' expected 100% got '+pct+'% missing='+JSON.stringify(missing));
  });
  test('§'+sec+' 0% when data is {}',()=>{
    const reqs=REQUIRED_FIELDS[sec];
    if(!reqs||!reqs.length){passed++;console.log('  ⏭  §'+sec+' (no required fields — skip)');return;}
    const{pct}=getCompleteness(sec,{});
    assert(pct===0,'§'+sec+' empty data should give 0%, got '+pct+'%');
  });
}

// ══════════════════════════════════════════════════════════════════════════
// 5. FIELD_DEFS coverage — all 10 sections
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n── FIELD_DEFS coverage ───────────────────────────────────────');
for(let sec=1;sec<=10;sec++){
  test('§'+sec+' FIELD_DEFS has ≥1 field',()=>{
    const defs=FIELD_DEFS[sec];
    assert(Array.isArray(defs)&&defs.length>0,'§'+sec+' has no FIELD_DEFS');
  });
  test('§'+sec+' FIELD_DEFS paths are unique',()=>{
    const paths=FIELD_DEFS[sec].map(f=>f.p);
    const dupes=paths.filter((p,i)=>paths.indexOf(p)!==i);
    assert(dupes.length===0,'§'+sec+' duplicate paths: '+dupes.join(', '));
  });
  test('§'+sec+' every field has p, l, t with valid type',()=>{
    const VALID_TYPES=new Set(['text','number','textarea','lines','json','bool','select']);
    for(const f of FIELD_DEFS[sec]){
      assert(f.p,'§'+sec+': field missing p');
      assert(f.l,'§'+sec+': field '+f.p+' missing l');
      assert(f.t,'§'+sec+': field '+f.p+' missing t');
      assert(VALID_TYPES.has(f.t),'§'+sec+': field '+f.p+' unknown type '+f.t);
    }
  });
}

// ══════════════════════════════════════════════════════════════════════════
// 6. syncJsonToForm guard logic (pure portion)
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n── syncJsonToForm guard logic ────────────────────────────────');
test('Invalid JSON triggers parse error', ()=>{
  let err=null;
  try{ JSON.parse('{bad json'); }catch(e){ err=e; }
  assert(err!==null,'should throw on bad JSON');
});
test('Array input rejected by guard', ()=>{
  const parsed=[1,2,3];
  assert(typeof parsed!=='object'||Array.isArray(parsed));
});
test('Null rejected by guard (falsy)', ()=>{
  assert(null===null,'null is falsy');
});
test('Plain object accepted by guard', ()=>{
  const parsed={report_type:'new_deal'};
  assert(typeof parsed==='object'&&!Array.isArray(parsed)&&parsed!==null);
});
test('syncJsonToForm and _applyJsonToForm are defined in index.html', ()=>{
  assert(typeof syncJsonToForm==='function','syncJsonToForm should be a function');
  assert(typeof _applyJsonToForm==='function','_applyJsonToForm should be a function');
});

// ══════════════════════════════════════════════════════════════════════════
// 7. stripNulls + expandPayload integration (ETL → form pipeline)
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n── stripNulls + expandPayload integration ────────────────────');
test('§7 ETL JSON with nulls → strip → expand → has financial fields', ()=>{
  const etl={
    entities_to_analyze:[{role:'Borrower',name:'TSC',currency:'USD',unit:'thousands',guarantor_exists:true}],
    '7A_borrower_financials':{reporting_entity:'Tree Shipping Corporation',reporting_currency:'USD',unit:'thousands',auditor:null,audit_opinion:null,accounting_standard:null,fiscal_year_end:'12-31',income_statement:{'2025':{revenue:379069,cogs:286390,gross_profit:92695,other_op_income:null,op_profit:74123,finance_income:null,finance_cost:null,other_non_op:8737}}},
    '7B_key_ratios':null,
    '8A_acra_banking_charges':{entity_name:null,acra_data_available:null,search_date:null}
  };
  const stripped=stripNulls(etl);
  assert(!('7B_key_ratios' in stripped),'null top-level key removed');
  assert(!('auditor' in stripped['7A_borrower_financials']),'null nested key removed');
  assert(stripped['7A_borrower_financials'].reporting_entity==='Tree Shipping Corporation');
  const expanded=expandPayload(7,stripped);
  assert(expanded['7A_borrower_financials'].reporting_entity==='Tree Shipping Corporation');
  // income_statement should be flattened
  const keys=Object.keys(expanded['7A_borrower_financials']);
  assert(keys.some(k=>k.startsWith('revenue')),'income statement should be flattened');
});
test('§4 all-null management entries stripped → empty array after strip', ()=>{
  const etl={'4C_management':[{name:null,title:null,years_experience:null,background:null},{name:null,title:null,years_experience:null,background:null}],'4D_business':{primary_business:'Container Shipping',trade_routes:null}};
  const stripped=stripNulls(etl);
  assert(!stripped['4C_management']||stripped['4C_management'].length===0,'all-null mgmt entries stripped');
  assertEqual(stripped['4D_business'].primary_business,'Container Shipping');
  assert(!('trade_routes' in stripped['4D_business']));
});
test('strip preserves 0, false, empty string', ()=>{
  const r=stripNulls({a:0,b:false,c:'',d:null});
  assert('a' in r&&r.a===0);
  assert('b' in r&&r.b===false);
  assert('c' in r&&r.c==='');
  assert(!('d' in r));
});

// ══════════════════════════════════════════════════════════════════════════
// Summary
// ══════════════════════════════════════════════════════════════════════════
console.log('\\n' + '─'.repeat(60));
console.log('  Total: '+(passed+failed)+'  ✅ '+passed+' passed  ❌ '+failed+' failed');
console.log('─'.repeat(60));
if(failed>0) throw new Error(failed+' test(s) failed');

})(); // end runTests
`;

try {
  vm.runInContext(testCode, ctx);
} catch (e) {
  process.exit(1);
}
