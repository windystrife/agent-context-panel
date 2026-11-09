# agent-context-panel

Adds the missing **conversation context panel** to coding-agent desktop apps that
track token usage but never show it.

DSCode shows you context capacity, total tokens, cache hit rate and cost.
Qwen Code Desktop and OpenCode Desktop don't — even though the data is already
being recorded. This project reads those records and gives you the panel back,
either as a standalone dashboard or injected **inside the app window**.

![status](https://img.shields.io/badge/platform-Windows%20%2B%20WSL2-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![deps](https://img.shields.io/badge/dependencies-none-brightgreen)

---

## Why

| App | Context panel | Evidence |
|---|---|---|
| DSCode | ✅ built in | — |
| **Qwen Code Desktop** | ❌ none | Zero hits for `Conversation context`, `Context capacity`, `Total tokens`, `contextUsage`, `tokensUsed`, `% used` anywhere in its bundles. The only UI string containing `contextWindow` is `"providerConnect.contextWindow": "Context window"` — the *provider-setup input label*. The 8 `contextWindow` hits in the renderer all belong to framer-motion's drag code. |
| **OpenCode Desktop** | ❌ none | Open upstream issues [#5892](https://github.com/anomalyco/opencode/issues/5892), [#34900](https://github.com/anomalyco/opencode/issues/34900), [#6152](https://github.com/anomalyco/opencode/issues/6152). The TUI has a context meter; the desktop app does not. |

Both apps *do* count tokens internally. Nothing here re-implements counting —
it only surfaces numbers the apps already wrote to disk.

---

## What's in here

```
qwen-code-desktop/   monitor.py  panel.js  inject.py  ctxpop.py  start-monitor.cmd
                     update-qwen-desktop.ps1/.cmd  update-helper.mjs
                     openrouter_billing.py  opencode_go_usage.py
                     opencode-session-header.mjs  orca-qwen-hooks.mjs
                     autostart-monitor.ps1  install-autostart.ps1
opencode-desktop/    monitor.py  inspect_db.py  start-monitor.cmd
ninfer/              monitor.py  start-ninfer.cmd
session-export/      claude_to_qwen.py
```

| | dashboard | in-app panel |
|---|---|---|
| Qwen Code Desktop | ✅ `:8098` | ✅ injected (renderer is unpacked) |
| OpenCode Desktop | ✅ `:8096` | ⛔ not yet — ships a 143 MB `app.asar`, so injecting means unpack + repack, and an `asarIntegrity` fuse would then refuse to start the app |
| NInfer (engine) | ✅ `:8099` | n/a |

### 1. Standalone dashboard — `qwen-code-desktop/monitor.py`

A zero-dependency HTTP dashboard on `127.0.0.1:8098`.

```bash
python3 monitor.py --port 8098
```

Shows, per session: a context ring (`used / context window`, % and available),
total tokens, input/output split, cache hit %, thinking tokens, estimated cost,
request count, tool calls and the project path. Click a row to inspect a session.

### 2. In-app panel — `panel.js` + `inject.py`

Injects a small floating pill into Qwen Code Desktop's own window. Click it to
expand the full card.

```bash
python3 inject.py            # patch
python3 inject.py --status   # patched / clean
python3 inject.py --revert   # restore the pristine backup
```

**Autostart.** The panel is only as alive as the monitor behind it. A button
inside the app cannot start it — the panel is a sandboxed web page and cannot
launch local programs — so instead:

```powershell
powershell -ExecutionPolicy Bypass -File install-autostart.ps1              # install
powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Uninstall   # remove
```

This drops one shortcut in the per-user Startup folder (no admin, no scheduled
task, no registry) that runs `autostart-monitor.ps1` hidden. It is a loop, not a
one-shot: the monitor lives in WSL, and WSL can be shut down under it mid-session.
Killing the monitor on purpose brought it back 12.8 s later. Restarts are logged
to `%LOCALAPPDATA%\qwen-ctx-monitor.log`.

The panel is injected **inline** because the app's CSP is
`script-src 'self' 'unsafe-inline' …` while a separate file under a `file:`/`app:`
origin is not reliably `'self'`. It renders inside a Shadow DOM, so no styles
leak in either direction, and it reads the dashboard's API over
`connect-src … http://127.0.0.1:*`, which that same CSP already allows.

This works because Qwen Code Desktop ships its renderer **unpacked**
(`resources/app/dist/renderer/`) rather than inside an `app.asar`.

**0.3.x layout.** Qwen Code Desktop 0.3.0 is no longer Electron: a WebView2
shell (`%LOCALAPPDATA%\Qwen Code Desktop\qwen-code-desktop.exe`) starts the CLI
as a daemon (`runtime\qwen-code\lib\cli-entry.js serve --port 0 --require-auth`)
and loads the UI from `runtime\qwen-code\lib\web-shell\` over loopback. The
page to inject is now `web-shell\index.html` (pass it with `--html`); its CSP is
`connect-src 'self'`, so the panel reads the monitor's snapshot from
`web-shell\assets\ctx-stats.json` instead of port 8098.

### 2b. Dismissible context breakdown — `ctxpop.py`

In 0.3.x the footer's context ring calls `/context`: it echoes the command into
the chat and appends the breakdown as a message, so every look at the numbers
leaves two entries in the transcript. `ctxpop.py` rewires that button to a
popover — the same "Context Usage" panel the app renders in its right-panel
tab, anchored above the button, closed by Esc, a click outside, or clicking
the button again — the way Claude Code's context-window popover behaves.

```bash
python ctxpop.py            # patch (backs up the bundle once, node --check)
python ctxpop.py --status   # patched / clean
python ctxpop.py --revert   # restore the pristine bundle
```

It patches one file, the web-shell's main bundle, in four places, capturing the
minified names with regexes and refusing unless each anchor matches exactly
once. Typing `/context` by hand still goes to the chat, and the header's
"Context Usage" icon still opens the docked right-panel tab.

Restart the app afterwards: assets are served `immutable`, and the running
page keeps the old bundle. A restart is enough — the daemon picks a fresh port,
so the cache key changes. App updates replace the bundle; re-run the script.
Its anchors do not match 0.24.0 (the context panel component changed); it
refuses and leaves the bundle untouched.

Newer builds do it themselves: from the web-shell bundle dated 2026-10-19
(0.26.5 here), hovering the context chip opens a popover with the usage and a
compress button, and clicking it opens the docked "Context Usage" panel. On
those `--status` reports `native` and a patch run does nothing and exits 0, so
the update script no longer warns about it.

### 2c. Updating the app — `update-qwen-desktop.ps1`

The in-app "Install and restart" of Qwen Code Desktop 0.2x can fail in three
ways, all before or while copying files:

- the app exits to run its NSIS installer, but the daemon it spawned
  (`runtime\qwen-code\node\node.exe … serve`) and its ACP child keep running
  and hold `node.exe` open, so the installer stops partway;
- the installer first runs the uninstaller of the old Electron
  "Qwen Code Desktop" 0.0.x if that is still installed, and aborts (exit code 2)
  when that uninstall fails;
- it installs to `HKCU\Software\alibaba\Qwen Code Desktop` or else
  `%LOCALAPPDATA%\Qwen Code Desktop`, so a copy unpacked elsewhere is never
  updated.

```bat
update-qwen-desktop.cmd                  :: update if the feed has a newer version
update-qwen-desktop.cmd -CheckOnly       :: report only
update-qwen-desktop.cmd -Force           :: reinstall the latest
update-qwen-desktop.cmd -InstallDir D:\apps\qwen-desktop -Launcher D:\apps\launch-qwen.ps1
```

It reads the official update feed, reuses the installer the app already
downloaded to `%TEMP%` (or downloads it) and checks its minisign signature
with the key embedded in the app, stops every process started from the install
folder, parks the old Electron uninstaller during the install (pass
`-RemoveLegacyElectron` to let the installer remove that app instead), installs
silently with `/S /NS /D=<dir>`, and refuses to report success unless the
installer exited 0, `uninstall.exe` was rewritten and every file listed in
`runtime\qwen-code\checksums.json` matches. Then it moves patch backups of the
previous version out of the way, re-runs `inject.py` and `ctxpop.py`, and starts
the app (not from an SSH session). `update-helper.mjs` does the Ed25519 /
BLAKE2b and SHA-256 work that Windows PowerShell 5.1 cannot; it needs Node 18+
on PATH or falls back to a copy of the app's own `node.exe`.

Do not run it from inside Qwen Code Desktop without `-CheckOnly`: it stops the
app it is running in.

### 3. OpenCode Desktop — `opencode-desktop/monitor.py`

```bash
python3 monitor.py --port 8096
```

Reads OpenCode's own SQLite database — `session.tokens_input`,
`tokens_cache_read`, `tokens_output`, `tokens_reasoning`, `cost`, `model` — and
resolves each model's `limit.context` and per-1M prices from the models.dev
catalog OpenCode caches at `~/.cache/opencode/models.json`. The database is
**copied before reading**, so a running OpenCode keeps exclusive use of its file.

> Not yet validated against live data: when this was written, OpenCode's
> `session`, `message` and `part` tables were empty, so column *semantics* are
> inferred from their names. Where a number can be read two ways the API returns
> both (`context_used` = `tokens_input + tokens_cache_read`, `context_used_alt` =
> `tokens_input` alone) and the page says so rather than rendering confident
> zeros. Run one OpenCode session and cross-check against its TUI meter.

### 4. Server-side monitor — `ninfer/monitor.py`

For a local inference engine ([NInfer](https://github.com/Neroued/ninfer)), this
tails `ninfer-serve` logs instead of any client's files, so **every** client is
visible at once — Claude Code, DSCode, curl, anything:

```bash
python3 monitor.py --log ~/serve.log --port 8099
```

Per request it reports prompt size, cache reuse (`append_frontier`,
`restore_turn_checkpoint`, …), TTFT, decode tok/s and MTP acceptance, and it
attributes each request to a client by the endpoint it used
(`/v1/messages` → Claude Code, `/v1/chat/completions` → OpenAI-compatible).

---

### 5. Cost and usage — `openrouter_billing.py` + `opencode_go_usage.py`

Loaded by `qwen-code-desktop/monitor.py` when a key is available
(`OPENROUTER_API_KEY` / `~/.claude/openrouter.key`, `OPENCODE_GO_API_KEY` /
`~/.claude/opencodego.key` or the Qwen `.env`). Keys only ever travel in an
`Authorization` header and are never logged. `--no-billing` turns both off.

**OpenRouter: billed cost, not an estimate.** Token counts Qwen logs match
OpenRouter's own to the token, yet a price-table estimate came out exactly
**2.00× low**: OpenRouter routes each request to one of ~12 providers, and the
headline price is the cheapest one's. `openrouter_billing.py` resolves every
`gen-…` id through `/api/v1/generation` into its real `total_cost` and provider,
caches it on disk, and backfills history. Stats are not available immediately
(first success took ~49 s; the endpoint can also briefly 404 on ids it answered
before), so misses back off and retry. Each session is labelled `billed`,
`partial` (still reconciling) or `estimate`. Account spend comes from
`/api/v1/key` and `/api/v1/credits`: today, week, month, all time, balance.

**OpenCode Go: limit standing, not a bill.** It is a subscription with per-model
budgets. The `cost` field on every chat response is always `"0"`, so it is not
used. `GET /zen/go/v1/usage` (undocumented; what the console shows) gives the
rolling 5-hour, weekly and monthly windows as status / percent / reset time —
account-wide (`?model=` is ignored) and integer percent. Per-model rows count
requests and tokens from Qwen's logs; their dollar figures are catalog
estimates and are labelled as such.

**Routing notes that affect cost** (measured, not assumed):

- Pinning DeepSeek's own endpoint can be refused by an account privacy setting
  ("paid-model-training-violation-by-account").
- `sort: price` (or the `:floor` model suffix) sorts by input/output price and
  ignores cache price. For cache-heavy agent traffic Morph and Modal cost
  59% / 112% *more* than the default. Exclude them (`ignore`, or account-wide at
  `openrouter.ai/settings/privacy`).
- Qwen Code does not send `generationConfig.extra_body` on chat/completions
  requests (it only reads `reasoning_effort` from it), so body routing is
  silently dropped; a model-id suffix such as `:floor` is what reaches OpenRouter.

**OpenCode Go needs a session id per conversation** — `opencode-session-header.mjs`.
The gateway answers `400 MissingSessionID` to a request without
`x-opencode-session`, and its docs ask for a stable id *for each
conversation*. A fixed UUID in `customHeaders` gets past the 400 but files every
conversation, subagent and machine under one id. Qwen Code 0.26 can fill the
header per request from `${session_id}`, but only with
`outboundCorrelation.allowDynamicHeaderValues: true`; with that switch off the
header is **dropped**, and the 400 comes back. The script sets both on every
OpenCode Go provider entry (backup first, dry run by default):

```bash
node opencode-session-header.mjs            # report
node opencode-session-header.mjs --apply    # write; then restart Qwen Code
```

Checked with Qwen Code 0.26.5 and 0.26.6 against a throwaway `QWEN_HOME`:
placeholder without the switch → `400 MissingSessionID`; with it → `200`.

### 5b. Orca status for Qwen panes — `orca-qwen-hooks.mjs`

[Orca](https://onorca.dev) marks each agent pane working / waiting / done from
hook events, and for Qwen Code it installs those hooks into
`~/.qwen/settings.json` only. A Qwen Code with `QWEN_HOME` set (as Qwen Code
Desktop's is) reads `$QWEN_HOME/settings.json`, so the hooks never fire and
Orca's Qwen panes show nothing. The script copies Orca's events into
`$QWEN_HOME/settings.json` (Windows; dry run by default, backup first,
`--remove` takes them out again):

```bash
node orca-qwen-hooks.mjs --apply
```

Each entry is just the path of Orca's own `qwen-code-hook.cmd`, written with
forward slashes and no `shell`, for two measured reasons:

- Orca's entries go through `powershell -Command`, ~330 ms per event, i.e.
  ~0.7 s per tool call, and settings in `QWEN_HOME` also apply to every Qwen
  Code Desktop session. Without `shell` Qwen uses cmd.exe: ~45 ms.
- Without `shell` Qwen picks bash when it was started from Git Bash. Shell
  syntax such as `if exist …` is then a bash syntax error, exit code 2, and
  Qwen reads exit 2 from a hook as "block": prompts were dropped without a word.
  A bare forward-slash path runs under cmd, bash and PowerShell alike. If the
  script is missing (Orca removed), the hook exits 1 or 127, which Qwen logs and
  ignores.

Checked end to end with Qwen Code 0.26.5 against a stand-in for Orca's hook
endpoint, from both cmd and Git Bash: `SessionStart`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse` and `Stop` all arrived with the pane key and token.

### 6. Session export — `session-export/claude_to_qwen.py`

Converts a Claude Code transcript (Anthropic-shaped `content[]`) into a Qwen Code
transcript (Gemini-shaped `parts[]`) and registers it with the desktop app.
Thinking blocks map to `parts[].thought`; tool calls have no counterpart and are
flattened to one text line each. Files the session under the target workspace's
working directory, because the app never finds a transcript filed under the
source session's cwd. Dry run by default.

## Where the numbers come from

Everything is read-only. No app API, no injection into the agent's own logic.

| Source | Gives |
|---|---|
| `~/.qwen/usage/token-usage-YYYY-MM.jsonl` | per request: `inputTokens`, `outputTokens`, `cachedTokens`, `thoughtsTokens`, `apiDurationMs` |
| `~/.qwen/usage_record.jsonl` | per session: project path, tool calls, lines added/removed, skills |
| `~/.qwen/settings.json` | the real `contextWindowSize` of each configured model |
| `~/.craft-agent/workspaces/*/sessions/*/session.jsonl` | session status, thinking level |
| `~/.dscode/models.json` | price table (reused if present, optional) |

**Context used** is the `inputTokens` of the most recent request in that session —
a measured value, not an estimate. That is also how DSCode's own panel behaves.

**Cost** bills cached tokens at the `cacheRead` rate and only the remainder at the
input rate, so cached input is never double-counted:

```
cost = (input − cached)·price.input + cached·price.cacheRead + output·price.output
```

---

## Requirements

- Python 3.10+ (stdlib only — no pip install)
- On Windows, run it under WSL if your `python` is the Microsoft Store stub

## Caveats

- **App updates overwrite the injection.** After an update, re-run `inject.py`
  (`--status` tells you), or update with `update-qwen-desktop.ps1`, which does it.
- The in-app panel shows the **most recently active session**, since an injected
  script cannot read the app's React state to learn which session is on screen.
- Cost is an estimate from a static price table; it is not billing data.
- Paths default to a Windows layout; override with `--qwen-dir`, `--craft-dir`,
  `--dscode-models`, `--html`.

## License

MIT
