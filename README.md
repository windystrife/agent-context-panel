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
ninfer/              monitor.py  start-ninfer.cmd
opencode-desktop/    (in progress)
```

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

The panel is injected **inline** because the app's CSP is
`script-src 'self' 'unsafe-inline' …` while a separate file under a `file:`/`app:`
origin is not reliably `'self'`. It renders inside a Shadow DOM, so no styles
leak in either direction, and it reads the dashboard's API over
`connect-src … http://127.0.0.1:*`, which that same CSP already allows.

This works because Qwen Code Desktop ships its renderer **unpacked**
(`resources/app/dist/renderer/`) rather than inside an `app.asar`.

### 3. Server-side monitor — `ninfer/monitor.py`

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
