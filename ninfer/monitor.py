#!/usr/bin/env python3
"""
NInfer live monitor - server-side context/throughput dashboard.

Tails ninfer-serve logs and exposes the numbers a client-side panel would show
(context used, cache hit rate, tok/s, TTFT) -- but measured at the server, so it
works for every client at once (Claude Code, DSCode, curl, ...).

  python3 monitor.py --log ~/serve.log --port 8099

Stdlib only. Read-only: it never writes to the log or talks to the engine
except for an optional /health probe.
"""

import argparse
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- log parsing

# [req 127] anthropic_messages stream msgs=342 max_tokens=32000 (client) tools=32 ...
RE_SUBMIT = re.compile(
    r"\[req (?P<req>\d+)\] (?P<api>[a-z_]+) (?P<mode>stream|non-stream) "
    r"msgs=(?P<msgs>\d+) max_tokens=(?P<max_tokens>\d+)"
    r"(?: \(client\))?(?: tools=(?P<tools>\d+))?"
)

# [req 127] done finish=tool_calls tool_calls=1 prompt=129292 gen=238 cache=129240
#   reuse=append_frontier ttft=525ms prefill=153.0tok/s decode=38.3tok/s wall=6.72s
#   speculative=mtp 3.12tok/round (70.6%)
RE_DONE = re.compile(
    r"\[req (?P<req>\d+)\] done finish=(?P<finish>\S+)"
    r"(?: tool_calls=(?P<tool_calls>\d+))?"
    r" prompt=(?P<prompt>\d+) gen=(?P<gen>\d+) cache=(?P<cache>\d+)"
    r" reuse=(?P<reuse>\S+) ttft=(?P<ttft>\d+)ms"
    r" prefill=(?P<prefill>[\d.]+)tok/s decode=(?P<decode>[\d.]+)tok/s"
    r" wall=(?P<wall>[\d.]+)s"
    r"(?: speculative=(?P<spec>\S+) (?P<spec_rate>[\d.]+)tok/round \((?P<spec_acc>[\d.]+)%\))?"
)

# KV capacity explicit resolved=131072 tokens pages=2048/4096 runtime=5.32 GiB ...
RE_KV = re.compile(
    r"KV capacity (?P<mode>\w+) resolved=(?P<cap>\d+) tokens.*?"
    r"runtime=(?P<runtime>[\d.]+) GiB.*?free-after-startup=(?P<free>[\d.]+) (?P<free_unit>MiB|GiB)"
)
RE_LISTEN = re.compile(r"listening on (?P<url>\S+) \(model id: (?P<model>[^,]+), auth: (?P<auth>\w+)\)")
RE_TS = re.compile(r"^\[(?P<ts>[\d\-]+ [\d:.]+)\]")

# Which client is talking, inferred from the endpoint it used.
CLIENT_BY_API = {
    "anthropic_messages": "Claude Code",
    "openai_chat_completions": "OpenAI-compat (DSCode/other)",
    "openai_responses": "OpenAI Responses (DSCode)",
}


class State:
    """Everything the dashboard shows. Guarded by one lock."""

    def __init__(self, history=500):
        self.lock = threading.Lock()
        # All-time counters: never roll off, so "tokens in/out" really is the
        # session total and not just whatever fits the rolling window below.
        self.total = {"count": 0, "prompt": 0, "gen": 0, "cache": 0}
        self.kv_capacity = None
        self.kv_runtime_gib = None
        self.kv_free = None
        self.kv_mode = None
        self.model = None
        self.listen_url = None
        self.requests = deque(maxlen=history)     # completed, newest last
        self.pending = {}                         # req id -> submit info
        self.log_files = {}                       # path -> last size
        self.server_up = None
        self.last_event_ts = None

    # -- ingest ------------------------------------------------------------
    def feed(self, line):
        m = RE_KV.search(line)
        if m:
            free = float(m.group("free"))
            if m.group("free_unit") == "GiB":
                free *= 1024.0
            with self.lock:
                self.kv_capacity = int(m.group("cap"))
                self.kv_mode = m.group("mode")
                self.kv_runtime_gib = float(m.group("runtime"))
                self.kv_free = free            # always MiB
            return

        m = RE_LISTEN.search(line)
        if m:
            with self.lock:
                self.listen_url = m.group("url")
                self.model = m.group("model")
            return

        m = RE_SUBMIT.search(line)
        if m:
            with self.lock:
                self.pending[m.group("req")] = {
                    "api": m.group("api"),
                    "mode": m.group("mode"),
                    "msgs": int(m.group("msgs")),
                    "max_tokens": int(m.group("max_tokens")),
                    "tools": int(m.group("tools") or 0),
                }
            return

        m = RE_DONE.search(line)
        if m:
            ts = RE_TS.match(line)
            rec = {
                "req": int(m.group("req")),
                "ts": ts.group("ts") if ts else None,
                "finish": m.group("finish"),
                "tool_calls": int(m.group("tool_calls") or 0),
                "prompt": int(m.group("prompt")),
                "gen": int(m.group("gen")),
                "cache": int(m.group("cache")),
                "reuse": m.group("reuse"),
                "ttft_ms": int(m.group("ttft")),
                "prefill": float(m.group("prefill")),
                "decode": float(m.group("decode")),
                "wall": float(m.group("wall")),
                "spec_rate": float(m.group("spec_rate")) if m.group("spec_rate") else None,
                "spec_acc": float(m.group("spec_acc")) if m.group("spec_acc") else None,
            }
            with self.lock:
                info = self.pending.pop(m.group("req"), {})
                rec["api"] = info.get("api", "?")
                rec["client"] = CLIENT_BY_API.get(rec["api"], rec["api"])
                rec["tools"] = info.get("tools", 0)
                rec["msgs"] = info.get("msgs", 0)
                self.requests.append(rec)
                self.total["count"] += 1
                self.total["prompt"] += rec["prompt"]
                self.total["gen"] += rec["gen"]
                self.total["cache"] += rec["cache"]
                self.last_event_ts = rec["ts"]

    # -- derive ------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            reqs = list(self.requests)
            totals = dict(self.total)
            cap = self.kv_capacity
            out = {
                "model": self.model,
                "listen_url": self.listen_url,
                "kv_capacity": cap,
                "kv_mode": self.kv_mode,
                "kv_runtime_gib": self.kv_runtime_gib,
                "kv_free_mib": self.kv_free,
                "server_up": self.server_up,
                "last_event_ts": self.last_event_ts,
                "in_flight": len(self.pending),
                "log_files": dict(self.log_files),
            }

        if not reqs:
            out.update({"count": 0, "requests": []})
            return out

        last = reqs[-1]
        # Session totals come from the all-time counters; everything else is
        # computed over the rolling window (recent behaviour, not lifetime).
        tot_prompt = totals["prompt"]
        tot_cache = totals["cache"]
        tot_gen = totals["gen"]
        decodes = [r["decode"] for r in reqs if r["decode"] > 0]
        ttfts = [r["ttft_ms"] for r in reqs]
        accs = [r["spec_acc"] for r in reqs if r["spec_acc"] is not None]

        by_client = {}
        for r in reqs:
            c = by_client.setdefault(r["client"], {"count": 0, "gen": 0, "prompt": 0})
            c["count"] += 1
            c["gen"] += r["gen"]
            c["prompt"] += r["prompt"]

        out.update({
            "count": totals["count"],
            "window": len(reqs),
            "context_used": last["prompt"],
            "context_pct": (100.0 * last["prompt"] / cap) if cap else None,
            "context_peak": max(r["prompt"] for r in reqs),
            "cache_pct_last": (100.0 * last["cache"] / last["prompt"]) if last["prompt"] else 0.0,
            "cache_pct_all": (100.0 * tot_cache / tot_prompt) if tot_prompt else 0.0,
            "tokens_in": tot_prompt,
            "tokens_out": tot_gen,
            "tokens_cached": tot_cache,
            "decode_last": last["decode"],
            "decode_mean": sum(decodes) / len(decodes) if decodes else 0.0,
            "decode_min": min(decodes) if decodes else 0.0,
            "decode_max": max(decodes) if decodes else 0.0,
            "ttft_last": last["ttft_ms"],
            "ttft_mean": sum(ttfts) / len(ttfts),
            "spec_acc_mean": sum(accs) / len(accs) if accs else None,
            "by_client": by_client,
            "requests": reqs[-25:][::-1],
        })
        return out


def tail_files(state, paths, poll=0.5):
    """Follow several log files. Re-opens on truncation or replacement."""
    handles = {}

    def open_at_end(path, from_start):
        try:
            f = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return None
        if not from_start:
            f.seek(0, os.SEEK_END)
        return f

    # Backfill once so the dashboard is useful the moment it opens.
    for p in paths:
        f = open_at_end(p, from_start=True)
        if f:
            for line in f:
                state.feed(line)
            handles[p] = f
            with state.lock:
                state.log_files[p] = f.tell()

    while True:
        for p in paths:
            f = handles.get(p)
            if f is None:
                f = open_at_end(p, from_start=True)
                if f is None:
                    continue
                handles[p] = f
            try:
                size = os.path.getsize(p)
            except OSError:
                handles[p] = None
                continue
            if size < f.tell():          # truncated / replaced -> restart it
                f.close()
                f = open_at_end(p, from_start=True)
                handles[p] = f
                if f is None:
                    continue
            for line in f:
                state.feed(line)
            with state.lock:
                state.log_files[p] = f.tell()
        time.sleep(poll)


def probe_health(state, url, interval=5.0):
    while True:
        up = False
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                up = (r.status == 200)
        except (urllib.error.URLError, OSError, ValueError):
            up = False
        with state.lock:
            state.server_up = up
        time.sleep(interval)


# ---------------------------------------------------------------------- web

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>NInfer monitor</title>
<style>
:root{--bg:#16181d;--card:#1e2128;--line:#2c313b;--fg:#e6e8ec;--dim:#8b93a1;
      --ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--accent:#818cf8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",sans-serif;padding:20px}
h1{font-size:15px;margin:0 0 4px;font-weight:600}
.sub{color:var(--dim);font-size:12px;margin-bottom:18px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.label{color:var(--dim);font-size:12px;margin-bottom:6px}
.big{font-size:26px;font-weight:650;letter-spacing:-.5px}
.unit{font-size:13px;color:var(--dim);font-weight:400}
.bar{height:7px;background:#0f1116;border-radius:4px;overflow:hidden;margin-top:9px}
.bar>i{display:block;height:100%;border-radius:4px;transition:width .4s}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:500;padding:7px 9px;border-bottom:1px solid var(--line)}
td{padding:7px 9px;border-bottom:1px solid #23262d;white-space:nowrap}
tr:last-child td{border-bottom:none}
.mono{font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;background:#272b34;color:var(--dim)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
.right{text-align:right}
</style></head><body>
<h1><span id="dot" class="dot"></span><span id="title">NInfer monitor</span></h1>
<div class="sub" id="sub">connecting...</div>
<div class="grid" id="cards"></div>
<div class="card"><div class="label">Recent requests (server-side, all clients)</div>
<div style="overflow-x:auto"><table id="tbl"></table></div></div>
<script>
const f=(n,d=0)=>n==null?"-":Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
function color(p){return p>=90?"var(--bad)":p>=70?"var(--warn)":"var(--ok)"}
function card(label,big,unit,barPct,barColor,note){
  return `<div class="card"><div class="label">${label}</div>
  <div class="big mono">${big}<span class="unit"> ${unit||""}</span></div>
  ${barPct!=null?`<div class="bar"><i style="width:${Math.min(100,barPct)}%;background:${barColor}"></i></div>`:""}
  ${note?`<div class="label" style="margin:7px 0 0">${note}</div>`:""}</div>`;
}
async function tick(){
  let s; try{ s=await (await fetch("/api/stats")).json(); }catch(e){ return; }
  document.getElementById("dot").style.background = s.server_up ? "var(--ok)" : "var(--bad)";
  document.getElementById("title").textContent = "NInfer monitor - " + (s.model || "no model");
  document.getElementById("sub").textContent =
    (s.server_up ? "engine up" : "engine DOWN") +
    (s.listen_url ? " - " + s.listen_url : "") +
    (s.kv_capacity ? " - KV " + f(s.kv_capacity) + " tok (" + s.kv_mode + ", " + s.kv_runtime_gib + " GiB)" : "") +
    (s.kv_free_mib!=null ? " - " + f(s.kv_free_mib) + " MiB VRAM free after startup" : "") +
    " - " + f(s.count) + " requests seen" + (s.in_flight ? ", " + s.in_flight + " in flight" : "");

  const c=[];
  if(s.count){
    c.push(card("Context used (last request)", f(s.context_used), "/ "+f(s.kv_capacity)+" tok",
      s.context_pct, color(s.context_pct),
      (s.context_pct==null?"":f(s.context_pct,1)+"% - peak "+f(s.context_peak))));
    c.push(card("Cache hit (last)", f(s.cache_pct_last,1), "%", s.cache_pct_last, "var(--accent)",
      "session-wide "+f(s.cache_pct_all,1)+"% - "+f(s.tokens_cached)+" tok reused"));
    c.push(card("Decode", f(s.decode_last,1), "tok/s", null, null,
      "mean "+f(s.decode_mean,1)+" - range "+f(s.decode_min,1)+"-"+f(s.decode_max,1)));
    c.push(card("TTFT (last)", f(s.ttft_last), "ms", null, null, "mean "+f(s.ttft_mean)+" ms"));
    c.push(card("Tokens", f(s.tokens_in), "in", null, null, f(s.tokens_out)+" out"+
      (s.spec_acc_mean!=null?" - MTP accept "+f(s.spec_acc_mean,1)+"%":"")));
    const by=Object.entries(s.by_client||{}).map(([k,v])=>k+": "+v.count).join("<br>");
    c.push(card("Clients", Object.keys(s.by_client||{}).length, "", null, null, by||"none yet"));
  } else {
    c.push(card("Waiting for traffic", "0", "requests", null, null, "send a prompt from any client"));
  }
  document.getElementById("cards").innerHTML=c.join("");

  const rows=(s.requests||[]).map(r=>`<tr>
    <td class="mono">${r.req}</td>
    <td><span class="pill">${r.client}</span></td>
    <td class="mono right">${f(r.prompt)}</td>
    <td class="mono right">${r.prompt?f(100*r.cache/r.prompt,0):0}%</td>
    <td class="mono right">${f(r.gen)}</td>
    <td class="mono right">${f(r.decode,1)}</td>
    <td class="mono right">${f(r.ttft_ms)}</td>
    <td class="mono right">${r.tools||0}</td>
    <td>${r.reuse}</td><td>${r.finish}</td>
    <td class="mono" style="color:var(--dim)">${r.ts||""}</td></tr>`).join("");
  document.getElementById("tbl").innerHTML=
    `<tr><th>req</th><th>client</th><th class="right">prompt</th><th class="right">cache</th>
     <th class="right">gen</th><th class="right">tok/s</th><th class="right">ttft ms</th>
     <th class="right">tools</th><th>reuse</th><th>finish</th><th>time</th></tr>`+rows;
}
tick(); setInterval(tick,1000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    state = None

    def do_GET(self):
        if self.path.startswith("/api/stats"):
            body = json.dumps(self.state.snapshot()).encode()
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass          # keep stdout clean; this is a monitor, not a web server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", action="append", default=None,
                    help="ninfer-serve log to follow (repeatable)")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--health", default="http://127.0.0.1:8080/health")
    args = ap.parse_args()

    logs = [os.path.expanduser(p) for p in (args.log or ["~/serve.log"])]

    state = State()
    threading.Thread(target=tail_files, args=(state, logs), daemon=True).start()
    threading.Thread(target=probe_health, args=(state, args.health), daemon=True).start()

    Handler.state = state
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ninfer-monitor: http://127.0.0.1:{args.port}  following {', '.join(logs)}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
