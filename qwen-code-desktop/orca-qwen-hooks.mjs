#!/usr/bin/env node
// Orca's Qwen Code status hooks, for a Qwen Code that lives in QWEN_HOME.
//
//   node orca-qwen-hooks.mjs                  # report only
//   node orca-qwen-hooks.mjs --apply          # back up, then add/refresh the hooks
//   node orca-qwen-hooks.mjs --remove --apply # take them out again
//   node orca-qwen-hooks.mjs --settings <path/to/settings.json> [--apply]
//
// Orca (onorca.dev) shows each agent pane as working / waiting / done from
// hook events. For Qwen Code it installs those hooks into ~/.qwen/settings.json
// and nowhere else. A Qwen Code started with QWEN_HOME pointing elsewhere reads
// $QWEN_HOME/settings.json instead, so the hooks never fire and Orca's Qwen
// panes stay blank. This copies the same events into $QWEN_HOME/settings.json,
// each running %USERPROFILE%\.orca\agent-hooks\qwen-code-hook.cmd (Orca keeps
// that script up to date; it exits at once when the pane is not Orca's).
//
// Orca's own entries run through `powershell -Command`, about 330 ms per event
// on Windows; two events per tool call adds ~0.7 s to every tool call, also in
// Qwen Code Desktop sessions that share these settings. These entries leave
// `shell` unset and are nothing but the script's path, written with forward
// slashes. Unset `shell` means Qwen picks cmd.exe, or bash when it was started
// from Git Bash (MSYSTEM set), or PowerShell when ComSpec is PowerShell, and a
// bare forward-slash path runs under all three (measured: cmd ~45 ms, bash
// ~100 ms, PowerShell ~225 ms). Shell syntax such as `if exist` would not: in
// bash it is a syntax error, exit code 2, and Qwen treats exit 2 from a hook as
// "block", so every prompt was silently dropped. A missing script (Orca
// uninstalled) exits 1 or 127, which Qwen logs and ignores; run --remove then.
// If the path contains spaces or shell characters, the PowerShell form is used.
//
// Restart Qwen Code afterwards (hooks are read when a session starts).

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const MARK = /\.orca[\\/]+agent-hooks[\\/]+qwen-code-hook\.(cmd|sh)/i;
const FALLBACK_EVENTS = ['SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
  'PostToolUseFailure', 'PermissionRequest', 'Stop', 'StopFailure', 'Notification', 'SubagentStart', 'SubagentStop'];

const argv = process.argv.slice(2);
const apply = argv.includes('--apply');
const remove = argv.includes('--remove');
const si = argv.indexOf('--settings');
const home = os.homedir();
const settingsPath = si >= 0 ? argv[si + 1]
  : path.join(process.env.QWEN_HOME || path.join(home, '.qwen'), 'settings.json');

if (process.platform !== 'win32') { console.log('Windows only (Orca writes its own hooks elsewhere)'); process.exit(1); }
const orcaSettings = path.join(home, '.qwen', 'settings.json');
if (path.resolve(settingsPath).toLowerCase() === path.resolve(orcaSettings).toLowerCase()) {
  console.log(`${settingsPath} is the file Orca manages itself - nothing to do`);
  process.exit(0);
}

// Which events Orca hooks today, read from the file it wrote.
let events = FALLBACK_EVENTS, eventsFrom = 'built-in list';
try {
  const o = JSON.parse(fs.readFileSync(orcaSettings, 'utf8'));
  const ev = Object.entries(o.hooks || {})
    .filter(([, groups]) => Array.isArray(groups) && groups.some((g) => (g.hooks || []).some((h) => MARK.test(h.command || ''))))
    .map(([name]) => name);
  if (ev.length) { events = ev; eventsFrom = orcaSettings; }
} catch { /* Orca not run yet, or no file: use the list */ }

const script = path.join(home, '.orca', 'agent-hooks', 'qwen-code-hook.cmd').replace(/\\/g, '/');
const plain = /^[A-Za-z]:\/[A-Za-z0-9._\/-]+$/.test(script);
const entry = plain
  ? { type: 'command', command: script, timeout: 10 }
  : { type: 'command', shell: 'powershell', timeout: 10,
      command: `$scriptPath = Join-Path $env:USERPROFILE '.orca\\agent-hooks\\qwen-code-hook.cmd'; if (Test-Path -LiteralPath $scriptPath -PathType Leaf) { & $scriptPath; exit $LASTEXITCODE }; [Console]::In.ReadToEnd() | Out-Null; Write-Output '{}'; exit 0` };

const raw = fs.readFileSync(settingsPath, 'utf8');
const eol = raw.includes('\r\n') ? '\r\n' : '\n';
const trailing = /\r?\n$/.test(raw) ? eol : '';
const bom = raw.charCodeAt(0) === 0xfeff ? '﻿' : '';
const serialize = (obj) => bom + JSON.stringify(obj, null, 2).replace(/\n/g, eol) + trailing;
const cfg = JSON.parse(bom ? raw.slice(1) : raw);
const sameFormat = serialize(cfg) === raw;

// Drop every Orca hook already there (ours or a stale copy), keep the user's own.
const hooks = cfg.hooks && typeof cfg.hooks === 'object' ? cfg.hooks : {};
let removed = 0;
for (const name of Object.keys(hooks)) {
  if (!Array.isArray(hooks[name])) continue;
  hooks[name] = hooks[name].map((g) => {
    const keep = (g.hooks || []).filter((h) => !MARK.test(h.command || ''));
    removed += (g.hooks || []).length - keep.length;
    return { ...g, hooks: keep };
  }).filter((g) => (g.hooks || []).length > 0);
  if (hooks[name].length === 0) delete hooks[name];
}
let added = 0;
if (!remove) {
  for (const name of events) { (hooks[name] ??= []).push({ hooks: [{ ...entry }] }); added++; }
}
if (Object.keys(hooks).length) cfg.hooks = hooks; else delete cfg.hooks;

const out = serialize(cfg);
console.log(`settings: ${settingsPath}`);
console.log(`events (${events.length}, from ${eventsFrom}): ${events.join(', ')}`);
console.log(`hook: ${plain ? 'script path, any shell' : 'powershell'}  ${entry.command}`);
if (!fs.existsSync(script)) console.log(`WARNING: ${script} does not exist yet (start Orca once); the hooks will log errors until it does`);
console.log(`Orca hooks found: ${removed}; after: ${added}`);
if (out === raw) { console.log('already up to date - nothing to do'); process.exit(0); }
if (cfg.hooksConfig?.disableAllHooks || cfg.disableAllHooks) console.log('WARNING: hooks are disabled in this settings file');
if (!sameFormat) console.log('note: the file is not in 2-space JSON layout; rewriting will normalize it');
if (!apply) { console.log('dry run - pass --apply to write'); process.exit(0); }

const stamp = new Date().toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15) + 'Z';
const bak = `${settingsPath}.bak-${stamp}-before-orca-hooks`;
fs.copyFileSync(settingsPath, bak);
if (fs.readFileSync(bak, 'utf8') !== raw) throw new Error(`backup does not match the original: ${bak}`);
const tmp = settingsPath + '.new';
fs.writeFileSync(tmp, out, 'utf8');
if (fs.readFileSync(tmp, 'utf8') !== out) { fs.unlinkSync(tmp); throw new Error('read-back of the new file does not match'); }
if (fs.readFileSync(settingsPath, 'utf8') !== raw) {
  fs.unlinkSync(tmp);
  throw new Error('settings.json changed while this ran (the app rewrote it?) - nothing written, run again');
}
fs.renameSync(tmp, settingsPath);
console.log(`backup -> ${bak}`);
console.log(`written: ${Buffer.byteLength(raw)} -> ${Buffer.byteLength(out)} bytes. Restart Qwen Code / Qwen Code Desktop.`);
