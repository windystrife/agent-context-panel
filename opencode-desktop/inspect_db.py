#!/usr/bin/env python3
"""Dev helper: dump the schema and a sample row of OpenCode's local database.

Copies the file first so a running OpenCode never sees a reader on its db.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

src = sys.argv[1] if len(sys.argv) > 1 else \
    "/mnt/c/Users/tungnt/.local/share/opencode/opencode.db"

tmp = tempfile.mktemp(suffix=".db")
shutil.copy(src, tmp)
try:
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("tables:", tables)
    for t in tables:
        try:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error as e:
            print(f"\n== {t}: count failed: {e}")
            continue
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
        print(f"\n== {t}  rows={n}\n   cols: {cols}")
        if n:
            row = con.execute(f'SELECT * FROM "{t}" LIMIT 1').fetchone()
            for k in row.keys():
                v = row[k]
                s = str(v)
                print(f"   {k} = {s[:220]}{'...' if len(s) > 220 else ''}")
finally:
    os.remove(tmp)
