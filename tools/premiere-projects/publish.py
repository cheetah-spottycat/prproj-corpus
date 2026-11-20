#!/usr/bin/env python3
"""Copy a checked local run into corpus/, the part of the repository others use.

    out/premiere/<major>/   ->  corpus/premiere/<major>/    generated projects + truth JSON
    out/perturbed/<major>/  ->  corpus/perturbed/<major>/   perturbed copies + request + readback

Files are copied byte for byte (never edited). Diagnostic cases (probe/) are left out. Each
target folder is made to mirror its source: project and JSON files no longer in the run are
removed. The copy is refused unless check.py passes on the source folders first, and
tools/format/claims.py should pass on the result.

corpus/premiere/<major>/ sits at the same depth as out/premiere/<major>/, so the projects'
RelativePath (..\\..\\..\\media\\…) still leads to media/ in this repository.

Usage:
    python publish.py [--major 27]

MIT OR Apache-2.0.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK = Path(__file__).resolve().parent / "check.py"


def mirror(source: Path, target: Path, suffixes: tuple[str, ...]) -> int:
    target.mkdir(parents=True, exist_ok=True)
    wanted = {f.name for f in source.iterdir() if f.name.endswith(suffixes) and not f.name.startswith("probe__")}
    for old in target.iterdir():
        if old.name.endswith(suffixes) and old.name not in wanted:
            old.unlink()
    for name in sorted(wanted):
        shutil.copyfile(source / name, target / name)
    return len(wanted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--major", default="27")
    args = parser.parse_args()
    pairs = [
        (ROOT / "out" / "premiere" / args.major, ROOT / "corpus" / "premiere" / args.major),
        (ROOT / "out" / "perturbed" / args.major, ROOT / "corpus" / "perturbed" / args.major),
    ]
    for source, _ in pairs:
        if not source.is_dir():
            print(f"{source} missing: run the generator (and perturb.py, Open perturbed) first", file=sys.stderr)
            return 1
        result = subprocess.run([sys.executable, str(CHECK), str(source)], capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout + result.stderr, file=sys.stderr)
            print(f"check.py failed on {source}; nothing copied", file=sys.stderr)
            return 1
    for source, target in pairs:
        n = mirror(source, target, (".prproj", ".json"))
        print(f"{n:>3} files  {source.relative_to(ROOT)} -> {target.relative_to(ROOT)}")
    print("now run: python tools/format/claims.py corpus/premiere/%s --doc docs/format/prproj.md" % args.major)
    return 0


if __name__ == "__main__":
    sys.exit(main())
