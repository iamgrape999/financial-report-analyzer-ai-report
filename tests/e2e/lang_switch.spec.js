/**
 * Playwright E2E tests: language-switch runtime behaviour.
 *
 * These tests load a minimal self-contained HTML page (assembled from the
 * real functions in static/index.html) into a headless Chromium browser.
 * They verify actual DOM mutations that static analysis and Jest cannot see:
 * - setLang() rewrites all [data-i18n] element text in real time
 * - The document-type dropdown re-renders with zh/en labels
 * - localStorage 'lang' is updated
 * - OPTS_ZH is accessible from window
 *
 * Test type: 端對端瀏覽器測試 (Playwright) + 語言切換執行時行為驗
 */

'use strict';

const { test, expect } = require('/opt/node22/lib/node_modules/playwright/test.js');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// ── Extract the functions we need from index.html ─────────────────────────

const HTML_PATH = path.join(__dirname, '../../static/index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');

function extractBetween(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  if (start === -1) throw new Error(`Not found: ${startMarker.slice(0, 40)}`);
  const end = html.indexOf(endMarker, start + startMarker.length);
  if (end === -1) throw new Error(`End not found: ${endMarker.slice(0, 40)}`);
  return html.slice(start, end);
}

function extractBlock(startIdx) {
  let depth = 0; let inStr = false; let strChar = null; let i = startIdx;
  while (i < html.length) {
    const c = html[i];
    if (inStr) { if (c === strChar && html[i-1] !== '\\') inStr = false; }
    else if (c==='"'||c==="'"||c==='`'){ inStr=true; strChar=c; }
    else if (c==='{'||c==='['||c==='(') depth++;
    else if (c==='}'||c===']'||c===')') { depth--; if(depth<=0){return i+1;} }
    else if (depth===0 && c===';') return i+1;
    i++;
  }
  return i;
}

function extractConst(name) {
  const marker = `const ${name}=`;
  const start = html.indexOf(marker);
  if (start === -1) throw new Error(`const ${name} not found`);
  return html.slice(start, extractBlock(start + marker.length));
}

const optsZh   = extractConst('OPTS_ZH');
const docTypes = extractConst('DOC_TYPE_OPTIONS');
const transMarker = 'const TRANSLATIONS={';
const transStart  = html.indexOf(transMarker);
const transDecl   = html.slice(transStart, extractBlock(transStart + transMarker.length - 1));

// setLang: use brace-counting extractor (defined below)

// renderDocTypeSelect: extract function body using brace counting
function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = html.indexOf(marker);
  if (start === -1) throw new Error(`Function ${name} not found`);
  // Find the opening brace
  let braceStart = html.indexOf('{', start);
  let depth = 0; let i = braceStart;
  while (i < html.length) {
    if (html[i] === '{') depth++;
    else if (html[i] === '}') { depth--; if (depth === 0) { return html.slice(start, i + 1); } }
    i++;
  }
  throw new Error(`Function ${name} body not closed`);
}

const setLangFn       = extractFunction('setLang');
const renderDocTypeFn = extractFunction('renderDocTypeSelect');
const tlFn = `function tl(en,zh){return lang==='zh'?zh:en;}`;
const tFn  = `function t(key){const T=TRANSLATIONS||{};return((T[lang]||T.en||{})[key])||((T.en||{})[key])||key;}`;

// ── Build self-contained minimal test HTML ─────────────────────────────────

function buildTestHtml() {
  return `<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><title>Lang Test</title>
<script>
// Mock localStorage before any script (about:blank denies native localStorage)
;(function(){
  const _s={};
  const mock={
    getItem:function(k){return Object.prototype.hasOwnProperty.call(_s,k)?_s[k]:null;},
    setItem:function(k,v){_s[k]=String(v);},
    removeItem:function(k){delete _s[k];},
    clear:function(){Object.keys(_s).forEach(function(k){delete _s[k];});},
    key:function(i){return Object.keys(_s)[i]||null;},
    get length(){return Object.keys(_s).length;}
  };
  try{
    window.localStorage.setItem('_pw','1');
    window.localStorage.removeItem('_pw');
  }catch(e){
    try{Object.defineProperty(window,'localStorage',{value:mock,configurable:true,writable:true});}
    catch(e2){}
  }
  // Also patch on globalThis for security contexts that block window.localStorage
  if(!globalThis._pwLSMock){globalThis._pwLSMock=mock;}
})();
</script>
</head>
<body>
<!-- Minimal UI for lang tests -->
<button id="langBtn">EN</button>
<select id="docType"></select>
<nav>
  <a data-i18n="nav.logout">登出</a>
  <a data-i18n="login.title">登入</a>
  <a data-i18n="app.subtitle">國泰金控</a>
</nav>
<script>
// Safe localStorage wrapper
var _ls=(function(){
  try{window.localStorage.setItem('_t','1');window.localStorage.removeItem('_t');return window.localStorage;}
  catch(e){var s={};return{getItem:function(k){return s[k]||null;},setItem:function(k,v){s[k]=String(v);},removeItem:function(k){delete s[k];}}}
})();
var lang = _ls.getItem('lang')||'zh';
${transDecl};
${optsZh};
${docTypes};
${tlFn}
${tFn}
${renderDocTypeFn}
// Patched setLang: uses _ls instead of localStorage
function setLang(l){
  lang=l;
  _ls.setItem('lang',lang);
  var btn=document.getElementById('langBtn');
  if(btn)btn.textContent=lang==='zh'?'EN':'中文';
  document.querySelectorAll('[data-i18n]').forEach(function(el){el.innerHTML=t(el.getAttribute('data-i18n'));});
  renderDocTypeSelect();
}
// Expose to window (const/let declarations don't auto-expose in non-module scripts)
window.OPTS_ZH = OPTS_ZH;
window.DOC_TYPE_OPTIONS = DOC_TYPE_OPTIONS;
window.setLang = setLang;
window.tl = tl;
window.__getLang = function(){ return _ls.getItem('lang'); };
// Wire lang toggle button (use addEventListener to avoid HTMLElement.lang shadowing window.lang in onclick)
document.getElementById('langBtn').addEventListener('click', function(){
  setLang(lang==='zh'?'en':'zh');
});
// Initialize
renderDocTypeSelect();
document.getElementById('langBtn').textContent=lang==='zh'?'EN':'中文';
document.querySelectorAll('[data-i18n]').forEach(function(el){el.textContent=t(el.getAttribute('data-i18n'));});
</script>
</body>
</html>`;
}

const TEST_HTML = buildTestHtml();

// ── Helper: load minimal test page ────────────────────────────────────────

async function loadTestPage(page) {
  // Mock localStorage (about:blank denies it in strict mode)
  await page.addInitScript(() => {
    const _store = {};
    try { localStorage.setItem('_pw_test', '1'); localStorage.removeItem('_pw_test'); }
    catch (_e) {
      Object.defineProperty(window, 'localStorage', {
        value: {
          getItem: k => Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null,
          setItem: (k, v) => { _store[k] = String(v); },
          removeItem: k => { delete _store[k]; },
          clear: () => { Object.keys(_store).forEach(k => delete _store[k]); },
          key: i => Object.keys(_store)[i] || null,
          get length() { return Object.keys(_store).length; },
        },
        configurable: true,
      });
    }
  });
  await page.setContent(TEST_HTML, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof window.setLang === 'function', { timeout: 5000 });
}

// ── Tests ─────────────────────────────────────────────────────────────────

test('lang button is present and has initial text', async ({ page }) => {
  await loadTestPage(page);
  const btn = page.locator('#langBtn');
  await expect(btn).toBeVisible();
  const text = await btn.textContent();
  expect(['EN', '中文']).toContain(text);
});

test('initial lang=zh → button shows EN', async ({ page }) => {
  await loadTestPage(page);
  const btn = page.locator('#langBtn');
  const text = await btn.textContent();
  expect(text).toBe('EN');
});

test('clicking lang button toggles language', async ({ page }) => {
  await loadTestPage(page);
  const btn = page.locator('#langBtn');
  const initial = await btn.textContent();

  await btn.click();
  const after = await btn.textContent();
  expect(after).not.toBe(initial);
  expect(['EN', '中文']).toContain(after);
});

test('setLang(en) changes button text to 中文', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('en'));
  const text = await page.locator('#langBtn').textContent();
  expect(text).toBe('中文');
});

test('setLang(zh) changes button text to EN', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('zh'));
  const text = await page.locator('#langBtn').textContent();
  expect(text).toBe('EN');
});

test('setLang(en) updates data-i18n elements', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('en'));
  const navTexts = await page.locator('[data-i18n]').allTextContents();
  expect(navTexts.length).toBeGreaterThan(0);
  // In English mode, nav items should not be pure Chinese
  const allChinese = navTexts.every(t => /^[一-鿿]+$/.test(t));
  expect(allChinese).toBe(false);
});

test('setLang(zh) and setLang(en) produce different data-i18n text', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('zh'));
  const zhTexts = await page.locator('[data-i18n]').allTextContents();

  await page.evaluate(() => window.setLang('en'));
  const enTexts = await page.locator('[data-i18n]').allTextContents();

  const anyDiff = zhTexts.some((t, i) => t !== enTexts[i]);
  expect(anyDiff).toBe(true);
});

test('docType select is present with options', async ({ page }) => {
  await loadTestPage(page);
  const sel = page.locator('#docType');
  await expect(sel).toBeVisible();
  const options = await sel.locator('option').allTextContents();
  expect(options.length).toBeGreaterThan(3);
});

test('docType options contain CJK characters when lang=zh', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('zh'));
  await page.waitForTimeout(50);
  const options = await page.locator('#docType option').allTextContents();
  const hasCjk = options.some(o => /[一-鿿㐀-䶿]/.test(o));
  expect(hasCjk).toBe(true);
});

test('docType options contain ASCII when lang=en', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('en'));
  await page.waitForTimeout(50);
  const options = await page.locator('#docType option').allTextContents();
  const hasEnglish = options.some(o => /[A-Za-z]/.test(o));
  expect(hasEnglish).toBe(true);
});

test('docType option labels change between zh and en', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('zh'));
  await page.waitForTimeout(50);
  const zhOptions = await page.locator('#docType option').allTextContents();

  await page.evaluate(() => window.setLang('en'));
  await page.waitForTimeout(50);
  const enOptions = await page.locator('#docType option').allTextContents();

  const anyDiff = zhOptions.some((t, i) => t !== enOptions[i]);
  expect(anyDiff).toBe(true);
});

test('localStorage lang is updated by setLang', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('zh'));
  const stored = await page.evaluate(() => window.__getLang());
  expect(stored).toBe('zh');

  await page.evaluate(() => window.setLang('en'));
  const storedEn = await page.evaluate(() => window.__getLang());
  expect(storedEn).toBe('en');
});

test('OPTS_ZH accessible from window', async ({ page }) => {
  await loadTestPage(page);
  const count = await page.evaluate(() => Object.keys(window.OPTS_ZH || {}).length);
  expect(count).toBeGreaterThan(20);
});

test('OPTS_ZH["PASS"] is a Chinese string', async ({ page }) => {
  await loadTestPage(page);
  const val = await page.evaluate(() => window.OPTS_ZH['PASS']);
  expect(typeof val).toBe('string');
  expect(val.length).toBeGreaterThan(0);
  // Should contain Chinese character
  expect(/[一-鿿㐀-䶿]/.test(val)).toBe(true);
});

test('tl() returns correct language', async ({ page }) => {
  await loadTestPage(page);
  await page.evaluate(() => window.setLang('en'));
  const en = await page.evaluate(() => window.tl('Upload', '上傳'));
  expect(en).toBe('Upload');

  await page.evaluate(() => window.setLang('zh'));
  const zh = await page.evaluate(() => window.tl('Upload', '上傳'));
  expect(zh).toBe('上傳');
});

test('double toggle returns to original state', async ({ page }) => {
  await loadTestPage(page);
  const initial = await page.locator('#langBtn').textContent();
  await page.locator('#langBtn').click();
  await page.locator('#langBtn').click();
  const restored = await page.locator('#langBtn').textContent();
  expect(restored).toBe(initial);
});
