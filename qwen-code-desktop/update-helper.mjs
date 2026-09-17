#!/usr/bin/env node
// Crypto helpers for update-qwen-desktop.ps1 (Windows PowerShell 5.1 has no
// Ed25519 or BLAKE2b). Everything goes to stdout; the exit code is the verdict.
//
//   node update-helper.mjs verify-sig <installer.exe> <signature-b64> <pubkey-b64>
//       minisign check, the same one the app's Tauri updater does:
//       0 = valid, 1 = invalid
//   node update-helper.mjs verify-runtime <...\runtime\qwen-code>
//       sha256 of every file listed in checksums.json:
//       0 = pristine, 2 = only our own UI patches differ, 1 = damaged/partial

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const fail = (msg) => { console.log(`FAIL: ${msg}`); process.exit(1); };

// markers the local patches leave behind (inject.py, ctxpop.py)
const PATCH_MARKERS = ['BEGIN qwen-ctx-panel', 'QCtxPop'];

function minisignLines(b64) {
  return Buffer.from(b64.trim(), 'base64').toString('utf8').split(/\r?\n/);
}

function verifySig(file, sigB64, keyB64) {
  const key = Buffer.from(minisignLines(keyB64)[1] || '', 'base64'); // "Ed" + keyid(8) + pk(32)
  if (key.length !== 42 || key.toString('latin1', 0, 2) !== 'Ed') fail('bad public key');
  const keyId = Buffer.from(key.subarray(2, 10)).reverse().toString('hex').toUpperCase();

  const lines = minisignLines(sigB64);
  const sig = Buffer.from(lines[1] || '', 'base64'); // alg(2) + keyid(8) + sig(64)
  if (sig.length !== 74) fail('bad signature blob');
  if (!sig.subarray(2, 10).equals(key.subarray(2, 10))) fail(`signature is not from the app key ${keyId}`);

  const pub = crypto.createPublicKey({
    key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), key.subarray(10)]),
    format: 'der', type: 'spki',
  });
  const alg = sig.toString('latin1', 0, 2);
  const data = fs.readFileSync(file);
  let msg;
  if (alg === 'ED') msg = crypto.createHash('blake2b512').update(data).digest(); // prehashed
  else if (alg === 'Ed') msg = data;
  else fail(`unknown signature algorithm ${alg}`);
  if (!crypto.verify(null, msg, pub, sig.subarray(10))) fail('SIGNATURE DOES NOT MATCH THIS FILE');

  const trusted = lines[2] || '';
  const prefix = 'trusted comment: ';
  if (!trusted.startsWith(prefix)) fail('signature has no trusted comment');
  const global = Buffer.from(lines[3] || '', 'base64');
  const signed = Buffer.concat([sig.subarray(10), Buffer.from(trusted.slice(prefix.length), 'utf8')]);
  if (!crypto.verify(null, signed, pub, global)) fail('trusted comment signature invalid');

  console.log(`signature OK: key ${keyId}, ${data.length} bytes, ${trusted.slice(prefix.length).replace(/\t/g, ' ')}`);
}

function verifyRuntime(dir) {
  const sums = JSON.parse(fs.readFileSync(path.join(dir, 'checksums.json'), 'utf8'));
  const entries = Object.entries(sums);
  let ok = 0;
  const patched = [], changed = [], missing = [];
  for (const [rel, want] of entries) {
    const p = path.join(dir, ...rel.split('/'));
    let data;
    try { data = fs.readFileSync(p); } catch { missing.push(rel); continue; }
    if (crypto.createHash('sha256').update(data).digest('hex') === want) { ok++; continue; }
    const text = data.toString('utf8');
    (PATCH_MARKERS.some((m) => text.includes(m)) ? patched : changed).push(rel);
  }
  console.log(`runtime files: ${ok} pristine, ${patched.length} locally patched, ` +
    `${changed.length} changed, ${missing.length} missing (of ${entries.length})`);
  for (const r of patched) console.log(`  patched: ${r}`);
  for (const r of changed.slice(0, 20)) console.log(`  changed: ${r}`);
  for (const r of missing.slice(0, 20)) console.log(`  missing: ${r}`);
  if (changed.length || missing.length) process.exit(1);
  process.exit(patched.length ? 2 : 0);
}

const [cmd, ...args] = process.argv.slice(2);
if (cmd === 'verify-sig' && args.length === 3) verifySig(...args);
else if (cmd === 'verify-runtime' && args.length === 1) verifyRuntime(args[0]);
else fail('usage: verify-sig <file> <sig-b64> <pubkey-b64> | verify-runtime <runtime-dir>');
