#!/usr/bin/env node
// OpenCode Go: send a per-conversation x-opencode-session from Qwen Code.
//
//   node opencode-session-header.mjs                 # report only
//   node opencode-session-header.mjs --apply         # back up, then rewrite settings.json
//   node opencode-session-header.mjs --settings <path/to/settings.json> [--apply]
//
// The OpenCode Go gateway refuses a request without `x-opencode-session`
// (HTTP 400 MissingSessionID) and asks for "a stable session ID ... for each
// conversation" so it can route and cache per conversation. A fixed value in
// customHeaders satisfies the 400 check but sends every conversation, every
// subagent and every machine under ONE session id.
//
// Qwen Code 0.26.x can expand `${session_id}` inside customHeaders per request,
// but only when `outboundCorrelation.allowDynamicHeaderValues` is true; with the
// switch off, a value containing the placeholder is DROPPED (so the gateway
// answers 400 again). This script sets both, on every modelProviders entry whose
// baseUrl is an OpenCode Go endpoint, and leaves everything else untouched.
//
// Restart Qwen Code (or Qwen Code Desktop) afterwards: the switch is read once.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const HEADER = 'x-opencode-session';
const VALUE = '${session_id}';
const IS_GO = (url) => /^https:\/\/opencode\.ai\/zen\/go(\/|$)/.test(url || '');

const argv = process.argv.slice(2);
const apply = argv.includes('--apply');
const si = argv.indexOf('--settings');
const settingsPath = si >= 0 ? argv[si + 1]
  : path.join(process.env.QWEN_HOME || path.join(os.homedir(), '.qwen'), 'settings.json');

const raw = fs.readFileSync(settingsPath, 'utf8');
const eol = raw.includes('\r\n') ? '\r\n' : '\n';
const trailing = /\r?\n$/.test(raw) ? eol : '';
const bom = raw.charCodeAt(0) === 0xfeff ? '﻿' : '';
const serialize = (obj) => bom + JSON.stringify(obj, null, 2).replace(/\n/g, eol) + trailing;

const cfg = JSON.parse(bom ? raw.slice(1) : raw);
const sameFormat = serialize(cfg) === raw;

let entries = 0, changed = 0;
const before = new Map();
for (const [kind, list] of Object.entries(cfg.modelProviders || {})) {
  if (!Array.isArray(list)) continue;
  for (const m of list) {
    if (!IS_GO(m.baseUrl)) continue;
    entries++;
    const gc = (m.generationConfig ??= {});
    const h = (gc.customHeaders ??= {});
    const old = h[HEADER];
    before.set(`${old}`, (before.get(`${old}`) || 0) + 1);
    if (old !== VALUE) { h[HEADER] = VALUE; changed++; }
  }
}
const gateWas = cfg.outboundCorrelation?.allowDynamicHeaderValues === true;
if (!gateWas) cfg.outboundCorrelation = { ...(cfg.outboundCorrelation || {}), allowDynamicHeaderValues: true };

console.log(`settings: ${settingsPath}`);
console.log(`OpenCode Go entries: ${entries}; ${HEADER} values now: ` +
  [...before].map(([v, n]) => `${v === 'undefined' ? '(none)' : JSON.stringify(v)} x${n}`).join(', '));
console.log(`outboundCorrelation.allowDynamicHeaderValues: ${gateWas}`);

if (entries === 0) { console.log('no OpenCode Go provider entries - nothing to do'); process.exit(0); }
if (changed === 0 && gateWas) { console.log('already per-conversation - nothing to do'); process.exit(0); }
console.log(`would set ${HEADER}=${VALUE} on ${changed} entr${changed === 1 ? 'y' : 'ies'}` +
  (gateWas ? '' : ' and turn allowDynamicHeaderValues on'));
if (!sameFormat) console.log('note: the file is not in 2-space JSON layout; rewriting will normalize it');
if (!apply) { console.log('dry run - pass --apply to write'); process.exit(0); }

const stamp = new Date().toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15) + 'Z';
const bak = `${settingsPath}.bak-${stamp}-before-session-header`;
fs.copyFileSync(settingsPath, bak);
if (fs.readFileSync(bak, 'utf8') !== raw) throw new Error(`backup does not match the original: ${bak}`);

const out = serialize(cfg);
const tmp = settingsPath + '.new';
fs.writeFileSync(tmp, out, 'utf8');
const back = fs.readFileSync(tmp, 'utf8');
if (back !== out) throw new Error('read-back of the new file does not match');
const check = JSON.parse(bom ? back.slice(1) : back);
const bad = Object.values(check.modelProviders || {}).flat()
  .filter((m) => IS_GO(m.baseUrl) && m.generationConfig?.customHeaders?.[HEADER] !== VALUE);
if (bad.length || check.outboundCorrelation?.allowDynamicHeaderValues !== true) {
  fs.unlinkSync(tmp);
  throw new Error('verification of the new file failed - original left untouched');
}
if (fs.readFileSync(settingsPath, 'utf8') !== raw) {
  fs.unlinkSync(tmp);
  throw new Error('settings.json changed while this ran (the app rewrote it?) - nothing written, run again');
}
fs.renameSync(tmp, settingsPath);
console.log(`backup -> ${bak}`);
console.log(`written: ${Buffer.byteLength(raw)} -> ${Buffer.byteLength(out)} bytes. Restart Qwen Code / Qwen Code Desktop.`);
