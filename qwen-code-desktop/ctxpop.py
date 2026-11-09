#!/usr/bin/env python3
"""
Qwen Code Desktop 0.3.x: make the footer "context" button open a dismissible
popover (like Claude Code's context-window breakdown) instead of pasting
"/context" + its answer into the chat as messages.

  python3 ctxpop.py            # patch
  python3 ctxpop.py --status   # patched / clean
  python3 ctxpop.py --revert   # restore the pristine bundle

What it changes (one file, the web-shell's main bundle):
  * adds a small React component QCtxPop that renders the app's OWN
    "Context Usage" panel (the same one the right panel uses for the
    context_usage tab) inside a fixed popover anchored at the last click;
    Esc, a click outside, or clicking the button again closes it;
  * rewires the footer handler that used to call Tx("/context") — i.e.
    echo "/context" into the chat and append the result as a message — to
    toggle that popover;
  * mounts the popover once, next to the composer, so it works for both the
    compact footer and the full toolbar.

Typing "/context" yourself still works the old way (it is a chat command).

Newer builds (the web-shell bundle from 2026-10-19 on; 0.26.5 here) have this
natively: hovering the context chip opens a popover, a click opens the docked
panel. On those, --status says "native" and patching is a no-op (exit 0).

Safe by construction: one-time pristine backup, refuses to patch twice, every
anchor must match exactly once (regexes capture the minified names, so a
re-minified build either matches fully or is refused), read-back check, and
an optional syntax check with the app's own bundled node.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

MARK = "QCtxPop"  # present in the bundle only after patching

DEFAULT_WEB_SHELL = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local")),
    "Qwen Code Desktop", "runtime", "qwen-code", "lib", "web-shell")


def read(p):
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    if read(p) != s:
        raise SystemExit("READBACK MISMATCH - file not written correctly")


def find_bundle(web_shell):
    html = os.path.join(web_shell, "index.html")
    if not os.path.exists(html):
        raise SystemExit(f"not found: {html}")
    m = re.findall(r'<script type="module" crossorigin src="/assets/(index-[^"]+\.js)">', read(html))
    if len(m) != 1:
        raise SystemExit(f"expected exactly one module entry script in index.html, found {len(m)}")
    return os.path.join(web_shell, "assets", m[0])


def backup_path(bundle):
    return bundle + ".bak-before-ctxpop"


# Qwen Code Desktop 0.26 ships its own popover for the context chip: hovering
# opens a card with the usage and a compress button, and a click opens the
# docked "Context Usage" panel. Its component takes these props, in this order.
NATIVE = re.compile(r'function \w+\(\{tokenCount:\w+,contextWindow:\w+,known:\w+,'
                    r'controls:\w+,onOpenDetails:\w+,')


def status(bundle):
    if not os.path.exists(bundle):
        return "missing"
    src = read(bundle)
    if MARK in src:
        return "patched"
    return "native" if NATIVE.search(src) else "clean"


def one(pattern, src, what):
    ms = list(re.finditer(pattern, src))
    if len(ms) != 1:
        raise SystemExit(f"anchor for {what} matched {len(ms)} times (need exactly 1) - refusing to guess")
    return ms[0]


# The popover. Written against the bundle's module-scope names: `d` is React,
# `a` is the jsx runtime; the panel component's minified name is captured.
COMPONENT = (
    'function QCtxPop({sessionActions:e,sessionId:t}){'
    'const[n,s]=d.useState(!1),[r,i]=d.useState(null),o=d.useRef(null);'
    'd.useEffect(()=>{'
    'if(!document.getElementById("qwen-ctx-pop-style")){const c=document.createElement("style");c.id="qwen-ctx-pop-style";'
    'c.textContent=".qwen-ctx-pop{position:fixed;z-index:2147483000;width:400px;max-width:calc(100vw - 24px);'
    'max-height:min(72vh,680px);overflow:auto;padding:14px 16px;border:1px solid var(--border,#3a3f4a);'
    'border-radius:var(--radius,10px);background:var(--popover,var(--background,#1e2128));'
    'color:var(--popover-foreground,var(--foreground,#e6e8ec));box-shadow:0 14px 44px rgba(0,0,0,.45);'
    'font-size:13px;line-height:1.45}";document.head.appendChild(c)}'
    'const l=c=>{window.__qctxPtr={x:c.clientX,y:c.clientY}},'
    'u=()=>{if(Date.now()-(window.__qctxClosedAt||0)<350)return;i(window.__qctxPtr||null),s(h=>!h)},'
    'f=()=>s(!1);'
    'return document.addEventListener("pointerdown",l,!0),window.addEventListener("qwen-ctx-toggle",u),'
    'window.addEventListener("qwen-ctx-close",f),()=>{document.removeEventListener("pointerdown",l,!0),'
    'window.removeEventListener("qwen-ctx-toggle",u),window.removeEventListener("qwen-ctx-close",f)}},[]);'
    'd.useEffect(()=>{if(!n)return;'
    'const l=c=>{c.key==="Escape"&&(window.__qctxClosedAt=Date.now(),s(!1))},'
    'u=c=>{o.current&&!o.current.contains(c.target)&&(window.__qctxClosedAt=Date.now(),s(!1))};'
    'return document.addEventListener("keydown",l,!0),document.addEventListener("pointerdown",u,!0),'
    '()=>{document.removeEventListener("keydown",l,!0),document.removeEventListener("pointerdown",u,!0)}},[n]);'
    'if(!n)return null;'
    'const l=window.innerWidth,c=window.innerHeight,u=r&&typeof r.x=="number"?r:{x:24,y:c-56},'
    'b=Math.max(12,c-u.y+14),'
    'f={left:Math.max(12,Math.min(u.x-48,l-412)),bottom:b,maxHeight:Math.max(160,Math.min(680,Math.round(c*.72),c-b-12))};'
    'return a.jsx("div",{ref:o,className:"qwen-ctx-pop",style:f,role:"dialog","aria-label":"Context usage",'
    'children:a.jsx(PANEL,{sessionActions:e,sessionId:t})})}'
)


def patch(bundle, check):
    st = status(bundle)
    if st == "missing":
        raise SystemExit(f"not found: {bundle}")
    if st == "patched":
        print("already patched - nothing to do")
        return
    if st == "native":
        print("this version has its own context popover (hover the context chip; click opens")
        print("the Context Usage panel) - nothing to patch")
        return
    src = read(bundle)

    # 1. the app's own "Context Usage" panel component (used by the right panel)
    m_panel = one(r'function (\w+)\(\{sessionActions:e,sessionId:t\}\)\{const\{t:n\}=St\(\),\[s,r\]=d\.useState\(null\)'
                  r'.{0,400}?getContextUsage\(\{detail:!0,silent:!0\}\)', src, "context-usage panel component")
    panel = m_panel.group(1)

    # 2. the footer handler: NAME=d.useCallback(()=>TX("/context",!1),[TX])
    m_handler = one(r'(\w+)=d\.useCallback\(\(\)=>(\w+)\("/context",!1\),\[\2\]\)', src, "context handler")
    handler, tx = m_handler.group(1), m_handler.group(2)

    # 3. the session-actions object and session state, as used by that Tx
    m_actions = one(r'(\w+)\.getContextUsage\(\{detail:P\}\)\.then', src, "session actions")
    actions = m_actions.group(1)
    m_guard = one(r'(\w+)=d\.useCallback\(\(\)=>(\w+)\.current\.sessionId\?!0:\(\w+\("info",\w+\("localCommand\.noSession"\)\),!1\)',
                  src, "no-session guard")
    guard = m_guard.group(1)
    m_session = one(r'tokenCount:(\w+)\.tokenCount\?\?0,contextWindow:\1\.contextWindow\?\?0,'
                    r'contextUsageAlwaysVisible:\w+,onShowContextUsage:' + re.escape(handler) + ',',
                    src, "session state")
    session = m_session.group(1)

    # 4. the compact footer passes an inline lambda instead of the handler
    m_inline = one(re.escape(f'onShowContext:()=>{tx}("/context",!1)'), src, "compact footer inline handler")

    # 5. mount point: a stable sibling in the composer's children array
    m_mount = one(r',(\w+)&&a\.jsx\((\w+),\{floating:!(\w+),hidden:', src, "mount point")

    # 6. define the component right before the panel component (module scope;
    #    function declarations there are hoisted, so order does not matter)
    def_at = m_panel.start()

    new_handler = (f'{handler}=d.useCallback(()=>{{if(!{guard}())return;'
                   f'window.dispatchEvent(new CustomEvent("qwen-ctx-toggle"))}},[{guard}])')
    mount = f',a.jsx(QCtxPop,{{sessionActions:{actions},sessionId:{session}.sessionId}})'

    # apply from the highest offset down so earlier offsets stay valid
    edits = sorted([
        (m_handler.start(), m_handler.end(), new_handler),
        (m_inline.start(), m_inline.end(), f'onShowContext:{handler}'),
        (m_mount.start(), m_mount.start(), mount),
        (def_at, def_at, COMPONENT.replace("PANEL", panel)),
    ], reverse=True)
    out = src
    for s, e, rep in edits:
        out = out[:s] + rep + out[e:]

    if check:
        syntax_check(bundle, out)

    bak = backup_path(bundle)
    if not os.path.exists(bak):
        shutil.copy2(bundle, bak)
        print(f"backup -> {bak}")
    else:
        print(f"backup already exists -> {bak}")
    write(bundle, out)
    print(f"patched {bundle}  (+{len(out) - len(src)} bytes)")
    print(f"  panel={panel} handler={handler} tx={tx} actions={actions} guard={guard} session={session}")
    print("restart Qwen Code Desktop to load the patched bundle (assets are cached as immutable;")
    print("a restart picks a new daemon port, which is a new origin, so the cache is bypassed)")


def syntax_check(bundle, text):
    node = os.path.join(os.path.dirname(bundle), "..", "..", "..", "node", "node.exe")
    node = os.path.normpath(node)
    if not os.path.exists(node):
        node = shutil.which("node")
    if not node:
        print("(no node found - skipping syntax check)")
        return
    fd, tmp = tempfile.mkstemp(suffix=".mjs")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("patched bundle fails `node --check`:\n" + r.stderr[-2000:])
        print("syntax check: ok")
    finally:
        os.unlink(tmp)


def revert(bundle):
    bak = backup_path(bundle)
    if not os.path.exists(bak):
        raise SystemExit(f"no backup at {bak} - nothing to restore")
    shutil.copy2(bak, bundle)
    print(f"restored from {bak}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web-shell", default=DEFAULT_WEB_SHELL,
                    help="the app's lib/web-shell directory")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-check", action="store_true", help="skip the node --check pass")
    a = ap.parse_args()

    bundle = find_bundle(a.web_shell)
    if a.status:
        print(f"{status(bundle)}: {bundle}")
        return
    if a.revert:
        revert(bundle)
        return
    patch(bundle, check=not a.no_check)


if __name__ == "__main__":
    sys.exit(main())
