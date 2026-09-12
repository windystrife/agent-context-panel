#!/usr/bin/env python3
"""
Export a Claude Code session into Qwen Code Desktop.

Both tools keep transcripts as JSONL under a per-working-directory folder, but
the records are different families: Claude carries Anthropic-shaped messages
(`content[]` with text / thinking / tool_use), Qwen carries Gemini-shaped ones
(`parts[]`, `role: "model"`). This converts one to the other and registers the
result with the Craft desktop shell so the session shows up in the app.

Writes exactly two places (both new, nothing is overwritten):

  ~/.qwen/projects/<encoded-cwd>/chats/<session>.jsonl        the transcript
  ~/.craft-agent/workspaces/<ws>/sessions/<session>/session.jsonl   app entry

What does NOT survive, because Qwen has no record for it: Claude's tool traffic.
Measured on a real 46.9 MB session, tool traffic is 93.9% of the bytes and the
conversation is 6.1%. Tool calls are not dropped silently - each becomes a short
text line - but their arguments and results are not preserved verbatim.

  python3 claude_to_qwen.py <claude-session.jsonl>            # dry run
  python3 claude_to_qwen.py <claude-session.jsonl> --apply
"""

import argparse
import collections
import io
import json
import os
import re
import sys
import uuid

REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
CMDBLOCK = re.compile(r"<(command-[a-z-]+|local-command-[a-z-]+)>.*?</\1>", re.S)
QWEN_VERSION = "0.22.0"


def home_candidates(rel):
    out = [os.path.join(os.path.expanduser("~"), rel)]
    for root in ("/mnt/c/Users", "C:/Users"):
        try:
            for n in sorted(os.listdir(root)):
                out.append(os.path.join(root, n, rel))
        except OSError:
            pass
    return out


def first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def encode_cwd(cwd):
    """Match Qwen's own folder naming, which does NOT collapse separators:

        C:\\Users\\tungnt            -> c--users-tungnt
        H:\\Claude                   -> h--claude

    The drive colon and the following backslash each become a dash, which is
    where the doubled dash comes from. Collapsing runs would produce
    'h-claude' and the session would land in a folder the app never reads.
    """
    s = cwd.lower()
    for ch in (":", "\\", "/"):
        s = s.replace(ch, "-")
    return s.strip("-")


def clean_user_text(content):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    text = REMINDER.sub("", text)
    text = CMDBLOCK.sub("", text)
    return text.strip()


def convert(src, session_id, cwd_override=None):
    """Claude transcript -> (list of Qwen records, stats)."""
    out = []
    stats = collections.Counter()
    parent = None
    title = None
    cwd = cwd_override

    def rec(kind, **kw):
        nonlocal parent
        u = str(uuid.uuid4())
        base = {
            "uuid": u,
            "parentUuid": parent,
            "sessionId": session_id,
            "timestamp": kw.pop("timestamp", None),
            "type": kind,
            "cwd": cwd or "",
            "version": QWEN_VERSION,
        }
        base.update(kw)
        parent = u
        out.append(base)
        return base

    with io.open(src, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                stats["unparseable"] += 1
                continue
            t = d.get("type")
            title = d.get("customTitle") or d.get("aiTitle") or title
            if cwd is None and d.get("cwd"):
                cwd = d["cwd"]

            if t == "user" and not (d.get("isMeta") or d.get("isSidechain")):
                text = clean_user_text((d.get("message") or {}).get("content"))
                if not text or "tool_use_id" in text[:200]:
                    stats["user skipped (tool result / empty)"] += 1
                    continue
                rec("user", provenance="real_user",
                    timestamp=d.get("timestamp"),
                    message={"role": "user", "parts": [{"text": text}]})
                stats["user"] += 1

            elif t == "assistant" and not d.get("isSidechain"):
                msg = d.get("message") or {}
                parts, tools = [], 0
                for b in msg.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "thinking":
                        th = b.get("thinking") or b.get("text") or ""
                        if th.strip():
                            parts.append({"text": th, "thought": True})
                    elif b.get("type") == "text":
                        if (b.get("text") or "").strip():
                            parts.append({"text": b["text"]})
                    elif b.get("type") == "tool_use":
                        tools += 1
                        args = json.dumps(b.get("input") or {}, ensure_ascii=False)
                        if len(args) > 300:
                            args = args[:300] + "…"
                        parts.append({"text": "[tool: %s] %s" % (b.get("name"), args)})
                if not parts:
                    stats["assistant skipped (no renderable content)"] += 1
                    continue
                usage = msg.get("usage") or {}
                cached = usage.get("cache_read_input_tokens", 0) or 0
                model = msg.get("model") or "claude"
                rec("assistant", provenance="assistant_output",
                    timestamp=d.get("timestamp"),
                    model=model,
                    # Qwen writes this on every assistant turn; the context meter
                    # reads it. Claude models are 200k unless stated otherwise.
                    contextWindowSize=200000 if model.startswith("claude") else 1048576,
                    message={"role": "model", "parts": parts},
                    usageMetadata={
                        "promptTokenCount": (usage.get("input_tokens", 0) or 0) + cached,
                        "candidatesTokenCount": usage.get("output_tokens", 0) or 0,
                        "thoughtsTokenCount": 0,
                        "totalTokenCount": (usage.get("input_tokens", 0) or 0)
                                           + cached + (usage.get("output_tokens", 0) or 0),
                        "cachedContentTokenCount": cached,
                    })
                stats["assistant"] += 1
                stats["tool calls flattened to text"] += tools
            else:
                stats["dropped: %s" % t] += 1

    if title:
        # a title record, inserted first so the app names the session
        head = {
            "uuid": str(uuid.uuid4()), "parentUuid": None,
            "sessionId": session_id, "timestamp": out[0]["timestamp"] if out else None,
            "type": "system", "provenance": "system", "cwd": cwd or "",
            "version": QWEN_VERSION, "subtype": "custom_title",
            "systemPayload": {"customTitle": title, "titleSource": "manual"},
        }
        if out:
            out[0]["parentUuid"] = head["uuid"]
        out.insert(0, head)
    return out, stats, title, cwd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="Claude Code transcript .jsonl")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workspace", default="Qwen")
    ap.add_argument("--session-id", default=None)
    ap.add_argument("--cwd", default=None,
                    help="working directory to file the session under. Defaults to "
                         "the Craft workspace's own workingDirectory, NOT the source "
                         "session's cwd: the app resolves transcripts relative to the "
                         "workspace, so a session filed under the original cwd is "
                         "never found.")
    a = ap.parse_args()

    src = a.source
    if not os.path.exists(src):
        raise SystemExit("not found: " + src)

    qwen_root = first_existing(home_candidates(".qwen/projects"))
    craft_root = first_existing(home_candidates(".craft-agent/workspaces"))
    if not qwen_root or not craft_root:
        raise SystemExit("could not locate ~/.qwen/projects or ~/.craft-agent/workspaces")

    # The workspace is bound to a working directory and resolves transcripts
    # relative to it. Filing the import under the SOURCE session's cwd puts the
    # transcript in a folder the app never looks at, and the session silently
    # does not appear.
    target_cwd = a.cwd
    if not target_cwd:
        wcfg = os.path.join(craft_root, a.workspace, "config.json")
        try:
            with io.open(wcfg, encoding="utf-8") as f:
                wd = (json.load(f).get("defaults") or {}).get("workingDirectory")
        except (OSError, ValueError):
            wd = None
        if wd:
            wd = wd.replace("/", "\\")
            if wd.startswith("~\\"):
                user = os.path.basename(os.path.expanduser("~"))
                wd = "C:\\Users\\%s\\%s" % (user, wd[2:])
            target_cwd = wd
            print("workspace working directory: %s" % target_cwd)

    sid = a.session_id or str(uuid.uuid4())
    records, stats, title, cwd = convert(src, sid, cwd_override=target_cwd)
    if not records:
        raise SystemExit("nothing convertible in " + src)

    folder = encode_cwd(cwd or "unknown")
    chat_dir = os.path.join(qwen_root, folder, "chats")
    chat_file = os.path.join(chat_dir, sid + ".jsonl")
    sess_dir = os.path.join(craft_root, a.workspace, "sessions", sid)
    sess_file = os.path.join(sess_dir, "session.jsonl")

    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    meta = json.dumps({
        "id": sid,
        "workspaceRootPath": "~\\.craft-agent\\workspaces\\" + a.workspace,
        "sdkSessionId": sid,
        "isFlagged": False,
        "sessionStatus": "todo",
        "thinkingLevel": "medium",
    }, ensure_ascii=False) + "\n"

    print("source    : %s (%.1f MB)" % (os.path.basename(src), os.path.getsize(src) / 1e6))
    print("title     : %s" % title)
    print("cwd       : %s   -> folder %s" % (cwd, folder))
    print("session   : %s" % sid)
    print("output    : %.2f MB in %d records" % (len(payload.encode()) / 1e6, len(records)))
    print("\nconversion:")
    for k, v in stats.most_common():
        print("  %-42s %d" % (k, v))
    print("\nwould write:\n  %s\n  %s" % (chat_file, sess_file))

    if not a.apply:
        print("\nDRY RUN - pass --apply to write")
        return
    for p in (chat_file, sess_file):
        if os.path.exists(p):
            raise SystemExit("refusing to overwrite existing " + p)
    os.makedirs(chat_dir, exist_ok=True)
    os.makedirs(sess_dir, exist_ok=True)
    with io.open(chat_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(payload)
    with io.open(sess_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(meta)
    back = io.open(chat_file, encoding="utf-8").read()
    assert back == payload, "readback mismatch"
    print("\nWRITTEN. To undo:\n  rm %s\n  rm -r %s" % (chat_file, sess_dir))


if __name__ == "__main__":
    sys.exit(main())
