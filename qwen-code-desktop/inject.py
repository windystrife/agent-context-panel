#!/usr/bin/env python3
"""
Inject the context panel into an Electron app's renderer HTML.

  python3 inject.py                 # patch Qwen Code Desktop
  python3 inject.py --status        # is it patched?
  python3 inject.py --revert        # restore the backup

The panel is inlined rather than loaded as a file: the app's CSP allows
'unsafe-inline' for scripts, while a separate file under a file:/app: origin
is not reliably 'self'.

Safe by construction: takes a one-time pristine backup, refuses to patch twice,
refuses if the anchor is missing, and verifies the file reads back byte-identical.
"""

import argparse
import os
import shutil
import sys

QWEN_HTML = ("C:/Users/tungnt/AppData/Local/Programs/qwen-code-desktop"
             "/resources/app/dist/renderer/index.html")
BEGIN = "<!-- BEGIN qwen-ctx-panel (injected; remove with inject.py --revert) -->"
END = "<!-- END qwen-ctx-panel -->"
ANCHOR = "</body>"


def backup_path(html):
    return html + ".bak-before-ctxpanel"


def read(p):
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    if read(p) != s:
        raise SystemExit("READBACK MISMATCH - file not written correctly")


def status(html):
    if not os.path.exists(html):
        return "missing"
    return "patched" if BEGIN in read(html) else "clean"


def patch(html, panel):
    st = status(html)
    if st == "missing":
        raise SystemExit(f"not found: {html}")
    if st == "patched":
        print("already patched - nothing to do")
        return
    doc = read(html)
    if ANCHOR not in doc:
        raise SystemExit(f"anchor {ANCHOR!r} not found - refusing to guess")
    if doc.count(ANCHOR) != 1:
        raise SystemExit(f"{doc.count(ANCHOR)} copies of {ANCHOR!r} - refusing")

    bak = backup_path(html)
    if not os.path.exists(bak):          # keep the pristine original, only once
        shutil.copy2(html, bak)
        print(f"backup -> {bak}")
    else:
        print(f"backup already exists -> {bak}")

    js = read(panel)
    block = f"{BEGIN}\n<script>\n{js}\n</script>\n{END}\n"
    out = doc.replace(ANCHOR, block + ANCHOR)
    write(html, out)
    print(f"patched {html}  (+{len(out) - len(doc)} bytes)")
    print("restart the app (or Ctrl+R) to see the panel")


def revert(html):
    bak = backup_path(html)
    if os.path.exists(bak):
        shutil.copy2(bak, html)
        print(f"restored from {bak}")
        return
    doc = read(html)
    if BEGIN not in doc:
        print("not patched - nothing to do")
        return
    i, j = doc.index(BEGIN), doc.index(END) + len(END)
    write(html, doc[:i] + doc[j:].lstrip("\n"))
    print("removed injected block (no backup was present)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=QWEN_HTML)
    ap.add_argument("--panel", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.js"))
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.status:
        print(f"{status(a.html)}: {a.html}")
        return
    if a.revert:
        revert(a.html)
        return
    patch(a.html, a.panel)


if __name__ == "__main__":
    sys.exit(main())
