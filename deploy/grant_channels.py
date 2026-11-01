#!/usr/bin/env python3
"""Add channels to a viewer's allowed list by channel category (description).

Dry run (read-only, any user that can read the DB):
    python3 grant_channels.py --viewer twan --category 'Netherland - %'
Apply (backs up the DB first):
    sudo -u tvproxy python3 grant_channels.py --viewer twan --category 'Netherland - %' --apply

Existing allowed channels are kept; only missing ones are added. The stored
format matches app.stores._dump_allowed_channels (sorted JSON list of ids).
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter

DB = "/var/lib/tv-proxy/mappings.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--viewer", required=True, help="viewer name (exact) or numeric id")
    ap.add_argument("--category", action="append", required=True,
                    help="SQL LIKE pattern on channels.description; repeatable")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    uri = f"file:{args.db}" + ("" if args.apply else "?immutable=1")
    conn = sqlite3.connect(uri, uri=True, timeout=10.0, isolation_level=None)

    key = "id" if args.viewer.isdigit() else "name"
    viewers = conn.execute(f"SELECT id, name, allowed_channels FROM viewers WHERE {key}=?",
                           (args.viewer,)).fetchall()
    if len(viewers) != 1:
        print(f"expected exactly one viewer {args.viewer!r}, found {len(viewers)}", file=sys.stderr)
        return 1
    vid, vname, allowed_json = viewers[0]
    current = set(json.loads(allowed_json)) if allowed_json else set()
    if not current:
        print(f"viewer {vname!r} has an empty list, which means ALL channels; nothing to add")
        return 0

    where = " OR ".join("description LIKE ?" for _ in args.category)
    rows = conn.execute(f"SELECT id, description FROM channels WHERE enabled=1 AND ({where})",
                        args.category).fetchall()
    missing = [(cid, desc) for cid, desc in rows if cid not in current]

    print(f"viewer {vname!r} (id {vid}): {len(current)} channels now, "
          f"{len(rows)} match, {len(missing)} to add")
    for desc, n in sorted(Counter(d for _, d in missing).items()):
        print(f"  + {n:4}  {desc}")
    if not args.apply:
        print("dry run; add --apply to save")
        return 0
    if not missing:
        return 0

    backup = f"{args.db}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    dst = sqlite3.connect(backup)
    conn.backup(dst)
    dst.close()
    print(f"backup: {backup}")

    new = sorted(current | {cid for cid, _ in missing})
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE viewers SET allowed_channels=?, updated_at=? WHERE id=?",
                 (json.dumps(new), time.time(), vid))
    conn.execute("COMMIT")
    print(f"saved: viewer {vname!r} now has {len(new)} channels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
