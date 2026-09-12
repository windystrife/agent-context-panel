"""
What OpenRouter actually billed, for the context monitor.

Measured 2026-09-13 on 30 generations: the token counts Qwen Code records match
OpenRouter's own to the token, but a cost computed from a price table came out
exactly 2.00x low. OpenRouter routes each request to one of ~12 providers with
different prices; DeepSeek's headline is $0.15 / $0.60 per 1M, while GMICloud,
Novita and SiliconFlow bill $0.30 / $1.20. A price table cannot know which one
served a request. OpenRouter's /api/v1/generation can, so this resolves every
generation id Qwen logged into its real billed cost.

Two OpenRouter behaviours shape the design:
- a generation's stats are NOT available right after the request; the first
  successful lookup took ~49 s, and a lookup before that is a plain 404
- account-wide spend lives on /api/v1/key (usage, usage_daily/weekly/monthly)
  and /api/v1/credits (purchased vs used), which is the "total money" figure

Resolved costs are cached on disk, so a restart does not refetch history.
The API key is only ever placed in an Authorization header; it is never logged.
Stdlib only.
"""

import glob
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request

API = "https://openrouter.ai/api/v1"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "agent-context-panel")
CACHE_FILE = os.path.join(CACHE_DIR, "openrouter-billed.json")
MAX_ATTEMPTS = 25                    # give up on an id after this many 404s


def find_key(candidates):
    env = os.environ.get("OPENROUTER_API_KEY")
    if env and env.strip():
        return env.strip()
    for p in candidates:
        try:
            with io.open(p, encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except OSError:
            continue
    return None


class Billing:
    def __init__(self, qwen_dir, key):
        self.qwen_dir = qwen_dir
        self.key = key
        self.lock = threading.Lock()
        self.cache = self._load_cache()          # gen id -> resolved record or miss info
        self.gens = {}                           # session id -> [gen ids]
        self.gen_model = {}                      # gen id -> model Qwen reported
        self.file_mtime = {}
        self.account = {}
        self.account_error = None
        self._dirty = 0

    # -- persistence ----------------------------------------------------------
    def _load_cache(self):
        try:
            with io.open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_cache(self):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = CACHE_FILE + ".tmp"
            with self.lock:
                data = json.dumps(self.cache)
            with io.open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, CACHE_FILE)
        except OSError:
            pass

    # -- http -------------------------------------------------------------------
    def _get(self, path):
        req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + self.key})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return 0, None

    # -- which generations exist ------------------------------------------------
    def scan(self):
        """Collect OpenRouter generation ids per session from Qwen's transcripts.
        Only files whose mtime changed are re-read."""
        pattern = os.path.join(self.qwen_dir, "projects", "**", "*.jsonl")
        for f in glob.glob(pattern, recursive=True):
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            if self.file_mtime.get(f) == mt:
                continue
            sid = os.path.basename(f)[:-6]
            ids = []
            try:
                with io.open(f, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"gen-' not in line or "api_response" not in line:
                            continue
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        ev = (r.get("systemPayload") or {}).get("uiEvent") or {}
                        gid = ev.get("response_id") or ""
                        if ev.get("event.name") == "qwen-code.api_response" and gid.startswith("gen-"):
                            ids.append(gid)
                            self.gen_model[gid] = ev.get("model")
            except OSError:
                continue
            with self.lock:
                self.gens[sid] = ids
            self.file_mtime[f] = mt

    # -- resolve ids into billed cost ------------------------------------------
    def _due(self, gid, now):
        c = self.cache.get(gid)
        if c is None:
            return True
        if "cost" in c:
            return False
        return c.get("attempts", 0) < MAX_ATTEMPTS and c.get("next", 0) <= now

    def resolve_some(self, budget=8):
        now = time.time()
        with self.lock:
            pending = [g for ids in self.gens.values() for g in ids if self._due(g, now)]
        done = 0
        for gid in pending[:budget]:
            status, body = self._get("/generation?id=" + gid)
            data = (body or {}).get("data") or {}
            with self.lock:
                if status == 200 and data:
                    self.cache[gid] = {
                        "cost": float(data.get("total_cost") or 0.0),
                        "provider": data.get("provider_name"),
                        "model": data.get("model"),
                    }
                else:
                    prev = self.cache.get(gid) or {}
                    n = prev.get("attempts", 0) + 1
                    # 404 is normal for the first ~minute; back off, then give up
                    self.cache[gid] = {"attempts": n, "status": status,
                                       "next": time.time() + min(3600, 30 * (2 ** min(n, 7)))}
            done += 1
            self._dirty += 1
            time.sleep(0.2)
        if self._dirty >= 20 or (done and not pending[budget:]):
            self._save_cache()
            self._dirty = 0
        return done

    def refresh_account(self):
        s1, key = self._get("/key")
        s2, credits = self._get("/credits")
        acct = {}
        kd = (key or {}).get("data") or {}
        if s1 == 200:
            for k in ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit", "limit_remaining"):
                acct[k] = kd.get(k)
        cd = (credits or {}).get("data") or {}
        if s2 == 200:
            total, used = cd.get("total_credits"), cd.get("total_usage")
            acct["credits_total"] = total
            acct["credits_used"] = used
            if isinstance(total, (int, float)) and isinstance(used, (int, float)):
                acct["balance"] = total - used
        with self.lock:
            self.account = acct
            self.account_error = None if acct else "key %s / credits %s" % (s1, s2)
            self.account["updated"] = time.time()

    def loop(self):
        last_acct = 0
        while True:
            try:
                self.scan()
                self.resolve_some()
                if time.time() - last_acct > 60:
                    self.refresh_account()
                    last_acct = time.time()
            except Exception:                    # never take the monitor down
                pass
            time.sleep(3)

    # -- views ------------------------------------------------------------------
    def session(self, sid):
        with self.lock:
            ids = list(self.gens.get(sid) or [])
            billed, resolved, providers = 0.0, 0, {}
            for g in ids:
                c = self.cache.get(g) or {}
                if "cost" in c:
                    billed += c["cost"]
                    resolved += 1
                    p = c.get("provider") or "?"
                    providers[p] = providers.get(p, 0) + 1
        return {"billed": billed, "resolved": resolved, "total": len(ids), "providers": providers}

    def summary(self):
        with self.lock:
            ids = [g for v in self.gens.values() for g in v]
            resolved = [self.cache[g] for g in ids if "cost" in (self.cache.get(g) or {})]
            return {
                "generations": len(ids),
                "resolved": len(resolved),
                "billed": sum(c["cost"] for c in resolved),
                "account": dict(self.account),
                "account_error": self.account_error,
            }
