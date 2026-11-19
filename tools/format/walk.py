#!/usr/bin/env python3
"""Print one object of a generated .prproj, with references shown as `-> Class#id`.

A .prproj is gzip-compressed XML: a flat list of top-level objects that point to each other
(see docs/format/prproj.md). This prints an object by id or by tag, to a given element depth,
following references up to a given number of hops. Only for projects this corpus generated.

Usage:
    python walk.py PROJECT.prproj (OBJECT-ID | TAG) [DEPTH] [FOLLOW]

    python walk.py out/premiere/27/trim__in-out.prproj VideoClipTrackItem 8 3

MIT OR Apache-2.0.
"""

from __future__ import annotations

import gzip
import sys
import xml.etree.ElementTree as ET


def load(path: str) -> tuple[ET.Element, dict[str, ET.Element]]:
    with gzip.open(path) as f:
        root = ET.fromstring(f.read())
    # Only top-level objects are addressable (nested ObjectIDs form their own scope).
    objects = {}
    for child in root:
        key = child.get("ObjectID") or child.get("ObjectUID")
        if key:
            objects[key] = child
    return root, objects


def reference(e: ET.Element) -> str | None:
    return e.get("ObjectRef") or e.get("ObjectURef")


def show(e: ET.Element, objects: dict, depth: int, follow: int, indent: str, seen: set) -> None:
    ref = reference(e)
    if ref is not None:
        target = objects.get(ref)
        print(f"{indent}{e.tag} -> {target.tag if target is not None else '?'}#{ref[:8]}")
        if target is not None and follow > 0 and ref not in seen:
            seen.add(ref)
            show(target, objects, depth, follow - 1, indent + "    ", seen)
        return
    attrs = {k: v for k, v in e.attrib.items() if k not in ("ClassID", "Version")}
    text = (e.text or "").strip()
    kids = list(e)
    line = indent + e.tag
    if attrs:
        line += f" {attrs}"
    if text and not kids:
        line += f" = {text[:100]}"
    print(line)
    if depth <= 0:
        if kids:
            print(f"{indent}  ... {len(kids)} children")
        return
    for kid in kids:
        show(kid, objects, depth - 1, follow, indent + "  ", seen)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: walk.py PROJECT.prproj (OBJECT-ID | TAG) [DEPTH] [FOLLOW]", file=sys.stderr)
        return 2
    root, objects = load(sys.argv[1])
    selector = sys.argv[2]
    depth = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    follow = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    targets = [objects[selector]] if selector in objects else [c for c in root if c.tag == selector]
    if not targets:
        print(f"no object {selector!r}", file=sys.stderr)
        return 1
    for target in targets:
        show(target, objects, depth, follow, "", set())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
