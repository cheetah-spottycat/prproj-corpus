#!/usr/bin/env python3
"""Check generated Premiere projects before anything is published.

For each <stem>.prproj in the folder:
  - it must be gzip-compressed XML (or plain XML) that parses;
  - it must not contain the current user name, host name, home folder, or an e-mail address;
  - a sibling <stem>.json must exist, parse, and hold at least one sequence.

Prints one line per project and a summary; exits non-zero on any problem. It never edits a
project: if something leaks, change the case or the run setup and regenerate.

Usage:
    python check.py [FOLDER]        (default: out/premiere/*)

MIT OR Apache-2.0.
"""

from __future__ import annotations

import getpass
import gzip
import json
import os
import re
import socket
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def needles() -> list[tuple[str, bytes]]:
    """Personal strings to look for, each in UTF-8 and UTF-16LE (Premiere may use either)."""
    words: set[str] = set()
    for value in (getpass.getuser(), socket.gethostname(), os.environ.get("USERNAME"), os.environ.get("USER")):
        if value and len(value) >= 3:
            words.add(value)
    home = str(Path.home())
    words.add(home)
    words.add(home.replace("\\", "/"))
    out: list[tuple[str, bytes]] = []
    for word in sorted(words):
        out.append((word, word.lower().encode("utf-8")))
        out.append((word + " (utf-16)", word.lower().encode("utf-16-le")))
    return out


def load_xml(path: Path) -> bytes:
    raw = path.read_bytes()
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def check_one(path: Path, personal: list[tuple[str, bytes]]) -> list[str]:
    problems: list[str] = []
    try:
        xml = load_xml(path)
    except (OSError, EOFError) as e:
        return [f"not gzip/XML: {e}"]
    try:
        ET.fromstring(xml)
    except ET.ParseError as e:
        problems.append(f"XML does not parse: {e}")
    lower = xml.lower()
    for label, needle in personal:
        if needle in lower:
            problems.append(f"contains {label!r}")
    for match in sorted(set(EMAIL.findall(xml)))[:5]:
        problems.append(f"contains an e-mail-like string {match.decode('utf-8', 'replace')!r}")

    truth = path.with_suffix(".json")
    if not truth.exists():
        problems.append("no truth .json")
    else:
        try:
            data = json.loads(truth.read_text(encoding="utf-8-sig"))
            # A case may ask for no sequence (labels/index); then the readback has none either.
            # A perturbed copy inherits its base case's sequences, so it isn't counted here.
            case = data.get("case", {})
            wanted = len(case.get("sequences", []))
            if "perturbedFrom" not in case and len(data.get("sequences", [])) != wanted:
                problems.append(f"truth has {len(data.get('sequences', []))} sequences, the case asked for {wanted}")
        except json.JSONDecodeError as e:
            problems.append(f"truth does not parse: {e}")
        raw_truth = truth.read_bytes().lower()
        for label, needle in personal:
            if needle in raw_truth:
                problems.append(f"truth contains {label!r}")
    return problems


def main() -> int:
    folders = [Path(sys.argv[1])] if len(sys.argv) > 1 else sorted((ROOT / "out" / "premiere").glob("*"))
    projects = [p for folder in folders for p in sorted(folder.glob("*.prproj"))]
    if not projects:
        print("no .prproj files found", file=sys.stderr)
        return 1
    personal = needles()
    bad = 0
    for path in projects:
        problems = check_one(path, personal)
        if problems:
            bad += 1
            print(f"FAIL {path.name}")
            for problem in problems:
                print(f"     {problem}")
        else:
            print(f"ok   {path.name}")
    print(f"{len(projects) - bad}/{len(projects)} clean")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
