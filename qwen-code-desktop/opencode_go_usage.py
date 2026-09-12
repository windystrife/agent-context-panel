"""
OpenCode Go subscription usage, for the context monitor.

OpenCode Go is a subscription with per-model dollar budgets, not a pay-per-
request API, so "spend" means how much of the limit is used. Measured
2026-09-13, which decides where each number comes from:

- the `cost` field OpenCode puts on every chat response is always "0", even
  for a 4,020-token prompt, so it cannot be used for accounting at all
- GET /zen/go/v1/usage (not in the docs) returns the account's real standing:
    rolling (5-hour), weekly (resets Monday 00:00 UTC) and monthly (resets on
    the subscription anniversary), each as status / percent / resetsAt.
  A ?model= parameter is ignored, so this is account-wide, and percent is an
  integer, so small traffic reads as 0%.
- per-model numbers can only come from what Qwen Code logs (request count and
  tokens, which are measured) priced from the models.dev catalog (an estimate:
  there is no per-model figure to check it against).

Documented limits: each model's 5-hour window is 20% of its monthly budget,
weekly 50%, monthly 100%; budgets are $60, $30 or $15 per month by model.

The key is only placed in an Authorization header and never logged.
"""

import datetime
import glob
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request

API = "https://opencode.ai/zen/go/v1"

BUDGET = {}      # model id -> documented monthly budget in USD
for _usd, _ids in (
    (60, ["glm-5.3-flash", "glm-5.2", "glm-5.1", "kimi-k2.7-code", "kimi-k2.6", "longcat-2.0",
          "mimo-v2.5", "minimax-m3", "minimax-m2.7", "muse-spark-1.3-contributor",
          "muse-spark-1.2-contributor", "qwen3.7-plus", "qwen3.6-plus", "deepseek-v4-flash", "hy3"]),
    (30, ["qwen3.8-flash", "deepseek-v4-flash-vision-exp"]),
    (15, ["glm-5.3", "kimi-k3", "mimo-v2.5-pro", "qwen3.8-max", "qwen3.7-max", "grok-4.6",
          "gpt-5.6-luna", "deepseek-v4.1-flash", "deepseek-v4-pro", "hy4-preview"]),
):
    for _m in _ids:
        BUDGET[_m] = _usd
WINDOW_SHARE = {"5h": 0.20, "week": 0.50, "month": 1.00}


def find_key(candidates, env_files=()):
    env = os.environ.get("OPENCODE_GO_API_KEY")
    if env and env.strip():
        return env.strip()
    for p in env_files:
        try:
            for line in io.open(p, encoding="utf-8"):
                if line.startswith("OPENCODE_GO_API_KEY="):
                    v = line.split("=", 1)[1].strip()
                    if v:
                        return v
        except OSError:
            continue
    for p in candidates:
        try:
            with io.open(p, encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except OSError:
            continue
    return None


def _price(catalog_cost, prompt_tokens):
    tier = catalog_cost or {}
    for t in (catalog_cost or {}).get("tiers") or []:
        if prompt_tokens > (t.get("tier") or {}).get("size", float("inf")):
            tier = t
    return tier


def _ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class OpenCodeGo:
    def __init__(self, qwen_dir, key, catalog_path):
        self.qwen_dir = qwen_dir
        self.key = key
        self.lock = threading.Lock()
        self.limits = {}
        self.limits_error = None
        self.events = {}             # file -> [(ts, model, prompt, cached, output)]
        self.file_mtime = {}
        self.catalog = {}
        try:
            with io.open(catalog_path, encoding="utf-8") as f:
                self.catalog = (json.load(f).get("opencode-go") or {}).get("models") or {}
        except (OSError, ValueError, TypeError):
            pass

    def refresh_limits(self):
        req = urllib.request.Request(API + "/usage", headers={
            "Authorization": "Bearer " + self.key, "User-Agent": "qwen-code"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                usage = (json.loads(r.read().decode("utf-8")) or {}).get("usage") or {}
            with self.lock:
                self.limits = usage
                self.limits_error = None
        except urllib.error.HTTPError as e:
            with self.lock:
                self.limits_error = "HTTP %s" % e.code
        except (urllib.error.URLError, OSError, ValueError) as e:
            with self.lock:
                self.limits_error = type(e).__name__

    def scan(self):
        """OpenCode Go requests Qwen logged: model in the opencode-go catalog and
        a non-OpenRouter response id (OpenRouter's all start with gen-)."""
        ids = set(self.catalog)
        for f in glob.glob(os.path.join(self.qwen_dir, "projects", "**", "*.jsonl"), recursive=True):
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            if self.file_mtime.get(f) == mt:
                continue
            rows = []
            try:
                with io.open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if "api_response" not in line:
                            continue
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        ev = (r.get("systemPayload") or {}).get("uiEvent") or {}
                        if ev.get("event.name") != "qwen-code.api_response":
                            continue
                        model = ev.get("model") or ""
                        if model not in ids or str(ev.get("response_id") or "").startswith("gen-"):
                            continue
                        rows.append((_ts(ev.get("event.timestamp") or r.get("timestamp")), model,
                                     int(ev.get("input_token_count") or 0),
                                     int(ev.get("cached_content_token_count") or 0),
                                     int(ev.get("output_token_count") or 0)))
            except OSError:
                continue
            with self.lock:
                self.events[f] = rows
            self.file_mtime[f] = mt

    def loop(self):
        last = 0
        while True:
            try:
                self.scan()
                if time.time() - last > 60:
                    self.refresh_limits()
                    last = time.time()
            except Exception:                    # never take the monitor down
                pass
            time.sleep(5)

    def summary(self):
        now = time.time()
        monday = datetime.datetime.now(datetime.timezone.utc)
        monday = (monday - datetime.timedelta(days=monday.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.lock:
            rows = [e for v in self.events.values() for e in v]
            limits, err = dict(self.limits), self.limits_error
        month_start = _ts((limits.get("monthly") or {}).get("resetsAt"))
        month_start = (month_start - 30 * 86400) if month_start else now - 30 * 86400
        starts = {"5h": now - 5 * 3600, "week": monday, "month": month_start}

        models = {}
        for ts, model, prompt, cached, out in rows:
            m = models.setdefault(model, {
                "requests": 0, "input": 0, "cached": 0, "output": 0,
                "est": {"5h": 0.0, "week": 0.0, "month": 0.0},
                "budget_month": BUDGET.get(model)})
            m["requests"] += 1
            m["input"] += prompt
            m["cached"] += cached
            m["output"] += out
            p = _price((self.catalog.get(model) or {}).get("cost"), prompt)
            usd = (max(0, prompt - cached) * p.get("input", 0)
                   + cached * p.get("cache_read", 0) + out * p.get("output", 0)) / 1e6
            for w, start in starts.items():
                if ts is None or ts >= start:
                    m["est"][w] += usd
        for m in models.values():
            b = m["budget_month"]
            m["est_pct"] = ({w: (100.0 * m["est"][w] / (b * WINDOW_SHARE[w])) for w in WINDOW_SHARE}
                            if b else None)
        return {"limits": limits, "limits_error": err, "models": models,
                "requests": sum(m["requests"] for m in models.values())}
