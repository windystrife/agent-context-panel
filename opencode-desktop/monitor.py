#!/usr/bin/env python3
"""
OpenCode Desktop - conversation context monitor.

OpenCode Desktop shows the chat and the selected model, but no context usage:
upstream issues #5892, #34900 and #6152 all ask for it. The TUI has a meter,
the desktop app does not. The numbers are nevertheless already in OpenCode's
own database.

  ~/.local/share/opencode/opencode.db   session rows: tokens_*, cost, model, title
  ~/.cache/opencode/models.json         limit.context and cost.* per model

The database is copied before reading, so a running OpenCode never sees another
reader on it. Read-only; OpenCode is never written to.

  python3 monitor.py --port 8096

Port 8096, not 8097: Qwen Code Desktop's renderer unconditionally tries to load
http://localhost:8097 as a React DevTools script when served from file:, and
answering that with HTML just produces a console parse error.

NOTE ON VERIFICATION: the schema below was read from a real opencode.db, but at
the time of writing the `session`, `message` and `part` tables were all empty,
so the field *semantics* are inferred from the column names and not yet
confirmed against live data. Where a value can be read two ways, this exposes
both (see `context_used` vs `context_used_alt`) instead of silently picking one.
"""

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def win_homes():
    """Home directories to search: this user's, plus every Windows profile
    reachable from here. Lets one script work natively and from inside WSL,
    without hard-coding a user name. Set WIN_USER to pin one."""
    homes = [os.path.expanduser("~")]
    pinned = os.environ.get("WIN_USER")
    if pinned:
        homes = [f"/mnt/c/Users/{pinned}", f"C:/Users/{pinned}"] + homes
    skip = {"public", "default", "default user", "all users", "defaultuser0"}
    for root in ("/mnt/c/Users", "C:/Users"):
        try:
            for n in sorted(os.listdir(root)):
                if n.lower() in skip:
                    continue
                p = os.path.join(root, n)
                if os.path.isdir(p):
                    homes.append(p)
        except OSError:
            pass
    return homes


def candidates(*names):
    return [os.path.join(h, n) for h in win_homes() for n in names]


def first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


class Catalog:
    """models.dev catalog: model id -> context limit and prices."""

    def __init__(self, path):
        self.path = path
        self.by_id = {}
        self.by_qualified = {}
        self.load()

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for pid, prov in (data or {}).items():
            for mid, m in (prov.get("models") or {}).items():
                info = {
                    "context": ((m.get("limit") or {}).get("context")),
                    "max_output": ((m.get("limit") or {}).get("output")),
                    "cost": m.get("cost") or {},
                    "name": m.get("name"),
                    "provider": pid,
                }
                self.by_qualified[f"{pid}/{mid}"] = info
                self.by_id.setdefault(mid, info)
                self.by_id.setdefault(mid.split("/")[-1], info)

    def lookup(self, model):
        """session.model may be 'provider/model', bare, or a JSON blob."""
        if not model:
            return None
        if isinstance(model, str) and model.strip().startswith("{"):
            try:
                d = json.loads(model)
                model = d.get("modelID") or d.get("model") or d.get("id") or ""
                prov = d.get("providerID") or d.get("provider")
                if prov and f"{prov}/{model}" in self.by_qualified:
                    return self.by_qualified[f"{prov}/{model}"]
            except ValueError:
                pass
        return (self.by_qualified.get(model)
                or self.by_id.get(model)
                or self.by_id.get(str(model).split("/")[-1]))


def read_sessions(db_path):
    """Copy-then-read so the live app keeps exclusive use of its own file."""
    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy(db_path, tmp)
    try:
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        cols = {c[1] for c in con.execute('PRAGMA table_info("session")')}
        if not cols:
            return []
        rows = [dict(r) for r in con.execute(
            'SELECT * FROM "session" ORDER BY COALESCE(time_updated, time_created) DESC')]
        return rows
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class Monitor:
    def __init__(self, db, catalog):
        self.db = db
        self.catalog = catalog
        self.lock = threading.Lock()
        self.snap = {"sessions": [], "totals": {}, "error": None, "empty_db": True}

    def refresh(self):
        sessions, tot = [], {"sessions": 0, "input": 0, "output": 0,
                             "cache_read": 0, "cost": 0.0}
        for r in read_sessions(self.db):
            info = self.catalog.lookup(r.get("model")) or {}
            window = info.get("context")
            tin = r.get("tokens_input") or 0
            tout = r.get("tokens_output") or 0
            tcr = r.get("tokens_cache_read") or 0
            tcw = r.get("tokens_cache_write") or 0
            treason = r.get("tokens_reasoning") or 0

            # The prompt actually carried = fresh input + the cached prefix.
            used = tin + tcr
            s = {
                "id": r.get("id"),
                "title": r.get("title") or (r.get("slug") or ""),
                "model": r.get("model"),
                "model_name": info.get("name"),
                "directory": r.get("directory") or r.get("path"),
                "agent": r.get("agent"),
                "input": tin, "output": tout, "cache_read": tcr,
                "cache_write": tcw, "reasoning": treason,
                "context_window": window,
                "context_used": used,
                "context_used_alt": tin,          # if tokens_input already includes cache
                "context_pct": (100.0 * used / window) if window else None,
                "context_available": (window - used) if window else None,
                "cache_pct": (100.0 * tcr / used) if used else 0.0,
                "cost_db": r.get("cost"),
                "compacting": bool(r.get("time_compacting")),
                "archived": bool(r.get("time_archived")),
                "updated": r.get("time_updated") or r.get("time_created"),
                "files": r.get("summary_files"),
                "added": r.get("summary_additions"),
                "removed": r.get("summary_deletions"),
            }
            price = info.get("cost") or {}
            s["cost_calc"] = (tin * float(price.get("input", 0) or 0)
                              + tcr * float(price.get("cache_read", 0) or 0)
                              + tcw * float(price.get("cache_write", 0) or 0)
                              + tout * float(price.get("output", 0) or 0)) / 1e6
            s["priced"] = bool(price)
            sessions.append(s)

            tot["sessions"] += 1
            tot["input"] += tin
            tot["output"] += tout
            tot["cache_read"] += tcr
            tot["cost"] += (s["cost_db"] if isinstance(s["cost_db"], (int, float))
                            else s["cost_calc"])

        with self.lock:
            self.snap = {
                "sessions": sessions,
                "totals": tot,
                "empty_db": not sessions,
                "db": self.db,
                "catalog": self.catalog.path,
                "catalog_models": len(self.catalog.by_qualified),
                "updated": time.strftime("%H:%M:%S"),
                "error": None,
            }

    def loop(self, interval=3.0):
        while True:
            try:
                self.refresh()
            except Exception as e:
                with self.lock:
                    self.snap = dict(self.snap, error=f"{type(e).__name__}: {e}")
            time.sleep(interval)

    def get(self):
        with self.lock:
            return dict(self.snap)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>OpenCode Desktop - context</title>
<style>
:root{--bg:#16181d;--card:#1e2128;--line:#2c313b;--fg:#e6e8ec;--dim:#8b93a1;
      --ok:#818cf8;--warn:#fbbf24;--bad:#f87171;--green:#4ade80}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);padding:20px;
     font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",sans-serif}
h1{font-size:15px;margin:0 0 3px;font-weight:600}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:14px}
.label{color:var(--dim);font-size:12px}
.big{font-size:25px;font-weight:650;letter-spacing:-.4px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));margin-top:12px}
.b{background:#191c22;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.bar{height:7px;background:#0f1116;border-radius:4px;overflow:hidden;margin:8px 0 3px}
.bar>i{display:block;height:100%;border-radius:4px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:500;padding:7px 9px;border-bottom:1px solid var(--line)}
td{padding:7px 9px;border-bottom:1px solid #23262d;white-space:nowrap}
.mono{font-variant-numeric:tabular-nums}.right{text-align:right}
.note{background:#2a2416;border:1px solid #5b4a1f;color:#e8d9a8;border-radius:10px;padding:11px 13px;font-size:12.5px}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;background:#272b34;color:var(--dim)}
</style></head><body>
<h1>OpenCode Desktop - conversation context</h1>
<div class="sub" id="sub">loading...</div>
<div id="body"></div>
<script>
const f=(n,d=0)=>n==null?"-":Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const k=n=>n==null?"-":n>=1e6?(n/1e6).toFixed(1)+"m":n>=1e3?Math.round(n/1e3)+"k":String(n);
const col=p=>p==null?"var(--dim)":p>=90?"var(--bad)":p>=70?"var(--warn)":"var(--ok)";
async function tick(){
  let s; try{ s=await (await fetch("/api/stats")).json(); }catch(e){ return; }
  document.getElementById("sub").textContent =
    (s.catalog_models||0)+" models in catalog  -  "+(s.totals?.sessions||0)+" sessions  -  updated "+(s.updated||"");
  const el=document.getElementById("body");
  if(s.empty_db){
    el.innerHTML=`<div class="card"><div class="note">
      <b>No sessions in OpenCode's database yet.</b><br>
      The schema is wired up (<span class="mono">session.tokens_input / tokens_cache_read /
      tokens_output / cost / model</span>) but there is nothing to read until you run at least
      one OpenCode session. Run one, then reload - the numbers below will populate and can be
      checked against OpenCode's own TUI meter.</div>
      <div class="label" style="margin-top:10px">db: ${s.db||"?"}<br>catalog: ${s.catalog||"?"}</div></div>`;
    return;
  }
  const cur=s.sessions[0];
  el.innerHTML=`<div class="card">
    <div class="label">${cur.title||cur.id}</div>
    <div class="big mono">${k(cur.context_used)} <span class="label" style="font-size:13px">/ ${k(cur.context_window)}</span></div>
    <div class="bar"><i style="width:${Math.min(100,cur.context_pct||0)}%;background:${col(cur.context_pct)}"></i></div>
    <div class="label mono">${f(cur.context_pct,1)}% used - ${f(cur.context_available)} available
      ${cur.compacting?" - <b>compacting</b>":""}</div>
    <div class="grid">
      <div class="b"><div class="label">Input</div><div class="big mono" style="font-size:17px">${k(cur.input)}</div></div>
      <div class="b"><div class="label">Output</div><div class="big mono" style="font-size:17px">${k(cur.output)}</div></div>
      <div class="b"><div class="label">Cache read</div><div class="big mono" style="font-size:17px">${k(cur.cache_read)}</div></div>
      <div class="b"><div class="label">Reasoning</div><div class="big mono" style="font-size:17px">${k(cur.reasoning)}</div></div>
      <div class="b"><div class="label">Cost (db)</div><div class="big mono" style="font-size:17px">${cur.cost_db!=null?"$"+f(cur.cost_db,3):"-"}</div></div>
      <div class="b"><div class="label">Cost (recomputed)</div><div class="big mono" style="font-size:17px">${cur.priced?"$"+f(cur.cost_calc,3):"-"}</div></div>
    </div>
    <div class="label" style="margin-top:10px"><span class="pill">${cur.model_name||cur.model||"?"}</span>
      ${cur.agent?' <span class="pill">'+cur.agent+'</span>':""}
      ${cur.directory?" "+cur.directory:""}</div></div>
   <div class="card"><div class="label" style="margin-bottom:9px">All sessions</div>
   <div style="overflow-x:auto"><table>
   <tr><th>session</th><th>model</th><th class="right">context</th><th class="right">%</th>
   <th class="right">in</th><th class="right">out</th><th class="right">cache</th><th class="right">cost</th></tr>
   ${s.sessions.map(x=>`<tr><td>${(x.title||x.id||"").slice(0,40)}</td>
     <td><span class="pill">${(x.model_name||x.model||"?")}</span></td>
     <td class="mono right">${k(x.context_used)}/${k(x.context_window)}</td>
     <td class="mono right" style="color:${col(x.context_pct)}">${f(x.context_pct,1)}</td>
     <td class="mono right">${k(x.input)}</td><td class="mono right">${k(x.output)}</td>
     <td class="mono right">${k(x.cache_read)}</td>
     <td class="mono right">${x.cost_db!=null?"$"+f(x.cost_db,3):"-"}</td></tr>`).join("")}
   </table></div></div>`;
}
tick(); setInterval(tick,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    mon = None

    def do_GET(self):
        if self.path.startswith("/api/stats"):
            body, ctype = json.dumps(self.mon.get()).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()

    db = a.db or first_existing(candidates(".local/share/opencode/opencode.db"))
    cat = a.catalog or first_existing(candidates(".cache/opencode/models.json"))
    if not db:
        raise SystemExit("opencode.db not found - pass --db")

    mon = Monitor(db, Catalog(cat))
    mon.refresh()
    threading.Thread(target=mon.loop, daemon=True).start()
    Handler.mon = mon
    print(f"opencode-monitor: http://127.0.0.1:{a.port}\n  db      = {db}\n"
          f"  catalog = {cat} ({len(mon.catalog.by_qualified)} models)", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
