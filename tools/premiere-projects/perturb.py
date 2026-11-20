#!/usr/bin/env python3
"""Make perturbed copies of generated projects, to watch what Premiere does with what it reads.

Generated projects show what Premiere *writes*. Some questions are about what it *reads*: which
of several disagreeing fields wins. For those, this script copies a generated project and changes
named fields in the copy. The plugin's **Open perturbed** button then opens each copy (never
saving it) and records Premiere's readback. The base projects in out/premiere/ are never changed.

Each definition in tools/premiere-projects/perturbations/ names a base case, the fields to vary
and their values. Output, all under out/ (gitignored):

    out/perturbed/<major>/<area>__<name>--<variant>.prproj    the edited copy
    out/perturbed/<major>/<area>__<name>--<variant>.request.json   what was changed, and how
    out/perturbed/media/<field>/<file>                         identical media copies

Path values: `here` points the field at its own identical copy of the media (copied with its
modification time), `missing` at a file that doesn't exist, `absent` removes the element.
A definition's optional `layout` moves the copies (`media`: a subfolder of out/perturbed/media,
`copies`: a folder name per field) and the missing targets (`missing`: a folder under
out/perturbed/ that must not exist), to tell a field Premiere follows from a file it finds by
searching. `freshFileKey: true` gives the Media a FileKey Premiere hasn't seen, because Premiere
remembers where it last found a media file outside the project. `project`: `in-place` records the copy's own location in the project settings; `moved` keeps
the base project's location, as if the project file had been copied elsewhere.

Windows paths only for now (the corpus is generated on Windows).

Usage:
    python perturb.py [--major 27]

MIT OR Apache-2.0.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import itertools
import json
import ntpath
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = Path(__file__).resolve().parent / "perturbations"
PATH_FIELDS = ("FilePath", "ActualMediaFilePath", "RelativePath")
SHORT = {"FilePath": "F", "ActualMediaFilePath": "A", "RelativePath": "R"}
LAST_GOOD = "project.settings.lastknowngoodprojectpath"
LAST_PARENT = "project.settings.lastknownparentdirectorypathaboveprojectpath"


def win(path: Path) -> str:
    return str(path.resolve(strict=False))


def media_block(xml: str, title: str) -> tuple[int, int]:
    """Span of the one top-level Media object whose Title is `title`."""
    spans = [m.span() for m in re.finditer(r"<Media ObjectUID=\"[^\"]+\"[^>]*>.*?</Media>", xml, re.S)
             if f"<Title>{title}</Title>" in m.group(0)]
    if len(spans) != 1:
        raise SystemExit(f"expected one Media titled {title}, found {len(spans)}")
    return spans[0]


def set_element(block: str, tag: str, value: str | None) -> str:
    pattern = re.compile(rf"(\n[ \t]*)<{tag}>[^<]*</{tag}>")
    if len(pattern.findall(block)) != 1:
        raise SystemExit(f"expected one <{tag}> in the Media object")
    if value is None:
        return pattern.sub("", block)
    return pattern.sub(lambda m: f"{m.group(1)}<{tag}>{value}</{tag}>", block)


def set_property(xml: str, name: str, value: str) -> str:
    pattern = re.compile(rf"<{re.escape(name)}>[^<]*</{re.escape(name)}>")
    if len(pattern.findall(xml)) != 1:
        raise SystemExit(f"expected one <{name}>")
    return pattern.sub(lambda m: f"<{name}>{value}</{name}>", xml)


def variants(definition: dict) -> list[dict]:
    grid = definition.get("grid", {})
    keys = list(grid)
    out = [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]
    out += definition.get("extra", [])
    # Variants with no reachable copy last: Premiere may stop and ask where the media is.
    return sorted(out, key=lambda v: not any(v.get(f) == "here" for f in PATH_FIELDS))


def variant_name(v: dict) -> str:
    return "-".join(f"{SHORT[f]}{v[f]}" for f in PATH_FIELDS) + f"-{v['project']}"


def build(definition: dict, major: str) -> int:
    base_stem = definition["base"].replace("/", "__")
    base = ROOT / "out" / "premiere" / major / f"{base_stem}.prproj"
    if not base.exists():
        raise SystemExit(f"{definition['id']}: base {base} not generated yet (run the plugin first)")
    base_truth = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
    title = definition["media"]
    source = ROOT / "media" / title
    out_dir = ROOT / "out" / "perturbed" / major
    # Layout: where each field's copy lives and where "missing" paths point. By default the copies
    # are siblings (out/perturbed/media/F, A, R) and missing paths sit next to them.
    layout = definition.get("layout", {})
    media_dir = ROOT / "out" / "perturbed" / "media" / layout.get("media", "")
    folders = {field: layout.get("copies", {}).get(field, SHORT[field]) for field in PATH_FIELDS}
    missing_dir = ROOT / "out" / "perturbed" / layout["missing"] if "missing" in layout else media_dir
    if "missing" in layout and missing_dir.exists():
        raise SystemExit(f"{missing_dir} must not exist: missing paths point into it")
    out_dir.mkdir(parents=True, exist_ok=True)
    for field in PATH_FIELDS:
        (media_dir / folders[field]).mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, media_dir / folders[field] / title)  # keeps the modification time
    original = gzip.decompress(base.read_bytes()).decode("utf-8")

    count = 0
    for v in variants(definition):
        stem = f"{definition['id'].replace('/', '__')}--{variant_name(v)}"
        project = out_dir / f"{stem}.prproj"
        start, end = media_block(original, title)
        block = original[start:end]
        changes = {}
        for field in PATH_FIELDS:
            mode = v[field]
            if mode == "here":
                target = media_dir / folders[field] / title
            else:
                target = missing_dir / f"missing-{SHORT[field]}" / title
            if mode == "absent":
                value = None
            elif field == "RelativePath":
                value = ntpath.relpath(win(target), win(out_dir))
            else:
                value = win(target)
            block = set_element(block, field, value)
            rel = target.relative_to(ROOT).as_posix()
            changes[field] = {"mode": mode, "points to": None if value is None else rel, "exists": target.exists()}
        if definition.get("freshFileKey"):
            # A FileKey no Premiere has seen, so nothing it remembers about the media applies.
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"filmcraft-corpus/perturb/{stem}"))
            block = set_element(block, "FileKey", key)
            changes["FileKey"] = f"replaced with {key} (uuid5 of the variant name, new to Premiere)"
        xml = original[:start] + block + original[end:]
        if v["project"] == "in-place":
            xml = set_property(xml, LAST_GOOD, "\\\\?\\" + win(project))
            xml = set_property(xml, LAST_PARENT, win(out_dir.parent))
            changes["project location"] = f"out/perturbed/{major}/{project.name} (its own)"
        else:
            changes["project location"] = f"out/premiere/{major}/{base.name} (the base's, unchanged)"
        data = gzip.compress(xml.encode("utf-8"), mtime=0)
        project.write_bytes(data)
        request = {
            "generator": {"name": "filmcraft-corpus/perturb.py"},
            "case": {
                "id": f"{definition['id']}--{variant_name(v)}",
                "description": definition["description"],
                "perturbedFrom": {"id": definition["base"], "premiere": base_truth.get("premiere")},
                "variant": v,
                "changes": changes,
                # Lets a check confirm Premiere never saved over the copy it opened.
                "projectSha256": hashlib.sha256(data).hexdigest(),
            },
        }
        (out_dir / f"{stem}.request.json").write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        print(f"ok   {stem}")
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--major", default="27", help="Premiere major version folder in out/premiere/")
    args = parser.parse_args()
    if os.name != "nt":
        print("perturb.py writes Windows paths; run it on the machine that generated the corpus", file=sys.stderr)
        return 2
    total = 0
    for path in sorted(DEFINITIONS.glob("*.json")):
        total += build(json.loads(path.read_text(encoding="utf-8")), args.major)
    print(f"{total} perturbed project(s) in out/perturbed/{args.major}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
