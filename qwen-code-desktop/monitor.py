#!/usr/bin/env python3
"""
Qwen Code Desktop - conversation context monitor.

Qwen Code Desktop (v0.0.5, built by Craft Docs Ltd. on @craft-agent/*) tracks
token usage but never shows it: the only `contextWindow` string in its UI is the
provider-setup field label. This rebuilds the panel it is missing, from the
records the app already writes:

  ~/.qwen/usage/token-usage-YYYY-MM.jsonl   per request: input/output/cached/thoughts
  ~/.qwen/usage_record.jsonl                per session: project, tools, files, skills
  ~/.qwen/settings.json                     contextWindowSize per configured model
  ~/.craft-agent/workspaces/*/sessions/     session status / thinking level
  ~/.dscode/models.json                     price table (reused, optional)

Read-only. Stdlib only.

  python3 monitor.py --port 8098
"""

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_CONTEXT = 128000


def candidates(*names):
    """Same home dir seen from Windows or from WSL."""
    out = []
    for n in names:
        out.append(os.path.expanduser("~/" + n))
        user = os.environ.get("WIN_USER", "tungnt")
        out.append(f"/mnt/c/Users/{user}/" + n)
        out.append(f"C:/Users/{user}/" + n)
    return out


def first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_jsonl(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


class Monitor:
    def __init__(self, qwen_dir, craft_dir, dscode_models):
        self.qwen_dir = qwen_dir
        self.craft_dir = craft_dir
        self.dscode_models = dscode_models
        self.lock = threading.Lock()
        self.snap = {"sessions": [], "totals": {}, "error": None}

    # -- static config -----------------------------------------------------
    def load_context_windows(self):
        """Model id -> context window. Authoritative source is the user's own
        Qwen settings; DSCode's table fills any gap."""
        ctx, active = {}, None
        s = read_json(os.path.join(self.qwen_dir, "settings.json"), {}) or {}
        active = (s.get("model") or {}).get("name")
        for prov in (s.get("modelProviders") or {}).get("openai", []) or []:
            size = (prov.get("generationConfig") or {}).get("contextWindowSize")
            if prov.get("id") and size:
                ctx[prov["id"]] = int(size)
        if self.dscode_models:
            d = read_json(self.dscode_models, {}) or {}
            for prov in (d.get("providers") or {}).values():
                for m in prov.get("models", []) or []:
                    if m.get("id") and m.get("contextWindow"):
                        ctx.setdefault(m["id"], int(m["contextWindow"]))
        return ctx, active

    def load_prices(self):
        """Model id -> {input, output, cacheRead} in USD per 1M tokens."""
        prices = {}
        if not self.dscode_models:
            return prices
        d = read_json(self.dscode_models, {}) or {}
        for prov in (d.get("providers") or {}).values():
            for m in prov.get("models", []) or []:
                if m.get("id") and isinstance(m.get("cost"), dict):
                    prices.setdefault(m["id"], m["cost"])
        return prices

    def load_session_meta(self):
        meta = {}
        if not self.craft_dir or not os.path.isdir(self.craft_dir):
            return meta
        for ws in os.listdir(self.craft_dir):
            sdir = os.path.join(self.craft_dir, ws, "sessions")
            if not os.path.isdir(sdir):
                continue
            for sid in os.listdir(sdir):
                rows = read_jsonl(os.path.join(sdir, sid, "session.jsonl"))
                if rows:
                    r = rows[0]
                    meta[sid] = {
                        "workspace": ws,
                        "status": r.get("sessionStatus"),
                        "thinking": r.get("thinkingLevel"),
                        "flagged": bool(r.get("isFlagged")),
                        "unread": bool(r.get("hasUnread")),
                    }
        return meta

    # -- refresh -----------------------------------------------------------
    def refresh(self):
        usage_dir = os.path.join(self.qwen_dir, "usage")
        records = []
        if os.path.isdir(usage_dir):
            for fn in sorted(os.listdir(usage_dir)):
                if fn.startswith("token-usage-") and fn.endswith(".jsonl"):
                    records.extend(read_jsonl(os.path.join(usage_dir, fn)))
        records.sort(key=lambda r: r.get("timestamp") or "")

        rollup = {}
        for r in read_jsonl(os.path.join(self.qwen_dir, "usage_record.jsonl")):
            sid = r.get("sessionId")
            if not sid:
                continue
            cur = rollup.setdefault(sid, {"project": r.get("project"), "tools": 0,
                                          "files_add": 0, "files_del": 0, "skills": 0})
            cur["project"] = r.get("project") or cur["project"]
            cur["tools"] += (r.get("tools") or {}).get("totalCalls", 0)
            cur["skills"] += (r.get("skills") or {}).get("totalCalls", 0)
            cur["files_add"] += (r.get("files") or {}).get("linesAdded", 0)
            cur["files_del"] += (r.get("files") or {}).get("linesRemoved", 0)

        ctx_windows, active_model = self.load_context_windows()
        prices = self.load_prices()
        meta = self.load_session_meta()

        sessions = {}
        for r in records:
            sid = r.get("sessionId")
            if not sid:
                continue
            s = sessions.setdefault(sid, {
                "session": sid, "requests": 0, "input": 0, "output": 0,
                "cached": 0, "thoughts": 0, "latency_ms": 0,
                "models": {}, "first": r.get("timestamp"), "last": None,
            })
            s["requests"] += 1
            s["input"] += r.get("inputTokens", 0)
            s["output"] += r.get("outputTokens", 0)
            s["cached"] += r.get("cachedTokens", 0)
            s["thoughts"] += r.get("thoughtsTokens", 0)
            s["latency_ms"] += r.get("apiDurationMs", 0)
            s["last"] = r.get("timestamp")
            s["last_input"] = r.get("inputTokens", 0)
            s["last_output"] = r.get("outputTokens", 0)
            s["last_cached"] = r.get("cachedTokens", 0)
            s["model"] = r.get("model")
            s["models"][r.get("model")] = s["models"].get(r.get("model"), 0) + 1

        out, tot = [], {"requests": 0, "input": 0, "output": 0, "cached": 0, "cost": 0.0}
        for sid, s in sessions.items():
            window = ctx_windows.get(s["model"], DEFAULT_CONTEXT)
            used = s["last_input"]
            s["context_window"] = window
            s["context_used"] = used
            s["context_pct"] = 100.0 * used / window if window else None
            s["context_available"] = max(0, window - used)
            s["cache_pct"] = 100.0 * s["cached"] / s["input"] if s["input"] else 0.0
            s["last_cache_pct"] = (100.0 * s["last_cached"] / s["last_input"]
                                   if s["last_input"] else 0.0)

            p = prices.get(s["model"]) or {}
            billed_input = max(0, s["input"] - s["cached"])
            s["cost"] = (billed_input * float(p.get("input", 0) or 0)
                         + s["cached"] * float(p.get("cacheRead", 0) or 0)
                         + s["output"] * float(p.get("output", 0) or 0)) / 1_000_000.0
            s["priced"] = bool(p)

            s.update(meta.get(sid, {}))
            s.update({k: v for k, v in (rollup.get(sid) or {}).items()})
            out.append(s)

            tot["requests"] += s["requests"]
            tot["input"] += s["input"]
            tot["output"] += s["output"]
            tot["cached"] += s["cached"]
            tot["cost"] += s["cost"]

        out.sort(key=lambda s: s["last"] or "", reverse=True)
        with self.lock:
            self.snap = {
                "sessions": out,
                "totals": tot,
                "active_model": active_model,
                "context_windows": ctx_windows,
                "qwen_dir": self.qwen_dir,
                "craft_dir": self.craft_dir,
                "priced_from": self.dscode_models,
                "updated": time.strftime("%H:%M:%S"),
            }

    def loop(self, interval=2.0):
        while True:
            try:
                self.refresh()
            except Exception as e:                      # never die on bad data
                with self.lock:
                    self.snap = dict(self.snap, error=f"{type(e).__name__}: {e}")
            time.sleep(interval)

    def get(self):
        with self.lock:
            return dict(self.snap)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Qwen Code Desktop - context</title>
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
.big{font-size:26px;font-weight:650;letter-spacing:-.5px}
.row{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.sep{border-top:1px solid var(--line);margin:14px 0}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.box{background:#191c22;border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.bar{height:7px;background:#0f1116;border-radius:4px;overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;border-radius:4px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:500;padding:7px 9px;border-bottom:1px solid var(--line)}
td{padding:7px 9px;border-bottom:1px solid #23262d;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr.sel td{background:#232735}
.mono{font-variant-numeric:tabular-nums}
.right{text-align:right}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;background:#272b34;color:var(--dim)}
.clk{cursor:pointer}
</style></head><body>
<h1>Qwen Code Desktop - conversation context</h1>
<div class="sub" id="sub">loading...</div>
<div class="card" id="panel"></div>
<div class="card"><div class="label" style="margin-bottom:9px">Sessions (click one to inspect)</div>
<div style="overflow-x:auto"><table id="tbl"></table></div></div>
<script>
let sel=null, last=null;
const f=(n,d=0)=>n==null?"-":Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const k=n=>n==null?"-":n>=1e6?(n/1e6).toFixed(1)+"m":n>=1e3?Math.round(n/1e3)+"k":String(n);
const col=p=>p>=90?"var(--bad)":p>=70?"var(--warn)":"var(--ok)";
function ring(pct){
  const C=2*Math.PI*54, off=C*(1-Math.min(100,pct||0)/100);
  return `<svg width="132" height="132" viewBox="0 0 132 132">
   <circle cx="66" cy="66" r="54" fill="none" stroke="#0f1116" stroke-width="11"/>
   <circle cx="66" cy="66" r="54" fill="none" stroke="${col(pct)}" stroke-width="11"
     stroke-linecap="round" stroke-dasharray="${C}" stroke-dashoffset="${off}"
     transform="rotate(-90 66 66)"/>
   <text x="66" y="62" text-anchor="middle" fill="#e6e8ec" font-size="21" font-weight="650">${f(pct,1)}%</text>
   <text x="66" y="80" text-anchor="middle" fill="#8b93a1" font-size="11">used</text></svg>`;
}
function render(s){
  last=s;
  document.getElementById("sub").textContent =
    "active model: " + (s.active_model||"?") + "  -  " + f((s.totals||{}).requests) +
    " requests across " + (s.sessions||[]).length + " sessions  -  updated " + (s.updated||"");
  const list=s.sessions||[];
  const cur=list.find(x=>x.session===sel) || list[0];
  const p=document.getElementById("panel");
  if(!cur){ p.innerHTML='<div class="label">No usage records yet.</div>'; return; }
  p.innerHTML=`
   <div class="row">
     ${ring(cur.context_pct)}
     <div style="min-width:200px">
       <div class="label">Context capacity</div>
       <div class="big mono">${k(cur.context_used)} <span class="label">/ ${k(cur.context_window)}</span></div>
       <div class="label">${f(cur.context_available)} available &nbsp;·&nbsp; last reply ${f(cur.last_output)} tok</div>
       <div class="label" style="margin-top:6px"><span class="pill">${cur.model||"?"}</span></div>
     </div>
     <div style="flex:1;min-width:260px">
       <div class="grid2">
         <div class="box"><div class="label">Total tokens</div><div class="big mono">${k(cur.input+cur.output)}</div></div>
         <div class="box"><div class="label">Cost (est.)</div><div class="big mono">${cur.priced?"$"+f(cur.cost,2):"local / free"}</div></div>
         <div class="box"><div class="label">Input</div><div class="big mono">${k(cur.input)}</div></div>
         <div class="box"><div class="label">Output</div><div class="big mono">${k(cur.output)}</div></div>
       </div>
     </div>
   </div>
   <div class="sep"></div>
   <div class="label">Cache <b style="color:var(--fg)">${f(cur.cache_pct,0)}%</b>
     &nbsp;·&nbsp; read ${k(cur.cached)} &nbsp;·&nbsp; last request ${f(cur.last_cache_pct,0)}%
     &nbsp;·&nbsp; thinking tokens ${k(cur.thoughts)}</div>
   <div class="bar"><i style="width:${Math.min(100,cur.cache_pct)}%;background:var(--green)"></i></div>
   <div class="label" style="margin-top:10px">${cur.requests} requests
     &nbsp;·&nbsp; ${f(cur.latency_ms/1000,1)}s total API time
     ${cur.tools!=null?" &nbsp;·&nbsp; "+cur.tools+" tool calls":""}
     ${cur.status?" &nbsp;·&nbsp; "+cur.status:""}${cur.thinking?" &nbsp;·&nbsp; thinking "+cur.thinking:""}</div>
   ${cur.project?`<div class="label" style="margin-top:4px">${cur.project}</div>`:""}`;

  document.getElementById("tbl").innerHTML =
   `<tr><th>session</th><th>model</th><th class="right">context</th><th class="right">%</th>
    <th class="right">in</th><th class="right">out</th><th class="right">cache</th>
    <th class="right">req</th><th class="right">cost</th><th>last</th></tr>` +
   list.map(x=>`<tr class="clk ${x.session===cur.session?'sel':''}" data-id="${x.session}">
     <td class="mono">${x.session.slice(0,8)}</td>
     <td><span class="pill">${(x.model||"?").split("/").pop()}</span></td>
     <td class="mono right">${k(x.context_used)}/${k(x.context_window)}</td>
     <td class="mono right" style="color:${col(x.context_pct)}">${f(x.context_pct,1)}</td>
     <td class="mono right">${k(x.input)}</td><td class="mono right">${k(x.output)}</td>
     <td class="mono right">${f(x.cache_pct,0)}%</td>
     <td class="mono right">${x.requests}</td>
     <td class="mono right">${x.priced?"$"+f(x.cost,2):"-"}</td>
     <td class="mono" style="color:var(--dim)">${(x.last||"").replace("T"," ").slice(5,16)}</td></tr>`).join("");
  document.querySelectorAll("#tbl tr.clk").forEach(tr=>
    tr.onclick=()=>{ sel=tr.dataset.id; render(last); });
}
async function tick(){ try{ render(await (await fetch("/api/stats")).json()); }catch(e){} }
tick(); setInterval(tick,2000);
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
        # The in-app panel is injected into Qwen Code Desktop's renderer, which
        # is a different origin (file:/app:), so it needs CORS to read this.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qwen-dir", default=None)
    ap.add_argument("--craft-dir", default=None)
    ap.add_argument("--dscode-models", default=None)
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    qwen = args.qwen_dir or first_existing(candidates(".qwen"))
    craft_root = args.craft_dir or first_existing(candidates(".craft-agent"))
    craft = os.path.join(craft_root, "workspaces") if craft_root else None
    ds = args.dscode_models or first_existing(candidates(".dscode/models.json"))

    if not qwen:
        raise SystemExit("could not find ~/.qwen - pass --qwen-dir")

    mon = Monitor(qwen, craft, ds)
    mon.refresh()
    threading.Thread(target=mon.loop, daemon=True).start()
    Handler.mon = mon
    print(f"qwen-monitor: http://127.0.0.1:{args.port}\n  qwen   = {qwen}\n"
          f"  craft  = {craft}\n  prices = {ds}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
