/**
 * Extract and evaluate pure JavaScript functions from static/index.html.
 *
 * finalizePayload() and expandPayload() are self-contained data transformers
 * with no DOM or network dependencies — we can run them in a plain Node.js vm
 * context to test their logic directly.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML_PATH = path.join(__dirname, '../../../static/index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');

/** Count balanced braces/brackets to find the end of a declaration. */
function extractBlock(startIdx) {
  let depth = 0;
  let inStr = false;
  let strChar = null;
  let i = startIdx;
  while (i < html.length) {
    const c = html[i];
    if (inStr) {
      if (c === strChar && html[i - 1] !== '\\') inStr = false;
    } else if (c === '"' || c === "'" || c === '`') {
      inStr = true; strChar = c;
    } else if (c === '{' || c === '[' || c === '(') {
      depth++;
    } else if (c === '}' || c === ']' || c === ')') {
      depth--;
      if (depth <= 0) { return i + 1; }
    } else if (depth === 0 && c === ';') {
      return i + 1;
    }
    i++;
  }
  return i;
}

/** Extract source of `const NAME = ...;` */
function extractConst(name) {
  const marker = `const ${name}=`;
  const start = html.indexOf(marker);
  if (start === -1) throw new Error(`const ${name} not found`);
  const valueStart = start + marker.length;
  const end = extractBlock(valueStart);
  return html.slice(start, end);
}

/** Extract a function body from marker to endMarker (exclusive of endMarker). */
function extractBetween(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  if (start === -1) throw new Error(`Start not found: ${startMarker}`);
  const end = html.indexOf(endMarker, start + startMarker.length);
  if (end === -1) throw new Error(`End not found: ${endMarker} after ${startMarker.slice(0, 30)}`);
  return html.slice(start, end);
}

// ── Build minimal vm context ────────────────────────────────────────────────

const optsZhDecl  = extractConst('OPTS_ZH');
const docTypeDecl = extractConst('DOC_TYPE_OPTIONS');

// TRANSLATIONS is a large object — extract it
const transMarker = 'const TRANSLATIONS={';
const transStart  = html.indexOf(transMarker);
const transEnd    = extractBlock(transStart + transMarker.length - 1); // point at '{'
const transDecl   = html.slice(transStart, transEnd);

const finalizeBody = extractBetween(
  'function finalizePayload(secNo,data){',
  '\nfunction expandPayload(secNo,data){'
);
const expandBody = extractBetween(
  'function expandPayload(secNo,data){',
  '\nasync function openStructuredForm(){'
);

const code = `
var lang = 'en';
${transDecl};
${optsZhDecl};
${docTypeDecl};
function tl(en,zh){return lang==='zh'?zh:en;}
function t(key){
  const T=TRANSLATIONS||{};
  return ((T[lang]||T.en||{})[key])||((T.en||{})[key])||key;
}
${finalizeBody}
${expandBody}
module.exports = {
  finalizePayload: finalizePayload,
  expandPayload: expandPayload,
  OPTS_ZH: OPTS_ZH,
  DOC_TYPE_OPTIONS: DOC_TYPE_OPTIONS,
  setLang: function(l){ lang = l; },
  getLang: function(){ return lang; },
  tl: tl,
};
`;

const ctx = { module: { exports: {} }, exports: {} };
ctx.exports = ctx.module.exports;
vm.createContext(ctx);
vm.runInContext(code, ctx);

// Re-export with convenience wrappers so callers don't need to access ctx directly
const _exp = ctx.module.exports;
module.exports = {
  finalizePayload: _exp.finalizePayload,
  expandPayload: _exp.expandPayload,
  OPTS_ZH: _exp.OPTS_ZH,
  DOC_TYPE_OPTIONS: _exp.DOC_TYPE_OPTIONS,
  tl: _exp.tl,
  setLang: _exp.setLang,
  getLang: _exp.getLang,
  ctx,
};
