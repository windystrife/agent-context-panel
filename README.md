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
qwen-code-desktop/   monitor.py  panel.js  inject.py  start-monitor.cmd
                     openrouter_billing.py  opencode_go_usage.py
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

- **App updates overwrite the injection.** Qwen Code Desktop ships
  `electron-updater`; after it updates, re-run `inject.py`. `--status` tells you.
- The in-app panel shows the **most recently active session**, since an injected
  script cannot read the app's React state to learn which session is on screen.
- Cost is an estimate from a static price table; it is not billing data.
- Paths default to a Windows layout; override with `--qwen-dir`, `--craft-dir`,
  `--dscode-models`, `--html`.

## License

MIT
