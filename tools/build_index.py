#!/usr/bin/env python3
"""
build_index.py — walk sources/ and (re)generate index.json.

The device fetches index.json first to populate its fixture-library list.
Keep entries small (name + path only) — device pulls full profile JSON on
demand. Run after any edit/add/remove under sources/.
"""

import datetime
import glob
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_DIR = os.path.join(REPO_ROOT, "sources")
INDEX_PATH = os.path.join(REPO_ROOT, "index.json")
SCHEMA_VERSION = "rehmlights-index/1"


def main():
    if not os.path.isdir(SOURCES_DIR):
        print(f"No sources/ directory at {SOURCES_DIR}")
        sys.exit(1)

    entries = []
    paths = sorted(glob.glob(os.path.join(SOURCES_DIR, "**", "*.json"), recursive=True))
    for p in paths:
        try:
            with open(p) as f:
                obj = json.load(f)
        except json.JSONDecodeError as e:
            print(f"  SKIP {p} — invalid JSON: {e}")
            continue

        # Profile carries footprint at top level (cooked schema). Fall back
        # to scanning channels[] for the max offset on older sources.
        footprint = obj.get("footprint") or _footprint(obj)

        rel = os.path.relpath(p, REPO_ROOT).replace(os.sep, "/")
        entries.append({
            "id": obj.get("id") or rel,
            "name": obj.get("name", ""),
            "manufacturer": obj.get("manufacturer", ""),
            "mode": obj.get("mode", ""),
            "fixture_type": obj.get("fixture_type", ""),
            "footprint": footprint,
            "url": rel,                              # repo-relative path
        })

    # Sort: manufacturer, then name, then mode — gives the device UI a stable order.
    entries.sort(key=lambda e: (e["manufacturer"].lower(),
                                e["name"].lower(),
                                e["mode"].lower()))

    index = {
        "schema": SCHEMA_VERSION,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "count": len(entries),
        "fixtures": entries,
    }

    with open(INDEX_PATH, "w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {INDEX_PATH}  ({len(entries)} fixtures)")


def _footprint(profile):
    """Fallback: largest 'offset' in the channels[] array. Returns 0 if none."""
    largest = 0
    for entry in profile.get("channels", []) or []:
        if isinstance(entry, dict):
            off = entry.get("offset", 0)
            if isinstance(off, int) and off > largest:
                largest = off
    return largest


if __name__ == "__main__":
    main()
