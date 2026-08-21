#!/usr/bin/env python3
"""Strip carriage returns out of recorded sessions.

Every text field the page posts arrives through a multipart form, and multipart
encoding normalises newlines to CRLF on the wire. Until `lf()` was added at the
ingress in app/main.py, those carriage returns were written straight into
turns.jsonl — which is the fine-tuning corpus. Nothing reads them deliberately
and a model trained on them would learn to emit them.

Dry run by default; pass --apply to rewrite. Rewrites are atomic (write a
temporary file beside the original, then replace) so an interrupted run cannot
truncate a transcript.

    scripts/normalise-newlines.py            # what would change
    scripts/normalise-newlines.py --apply    # change it
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def clean(value):
    """Carriage returns out of every string, however deeply nested."""
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


def sweep(path: Path, apply: bool) -> int:
    """Rewrite one transcript. Returns the number of records that changed."""
    records, changed = [], 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        tidied = clean(record)
        if tidied != record:
            changed += 1
        records.append(tidied)

    if changed and apply:
        # Write beside the original so the replace is on the same filesystem.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        os.replace(tmp, path)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="rewrite the files")
    ap.add_argument("--root", default="sessions", type=Path)
    args = ap.parse_args()

    files = sorted(args.root.glob("*/turns.jsonl"))
    if not files:
        print(f"no transcripts under {args.root}", file=sys.stderr)
        return 1

    total = 0
    for path in files:
        changed = sweep(path, args.apply)
        total += changed
        if changed:
            print(f"{'rewrote' if args.apply else 'would rewrite'} "
                  f"{changed:4d} records  {path}")

    verb = "rewrote" if args.apply else "would rewrite"
    print(f"\n{verb} {total} records across {len(files)} transcripts")
    if total and not args.apply:
        print("re-run with --apply to make the change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
