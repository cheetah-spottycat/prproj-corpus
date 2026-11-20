#!/usr/bin/env python3
"""Check the claims in docs/format/prproj.md against the generated corpus.

Each claim below is a small function with an id. It reads generated projects (and, where it
can, Premiere's own readback in the truth JSON next to each project) and returns what it found
wrong. A claim passes when every project it applies to agrees, and at least one does.

The reference cites claims as `[claim: id]` in its Evidence column. `--doc` also checks that
link: every confirmed or settled row cites a claim, every cited claim exists, and every claim
here is cited. So the reference can't say *confirmed* about something no check backs.

Usage:
    python claims.py [OUT_FOLDER] [--doc docs/format/prproj.md] [-v]
    python claims.py [OUT_FOLDER] --self-test

--self-test changes one fact at a time in copies of corpus projects (in memory) and checks that
the claim about that fact then fails.

    python tools/format/claims.py out/premiere/27 --doc docs/format/prproj.md

OUT_FOLDER defaults to out/premiere/27. Exit status 1 if a claim or the doc check fails.
Only for projects this corpus generated. MIT OR Apache-2.0.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

TICKS_PER_SECOND = 254_016_000_000
TICKS_PER_SAMPLE_48K = TICKS_PER_SECOND // 48_000
STILL_DURATION = 10_973_491_200_000_000
CROSS_DISSOLVE = "AE.ADBE Cross Dissolve New"
# Marker colour by index (cases markers/colour--0 ... --7), as 0xAABBGGRR.
MARKER_COLOURS = [0xFF378671, 0xFF362CD2, 0xFFB18BAF, 0xFF246FE9, 0xFF2BA1D0, 0xFFFFFFFF, 0xFFFC8D42, 0xFFD6F419]


# ------------------------------------------------------------------ reading a project


class Project:
    """One generated project: its XML object table and its truth JSON."""

    def __init__(self, path: Path, raw: bytes | None = None):
        self.path = path
        raw = path.read_bytes() if raw is None else raw
        self.gzipped = raw[:2] == b"\x1f\x8b"
        self.xml = gzip.decompress(raw) if self.gzipped else raw
        self.root = ET.fromstring(self.xml)
        self.objects: dict[str, ET.Element] = {}
        self.duplicate_keys: list[str] = []
        for child in self.root:
            key = child.get("ObjectID") or child.get("ObjectUID")
            if key is None:
                continue
            if key in self.objects:
                self.duplicate_keys.append(key)
            self.objects[key] = child
        request_path = path.with_name(path.stem + ".request.json")
        self.request = json.loads(request_path.read_text(encoding="utf-8")) if request_path.exists() else None
        truth_path = path.with_suffix(".json")
        self.truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.exists() else {}
        case = self.truth.get("case", {})
        self.case = case
        self.id = case.get("id") or path.stem.replace("__", "/")

    # -- navigation

    def deref(self, e: ET.Element | None) -> ET.Element | None:
        if e is None:
            return None
        ref = e.get("ObjectRef") or e.get("ObjectURef")
        return self.objects.get(ref) if ref is not None else None

    def q(self, e: ET.Element | None, path: str) -> list[ET.Element]:
        """Child path; a segment ending in '>' follows that child's reference."""
        current = [e] if e is not None else []
        for segment in path.split("/"):
            follow = segment.endswith(">")
            tag = segment.rstrip(">")
            found = [c for parent in current for c in parent if c.tag == tag]
            if follow:
                found = [t for t in (self.deref(c) for c in found) if t is not None]
            current = found
        return current

    def one(self, e: ET.Element | None, path: str) -> ET.Element | None:
        found = self.q(e, path)
        return found[0] if found else None

    def text(self, e: ET.Element | None, path: str, default: str | None = None) -> str | None:
        found = self.one(e, path)
        return found.text if found is not None and found.text is not None else default

    def ticks(self, e: ET.Element | None, path: str) -> int:
        """A time; a missing element means 0 (see the reference, §3)."""
        return int(self.text(e, path, "0"))

    def tops(self, tag: str) -> list[ET.Element]:
        return [c for c in self.root if c.tag == tag]

    def prop(self, e: ET.Element | None, name: str) -> str | None:
        return self.text(e, f"Node/Properties/{name}")

    # -- timeline, read straight from the XML

    def sequences(self) -> list[dict]:
        out = []
        for seq in self.tops("Sequence"):
            groups = sorted(self.q(seq, "TrackGroups/TrackGroup"), key=lambda g: int(g.get("Index", "0")))
            entry = {"element": seq, "name": self.text(seq, "Name"), "groups": [], "video": [], "audio": []}
            for g in groups:
                second = self.one(g, "Second>")
                entry["groups"].append(second.tag if second is not None else None)
                if second is None or second.tag not in ("VideoTrackGroup", "AudioTrackGroup"):
                    continue
                kind = "video" if second.tag == "VideoTrackGroup" else "audio"
                entry[kind + "Group"] = second
                refs = sorted(self.q(second, "TrackGroup/Tracks/Track"), key=lambda t: int(t.get("Index", "0")))
                for ref in refs:
                    entry[kind].append(self.track(self.deref(ref), ref))
            out.append(entry)
        return out

    def track(self, track: ET.Element | None, ref: ET.Element) -> dict:
        items = self.q(track, "ClipTrack/ClipItems/TrackItems/TrackItem>")
        transitions = self.q(track, "ClipTrack/TransitionItems/TrackItems/TrackItem>")
        return {
            "element": track,
            "listIndex": ref.get("Index"),
            "index": self.text(track, "ClipTrack/Track/Index"),
            "clips": [self.clip(i) for i in items],
            "transitions": transitions,
        }

    def clip(self, item: ET.Element) -> dict:
        sub = self.one(item, "ClipTrackItem/SubClip>")
        media_clip = self.one(sub, "Clip>")
        return {
            "element": item,
            "start": self.ticks(item, "ClipTrackItem/TrackItem/Start"),
            "end": self.ticks(item, "ClipTrackItem/TrackItem/End"),
            "name": self.text(sub, "Name"),
            "inPoint": self.ticks(media_clip, "Clip/InPoint"),
            "clipElement": media_clip,
        }


def truth_sequence(p: Project, name: str | None) -> dict | None:
    for s in p.truth.get("sequences", []):
        if s.get("name") == name:
            return s
    return None


def t(value: dict | None) -> int:
    return int((value or {}).get("ticks", "0"))


# ------------------------------------------------------------------ claims


class Claim:
    def __init__(self, id: str, cases: str, check: Callable, doc: str, perturbed: bool, group: bool):
        self.id, self.cases, self.check, self.doc = id, re.compile(cases), check, doc
        self.perturbed, self.group = perturbed, group


CLAIMS: list[Claim] = []


def claim(id: str, cases: str = ".*", perturbed: bool = False, group: bool = False):
    """Register a check. `cases` is a regex on the case id; the function returns a list of
    problems, or None when the project turns out not to be evidence for the claim.

    `perturbed` checks run on the perturbed copies (out/perturbed/<major>/) and their readback
    instead of the generated projects. A `group` check gets all matching projects at once, for
    claims about how variants compare."""

    def register(fn):
        CLAIMS.append(Claim(id, cases, fn, (fn.__doc__ or "").strip(), perturbed, group))
        return fn

    return register


# §1 Container


@claim("container.gzip-xml")
def _(p):
    """The file is gzip around UTF-8 XML."""
    problems = [] if p.gzipped else ["not gzip"]
    if not p.xml.startswith(b'<?xml version="1.0" encoding="UTF-8"'):
        problems.append(f"XML declaration {p.xml[:40]!r}")
    return problems


@claim("container.root")
def _(p):
    """The root element is <PremiereData Version="3">."""
    return [] if p.root.tag == "PremiereData" and p.root.get("Version") == "3" else [f"root {p.root.tag} {p.root.attrib}"]


@claim("container.class-ids")
def _(p):
    """Every top-level object has a ClassID GUID and a Version."""
    return [f"{c.tag} lacks ClassID/Version" for c in p.objects.values() if not (c.get("ClassID") and c.get("Version"))]


# §2 Object model


@claim("objects.id-or-uid")
def _(p):
    """A top-level object has ObjectID (decimal) or ObjectUID (GUID), never both, and each key once."""
    problems = [f"duplicate key {k}" for k in p.duplicate_keys]
    guid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    for c in p.objects.values():
        oid, uid = c.get("ObjectID"), c.get("ObjectUID")
        if oid and uid:
            problems.append(f"{c.tag} has both")
        elif oid and not oid.isdigit():
            problems.append(f"{c.tag} ObjectID {oid}")
        elif uid and not guid.match(uid):
            problems.append(f"{c.tag} ObjectUID {uid}")
    return problems


@claim("objects.refs-resolve")
def _(p):
    """Outside nested tables, every ObjectRef/ObjectURef names a top-level object."""
    problems = []
    for c in p.objects.values():
        if c.tag == "Project":
            continue  # holds the nested ProjectViewState table, which has its own scope
        for e in c.iter():
            ref = e.get("ObjectRef") or e.get("ObjectURef")
            if ref is not None and ref not in p.objects:
                problems.append(f"{c.tag}/{e.tag} -> {ref} unresolved")
    return problems


@claim("objects.nested-table")
def _(p):
    """Project/…/ProjectViewState.List holds nested object definitions whose ObjectIDs repeat top-level ones."""
    view = [e for e in p.root.iter("ProjectViewState.List")]
    if not view:
        return ["no ProjectViewState.List"]
    nested = {e.get("ObjectID") for v in view for e in v.iter() if e.get("ObjectID")}
    collisions = nested & set(p.objects)
    return [] if nested and collisions else [f"{len(nested)} nested, {len(collisions)} colliding"]


@claim("objects.project-ref-first")
def _(p):
    """The root starts with <Project ObjectRef="1"/> before <Project ObjectID="1">."""
    first = list(p.root)[:2]
    ok = len(first) == 2 and first[0].tag == "Project" and first[0].get("ObjectRef") == "1" and first[1].get("ObjectID") == "1"
    return [] if ok else ["root does not start Project ref, Project 1"]


@claim("objects.list-index")
def _(p):
    """Children carrying Index are numbered 0..n-1 in document order (per parent and tag)."""
    problems = []
    for parent in p.root.iter():
        by_tag: dict[str, list[str]] = {}
        for c in parent:
            if c.get("Index") is not None:
                by_tag.setdefault(c.tag, []).append(c.get("Index"))
        for tag, idx in by_tag.items():
            if idx != [str(i) for i in range(len(idx))]:
                problems.append(f"{parent.tag}/{tag} Index {idx[:8]}")
    return problems


# §3 Time, §8 Clips: the timeline read from XML matches Premiere's readback


@claim("clips.timeline-matches-readback")
def _(p):
    """Per sequence and track, clips read from TrackItem Start/End (missing = 0), SubClip/Name and
    the clip's own Clip/InPoint match Premiere's readback, in ticks."""
    if not p.truth.get("sequences"):
        return None
    problems = []
    for s in p.sequences():
        truth = truth_sequence(p, s["name"])
        if truth is None:
            problems.append(f"sequence {s['name']} not in truth")
            continue
        for kind, key in (("video", "videoTracks"), ("audio", "audioTracks")):
            tracks = truth.get(key, [])
            if len(tracks) != len(s[kind]):
                problems.append(f"{s['name']} {kind}: {len(s[kind])} tracks, truth {len(tracks)}")
                continue
            for i, (mine, theirs) in enumerate(zip(s[kind], tracks)):
                a = sorted((c["start"], c["end"], c["inPoint"], c["name"]) for c in mine["clips"])
                b = sorted((t(c["start"]), t(c["end"]), t(c["inPoint"]), c["name"]) for c in theirs["clips"])
                if a != b:
                    problems.append(f"{s['name']} {kind} {i}: xml {a} truth {b}")
    return problems


@claim("time.start-omitted")
def _(p):
    """A clip at sequence time 0 has no TrackItem/Start element."""
    problems, seen = [], 0
    for s in p.sequences():
        for track in s["video"] + s["audio"]:
            for c in track["clips"]:
                if c["start"] == 0:
                    seen += 1
                    if p.one(c["element"], "ClipTrackItem/TrackItem/Start") is not None:
                        problems.append("explicit Start of 0")
    return problems if seen else None


@claim("time.video-frame-rate")
def _(p):
    """VideoTrackGroup/TrackGroup/FrameRate is ticks per frame (matches the readback's ticksPerFrame)."""
    problems = []
    for s in p.sequences():
        truth = truth_sequence(p, s["name"])
        if truth is None or "videoGroup" not in s:
            continue
        rate = int(p.text(s["videoGroup"], "TrackGroup/FrameRate", "0"))
        if rate != truth.get("ticksPerFrame"):
            problems.append(f"{s['name']}: {rate} vs {truth.get('ticksPerFrame')}")
    return problems if p.truth.get("sequences") else None


@claim("time.audio-frame-rate")
def _(p):
    """Audio FrameRate (sequence audio group and 48 kHz media streams) is ticks per sample: 5,292,000."""
    problems, seen = [], 0
    for s in p.sequences():
        if "audioGroup" in s:
            seen += 1
            rate = int(p.text(s["audioGroup"], "TrackGroup/FrameRate", "0"))
            if rate != TICKS_PER_SAMPLE_48K:
                problems.append(f"{s['name']} audio group {rate}")
    for stream in p.tops("AudioStream"):
        seen += 1
        rate = int(p.text(stream, "FrameRate", "0"))
        if rate != TICKS_PER_SAMPLE_48K:
            problems.append(f"AudioStream {rate}")
    return problems if seen else None


# §4 Project items and bins


def xml_bins(p: Project, item: ET.Element) -> list[dict]:
    out = []
    for child in p.q(item, "ProjectItemContainer/Items/Item>"):
        name = p.text(child, "ProjectItem/Name")
        if child.tag == "BinProjectItem":
            out.append({"name": name, "type": "bin", "children": xml_bins(p, child)})
        else:
            out.append({"name": name, "type": "item"})
    return sorted(out, key=lambda e: e["name"] or "")


def strip_bins(entries: list[dict]) -> list[dict]:
    out = []
    for e in entries:
        x = {"name": e["name"], "type": e["type"]}
        if e["type"] == "bin":
            x["children"] = strip_bins(e.get("children", []))
        out.append(x)
    return sorted(out, key=lambda e: e["name"] or "")


@claim("bins.tree")
def _(p):
    """The bin tree under RootProjectItem (ProjectItemContainer/Items/Item) matches the readback."""
    if "bins" not in p.truth:
        return None
    root = p.tops("RootProjectItem")
    if len(root) != 1:
        return [f"{len(root)} RootProjectItem"]
    mine, theirs = xml_bins(p, root[0]), strip_bins(p.truth["bins"])
    return [] if mine == theirs else [f"xml {mine} truth {theirs}"]


@claim("bins.sequence-item")
def _(p):
    """Each Sequence has a ClipProjectItem whose MasterClip leads to it via a Video/AudioSequenceSource."""
    seqs = p.tops("Sequence")
    if not seqs:
        return None
    reached = set()
    for item in p.tops("ClipProjectItem"):
        for clip in p.q(item, "MasterClip>/Clips/Clip>"):
            for source in p.q(clip, "Clip/Source>"):
                if source.tag in ("VideoSequenceSource", "AudioSequenceSource"):
                    ref = p.one(source, "SequenceSource/Sequence")
                    if ref is not None:
                        reached.add(ref.get("ObjectURef") or ref.get("ObjectRef"))
    return [f"sequence {p.text(s, 'Name')} has no project item" for s in seqs if s.get("ObjectUID") not in reached]


# §5 Media


@claim("media.relative-path")
def _(p):
    """Media/RelativePath, resolved from the project's folder, is the media file the readback names;
    FilePath and ActualMediaFilePath end with the same file name."""
    problems, seen = [], 0
    for media in p.tops("Media"):
        rel = p.text(media, "RelativePath")
        if rel is None:
            continue
        seen += 1
        target = (p.path.parent / rel.replace("\\", "/")).resolve()
        if not target.exists():
            problems.append(f"RelativePath {rel} does not resolve")
        name = rel.replace("\\", "/").rsplit("/", 1)[-1]
        for field in ("FilePath", "ActualMediaFilePath"):
            value = (p.text(media, field) or "").replace("\\", "/")
            if not value.endswith("/" + name):
                problems.append(f"{field} does not end in {name}")
    return problems if seen else None


@claim("media.frame-rect")
def _(p):
    """VideoStream/FrameRect is left,top,right,bottom: a sequence made from the media has that size."""
    problems = []
    for spec in p.case.get("sequences", []):
        truth = truth_sequence(p, spec.get("name"))
        if truth is None:
            continue
        for media in p.tops("Media"):
            if (p.text(media, "Title") or "") != spec.get("fromMedia"):
                continue
            stream = p.one(media, "VideoStream>")
            rect = p.text(stream, "FrameRect")
            if rect != f"0,0,{truth['width']},{truth['height']}":
                problems.append(f"{spec['fromMedia']} FrameRect {rect} vs {truth['width']}x{truth['height']}")
    return problems if p.case.get("sequences") else None


@claim("media.still")
def _(p):
    """A still image has VideoStream IsStill=true and Duration 10,973,491,200,000,000 ticks."""
    problems, seen = [], 0
    for media in p.tops("Media"):
        if not (p.text(media, "Title") or "").endswith(".png"):
            continue
        seen += 1
        stream = p.one(media, "VideoStream>")
        if p.text(stream, "IsStill") != "true" or int(p.text(stream, "Duration", "0")) != STILL_DURATION:
            problems.append(f"still {p.text(stream, 'IsStill')} {p.text(stream, 'Duration')}")
    return problems if seen else None


@claim("media.channel-layout")
def _(p):
    """AudioStream/AudioChannelLayout is JSON with one entry per channel (stereo 2, mono 1)."""
    problems, seen = [], 0
    for media in p.tops("Media"):
        title = p.text(media, "Title") or ""
        stream = p.one(media, "AudioStream>")
        if stream is None:
            continue
        expected = 1 if "mono" in title else 2  # the corpus's other audio is stereo
        seen += 1
        layout = json.loads(p.text(stream, "AudioChannelLayout", "[]"))
        if len(layout) != expected:
            problems.append(f"{title}: {len(layout)} channels, expected {expected}")
    return problems if seen else None


@claim("media.cache-paths")
def _(p):
    """The AudioStream of online media stores ConformedAudioPath and PeakFilePath (offline media
    drops both: see paths.offline)."""
    streams = [s for m in p.tops("Media") if p.one(m, "OfflineReason") is None for s in p.q(m, "AudioStream>")]
    if not streams:
        return None
    return [f"AudioStream lacks {f}" for s in streams for f in ("ConformedAudioPath", "PeakFilePath") if p.one(s, f) is None]


# §6 Sequences


@claim("sequence.track-groups")
def _(p):
    """A sequence has three track groups in Index order: video, audio, data."""
    return [f"{s['name']}: {s['groups']}" for s in p.sequences() if s["groups"] != ["VideoTrackGroup", "AudioTrackGroup", "DataTrackGroup"]] if p.tops("Sequence") else None


@claim("sequence.frame-size")
def _(p):
    """The sequence frame size is the VideoTrackGroup's FrameRect; it matches the readback."""
    problems = []
    for s in p.sequences():
        truth = truth_sequence(p, s["name"])
        if truth is None or "videoGroup" not in s:
            continue
        rect = p.text(s["videoGroup"], "FrameRect")
        if rect != f"0,0,{truth['width']},{truth['height']}":
            problems.append(f"{s['name']} {rect} vs {truth['width']}x{truth['height']}")
    return problems if p.truth.get("sequences") else None


@claim("sequence.work-area")
def _(p):
    """A sequence's Node/Properties hold MZ.WorkInPoint and MZ.WorkOutPoint."""
    seqs = p.tops("Sequence")
    return [f"{p.text(s, 'Name')} lacks {k}" for s in seqs for k in ("MZ.WorkInPoint", "MZ.WorkOutPoint") if p.prop(s, k) is None] if seqs else None


# §7 Tracks


@claim("tracks.new-sequence", cases=r"^basic/two-clips-gap$")
def _(p):
    """A new sequence has three video and three audio tracks; Track/Index matches the list Index."""
    problems = []
    for s in p.sequences():
        if (len(s["video"]), len(s["audio"])) != (3, 3):
            problems.append(f"{s['name']}: {len(s['video'])} video, {len(s['audio'])} audio")
        problems += [f"track Index {tr['index']} in list slot {tr['listIndex']}" for tr in s["video"] + s["audio"] if tr["index"] != tr["listIndex"]]
    return problems


@claim("tracks.name")
def _(p):
    """A track name set through the API is ClipTrack/Track/Node/Properties/MZ.TrackName; no
    ClipTrack/Track/Name is written."""
    renamed = [op for op in p.case.get("ops", []) if op.get("op") == "trackName"]
    problems = []
    for s in p.sequences():
        for track in s["video"] + s["audio"]:
            if p.one(track["element"], "ClipTrack/Track/Name") is not None:
                problems.append("ClipTrack/Track/Name present")
    if not renamed:
        return problems or None
    for s in p.sequences():
        truth = truth_sequence(p, s["name"])
        for kind, key in (("video", "videoTracks"), ("audio", "audioTracks")):
            for mine, theirs in zip(s[kind], (truth or {}).get(key, [])):
                stored = p.text(mine["element"], "ClipTrack/Track/Node/Properties/MZ.TrackName")
                if stored is not None and stored != theirs.get("name"):
                    problems.append(f"MZ.TrackName {stored} vs readback {theirs.get('name')}")
    names = {op.get("name") for op in renamed}
    stored = {p.text(tr["element"], "ClipTrack/Track/Node/Properties/MZ.TrackName") for s in p.sequences() for tr in s["video"] + s["audio"]}
    problems += [f"name {n} not stored" for n in names - stored]
    return problems


# §8 Clips


@claim("clips.master-no-range")
def _(p):
    """A MasterClip's own Video/AudioClip objects carry no InPoint or OutPoint, except a still's:
    InPoint 0 (omitted or explicit) and OutPoint 5 s, the default still duration."""
    problems, seen = [], 0
    for m in p.tops("MasterClip"):
        for c in p.q(m, "Clips/Clip>"):
            seen += 1
            stream = p.one(c, "Clip/Source>/MediaSource/Media>/VideoStream>")
            if p.text(stream, "IsStill") == "true":
                if p.ticks(c, "Clip/InPoint") != 0 or p.ticks(c, "Clip/OutPoint") != 5 * TICKS_PER_SECOND:
                    problems.append(f"still {p.text(m, 'Name')}: {p.text(c, 'Clip/InPoint')}-{p.text(c, 'Clip/OutPoint')}")
                continue
            problems += [f"{p.text(m, 'Name')} {c.tag} has {f}" for f in ("InPoint", "OutPoint") if p.one(c, f"Clip/{f}") is not None]
    return problems if seen else None


@claim("clips.trim-moves-start", cases=r"^trim/in-out")
def _(p):
    """Trimming the in point moves the clip's start by the same amount (readback: start = in point)."""
    problems = []
    for s in p.truth.get("sequences", []):
        for c in s["videoTracks"][0]["clips"]:
            if t(c["start"]) != t(c["inPoint"]):
                problems.append(f"start {t(c['start'])} in {t(c['inPoint'])}")
    return problems


@claim("clips.disabled")
def _(p):
    """A disabled clip has ClipTrackItem/IsMuted=true; enabled clips have no IsMuted=true; no IsDisabled is written."""
    problems = [f"IsDisabled in {c.tag}" for c in p.objects.values() if any(e.tag == "IsDisabled" for e in c.iter())]
    disabled_seen = False
    for s in p.sequences():
        truth = truth_sequence(p, s["name"])
        if truth is None:
            continue
        for kind, key in (("video", "videoTracks"), ("audio", "audioTracks")):
            for mine, theirs in zip(s[kind], truth.get(key, [])):
                by_range = {(t(c["start"]), t(c["end"])): c.get("disabled") for c in theirs["clips"]}
                for c in mine["clips"]:
                    muted = p.text(c["element"], "ClipTrackItem/IsMuted") == "true"
                    expected = by_range.get((c["start"], c["end"]))
                    disabled_seen |= bool(expected)
                    if expected is not None and muted != expected:
                        problems.append(f"{c['name']} IsMuted {muted}, readback disabled {expected}")
    return problems if (disabled_seen or problems) else None


@claim("clips.speed-omitted")
def _(p):
    """At 100% speed (readback speed 1), Clip/PlaybackSpeed is omitted."""
    speeds = [c.get("speed") for s in p.truth.get("sequences", []) for k in ("videoTracks", "audioTracks") for tr in s[k] for c in tr["clips"]]
    if not speeds or any(sp != 1 for sp in speeds):
        return None
    return [f"PlaybackSpeed in {c.tag}" for c in p.objects.values() if any(e.tag == "PlaybackSpeed" for e in c.iter())]


# §9 Links


@claim("links.objects")
def _(p):
    """A Link's TrackItemGroup/TrackItems/TrackItem point to clip track items; the sequence lists its
    links under PersistentGroupContainer/LinkContainer/Links."""
    links = p.tops("Link")
    if not links:
        return None
    problems = []
    for link in links:
        targets = p.q(link, "TrackItemGroup/TrackItems/TrackItem>")
        if len(targets) < 2 or any(not x.tag.endswith("ClipTrackItem") for x in targets):
            problems.append(f"link -> {[x.tag for x in targets]}")
    listed = {e.get("ObjectRef") for s in p.tops("Sequence") for e in p.q(s, "PersistentGroupContainer/LinkContainer/Links/Link")}
    problems += [f"Link {k.get('ObjectID')} not listed by a sequence" for k in links if k.get("ObjectID") not in listed]
    return problems


@claim("links.pairs", cases=r"^basic/two-clips-gap$")
def _(p):
    """Two clips with audio make two links, one per video/audio pair."""
    links = p.tops("Link")
    kinds = [sorted(x.tag for x in p.q(k, "TrackItemGroup/TrackItems/TrackItem>")) for k in links]
    return [] if len(links) == 2 and all(k == ["AudioClipTrackItem", "VideoClipTrackItem"] for k in kinds) else [f"{kinds}"]


# §10 Transitions


def ref_of(e: ET.Element | None) -> str | None:
    # Not `e or …`: an element without children is falsy in ElementTree.
    return None if e is None else (e.get("ObjectRef") or e.get("ObjectURef"))


def transitions(p: Project):
    for s in p.sequences():
        for track in s["video"]:
            for tr in track["transitions"]:
                yield s, track, tr


@claim("transitions.listed-and-referenced", cases=r"^transitions/")
def _(p):
    """A transition is listed in its track's TransitionItems; the outgoing clip's TailTransition and
    the incoming clip's HeadTransition point to it."""
    problems = []
    for s, track, tr in transitions(p):
        key = tr.get("ObjectID")
        heads = [c for c in track["clips"] if ref_of(p.one(c["element"], "ClipTrackItem/HeadTransition")) == key]
        tails = [c for c in track["clips"] if ref_of(p.one(c["element"], "ClipTrackItem/TailTransition")) == key]
        out_ = p.text(tr, "TransitionTrackItem/HasOutgoingClip") == "true"
        in_ = p.text(tr, "TransitionTrackItem/HasIncomingClip") == "true"
        if len(tails) != int(out_) or len(heads) != int(in_):
            problems.append(f"transition {key}: {len(tails)} tail / {len(heads)} head refs, flags out={out_} in={in_}")
    return problems


@claim("transitions.centred", cases=r"^transitions/cross-dissolve")
def _(p):
    """A dissolve of d frames on a cut at frame c covers c-d/2 to c+d/2 (TrackItem Start/End), and
    Alignment is the cut's offset from its start."""
    duration = p.case["ops"][4]["duration"]["frames"]
    rate = p.truth["sequences"][0]["ticksPerFrame"]
    problems = []
    found = list(transitions(p))
    for s, track, tr in found:
        cut = next(c["end"] for c in track["clips"] if p.one(c["element"], "ClipTrackItem/TailTransition") is not None)
        start, end = p.ticks(tr, "TransitionTrackItem/TrackItem/Start"), p.ticks(tr, "TransitionTrackItem/TrackItem/End")
        if (start, end) != (cut - duration * rate // 2, cut + duration * rate // 2):
            problems.append(f"frames {start / rate}-{end / rate} around cut {cut / rate}")
        if p.ticks(tr, "TransitionTrackItem/Alignment") != cut - start:
            problems.append(f"Alignment {p.text(tr, 'TransitionTrackItem/Alignment')}")
    return problems if found else ["no transition"]


@claim("transitions.single-sided", cases=r"^transitions/single-sided$")
def _(p):
    """A single-sided transition at a clip's start at time 0 has no Start, Alignment 0,
    HasOutgoingClip=false, and only a HeadTransition reference."""
    found = list(transitions(p))
    problems = [] if found else ["no transition"]
    for s, track, tr in found:
        if p.one(tr, "TransitionTrackItem/TrackItem/Start") is not None:
            problems.append("has Start")
        if p.ticks(tr, "TransitionTrackItem/Alignment") != 0 or p.text(tr, "TransitionTrackItem/HasOutgoingClip") != "false":
            problems.append("alignment or outgoing flag")
        if any(p.one(c["element"], "ClipTrackItem/TailTransition") is not None for c in track["clips"]):
            problems.append("a TailTransition exists")
    return problems


@claim("transitions.match-name", cases=r"^transitions/")
def _(p):
    """The transition the case picked by /cross ?dissolve/i is stored with MatchName 'AE.ADBE Cross
    Dissolve New'; DisplayName differs from it."""
    found = list(transitions(p))
    problems = [] if found else ["no transition"]
    for s, track, tr in found:
        if p.text(tr, "TransitionTrackItem/MatchName") != CROSS_DISSOLVE:
            problems.append(f"MatchName {p.text(tr, 'TransitionTrackItem/MatchName')}")
        if p.text(tr, "TransitionTrackItem/DisplayName") == CROSS_DISSOLVE:
            problems.append("DisplayName equals MatchName")
    return problems


@claim("transitions.api-null", cases=r"^transitions/")
def _(p):
    """Premiere 27's API returns each transition as null: the readback has one unreadable entry per
    transition object in the XML."""
    problems = []
    for s in p.sequences():
        truth = truth_sequence(p, s["name"])
        for mine, theirs in zip(s["video"], truth["videoTracks"]):
            entries = theirs.get("transitions", [])
            if len(entries) != len(mine["transitions"]) or not all(e.get("unreadable") for e in entries):
                problems.append(f"xml {len(mine['transitions'])} vs readback {entries}")
    return problems


# §11 Markers


def markers(p: Project) -> list[dict]:
    out = []
    for s in p.tops("Sequence"):
        for m in p.q(s, "MarkerOwner/Markers>/Markers/Marker/Second>"):
            data = json.loads(p.text(m, "DVAMarker", "{}"))["DVAMarker"]
            out.append(data)
    return out


@claim("markers.dva-json", cases=r"^markers/")
def _(p):
    """Sequence markers are DVAMarker JSON whose mName and mStartTime.ticks match the readback."""
    mine = sorted((int(m["mStartTime"]["ticks"]), m.get("mName", "")) for m in markers(p))
    theirs = sorted((t(m["start"]), m["name"]) for s in p.truth.get("sequences", []) for m in s["markers"])
    return [] if mine == theirs and mine else [f"xml {mine} readback {theirs}"]


@claim("markers.type", cases=r"^markers/sequence$")
def _(p):
    """mType is "Comment" or "Chapter", as the case asked."""
    asked = sorted((op.get("type") or "comment").capitalize() for op in p.case["ops"] if op["op"] == "marker")
    stored = sorted(m["mType"] for m in markers(p))
    return [] if asked == stored else [f"asked {asked} stored {stored}"]


@claim("markers.colour", cases=r"^markers/colour--\d$")
def _(p):
    """A marker colour index n is a cue-point mValue {"color": c}, c from the eight-colour table."""
    index = p.case["variedFrom"]["value"]
    problems = []
    for m in markers(p):
        values = [json.loads(cp["mValue"]).get("color") for cp in m.get("mCuePointList", []) if "color" in cp.get("mValue", "")]
        if values != [MARKER_COLOURS[index]]:
            problems.append(f"index {index}: {values}")
    return problems


# §12 Colour labels


@claim("labels.stored-index")
def _(p):
    """A project item whose readback label index is n stores BE.Prefs.LabelColors.n as its
    ProjectItem Column.PropertyText.Label and as Clip/Node/Properties/asl.clip.label.name on its
    MasterClip's clips."""
    problems, seen = [], 0
    labels = {}

    def collect(entries):
        for e in entries:
            if e["type"] == "item" and e.get("colorLabel") is not None:
                labels[e["name"]] = e["colorLabel"]
            collect(e.get("children", []))

    collect(p.truth.get("bins", []))
    for item in p.tops("ClipProjectItem"):
        name = p.text(item, "ProjectItem/Name")
        if name not in labels:
            continue
        seen += 1
        expected = f"BE.Prefs.LabelColors.{labels[name]}"
        column = p.text(item, "ProjectItem/Node/Properties/Column.PropertyText.Label")
        if column != expected:
            problems.append(f"{name}: column {column}, readback {expected}")
        for clip in p.q(item, "MasterClip>/Clips/Clip>"):
            stored = p.prop(p.one(clip, "Clip"), "asl.clip.label.name")
            if stored != expected:
                problems.append(f"{name} {clip.tag}: {stored}, readback {expected}")
    return problems if seen else None


DEFAULT_LABELS = {"video": 0, "audio+video": 1, "audio": 2, "still": 3, "sequence": 5}


def item_kind(p: Project, item: ET.Element) -> str | None:
    clips = p.q(item, "MasterClip>/Clips/Clip>")
    sources = [s for c in clips for s in p.q(c, "Clip/Source>")]
    if any(s.tag.endswith("SequenceSource") for s in sources):
        return "sequence"
    media = next((m for s in sources for m in p.q(s, "MediaSource/Media>")), None)
    if media is None:
        return None
    video, audio = p.one(media, "VideoStream>"), p.one(media, "AudioStream>")
    if video is not None and p.text(video, "IsStill") == "true":
        return "still"
    return {(True, True): "audio+video", (True, False): "video", (False, True): "audio"}.get((video is not None, audio is not None))


@claim("labels.defaults")
def _(p):
    """With Premiere's default label preferences, an item nobody labelled gets a label by kind:
    video only 0, video with audio 1, audio 2, still 3, sequence 5 (readback index)."""
    labelled = {op["media"] for op in p.case.get("ops", []) if op.get("op") == "label"}
    readback = {}

    def collect(entries):
        for e in entries:
            if e["type"] == "item":
                readback[e["name"]] = e.get("colorLabel")
            collect(e.get("children", []))

    collect(p.truth.get("bins", []))
    problems, seen = [], 0
    for item in p.tops("ClipProjectItem"):
        name = p.text(item, "ProjectItem/Name")
        kind = item_kind(p, item)
        if name in labelled or name not in readback or kind is None:
            continue
        seen += 1
        if readback[name] != DEFAULT_LABELS[kind]:
            problems.append(f"{name} ({kind}): label {readback[name]}, expected {DEFAULT_LABELS[kind]}")
    return problems if seen else None


# asl.clip.label.color per default label, as stored at import (labels/defaults, labels/index--*).
IMPORT_COLOURS = {0: 11405886, 1: 6769408, 2: 480554, 3: 8851829, 5: 19005}


@claim("labels.colour-from-import")
def _(p):
    """A master clip's asl.clip.label.color is the colour of the label its kind gets on import, and
    stays so after the label is changed (labels/index: 16 labels, one integer)."""
    problems, seen = [], 0
    for item in p.tops("ClipProjectItem"):
        kind = item_kind(p, item)
        if kind is None:
            continue
        for clip in p.q(item, "MasterClip>/Clips/Clip>"):
            stored = p.prop(p.one(clip, "Clip"), "asl.clip.label.color")
            if stored is None:
                continue
            seen += 1
            if int(stored) != IMPORT_COLOURS[DEFAULT_LABELS[kind]]:
                problems.append(f"{p.text(item, 'ProjectItem/Name')} ({kind}) {clip.tag}: {stored}")
    return problems if seen else None


@claim("labels.placed-clips-keep-label", cases=r"^(labels/after-placing|project/bins-labels)$")
def _(p):
    """Relabelling a project item after it is in a sequence does not relabel the clips already there:
    their own clips keep the label they were placed with."""
    relabelled = {op["media"]: op["color"] for op in p.case.get("ops", []) if op.get("op") == "label"}
    problems, seen = [], 0
    for s in p.sequences():
        for track in s["video"] + s["audio"]:
            for c in track["clips"]:
                master = p.one(c["element"], "ClipTrackItem/SubClip>/MasterClip>")
                name = p.text(master, "Name")
                if name not in relabelled:
                    continue
                seen += 1
                stored = p.prop(p.one(c["clipElement"], "Clip"), "asl.clip.label.name")
                if stored == f"BE.Prefs.LabelColors.{relabelled[name]}":
                    problems.append(f"{name}: timeline clip took the new label {stored}")
    return problems if seen else ["no relabelled clip in a sequence"]


# §5 Media paths: what Premiere writes on relink and offline, and what it follows on open


def one_media(p: Project, title: str = "v25_h264_aac.mp4") -> ET.Element | None:
    media = [m for m in p.tops("Media") if p.text(m, "Title") == title]
    return media[0] if len(media) == 1 else None


def readback_item(p: Project, title: str = "v25_h264_aac.mp4") -> list[dict]:
    return [b for b in p.truth.get("bins", []) if b["name"] == title]


@claim("paths.relink-rewrites-all", cases=r"^paths/relink$")
def _(p):
    """Relinking through the API (changeMediaFilePath) rewrites FilePath, ActualMediaFilePath and
    RelativePath together, to the new file; the readback links the new file."""
    m = one_media(p)
    if m is None:
        return ["not exactly one Media for the clip"]
    target = (p.path.parents[3] / "out" / "relinked" / "relink" / "v25_h264_aac.mp4").resolve()
    problems = [f"{f} {p.text(m, f)}" for f in ("FilePath", "ActualMediaFilePath")
                if Path(p.text(m, f) or "").resolve() != target]
    rel = p.text(m, "RelativePath")
    if rel is None or (p.path.parent / rel.replace("\\", "/")).resolve() != target:
        problems.append(f"RelativePath {rel}")
    links = [b.get("mediaPath") for b in readback_item(p)]
    if links != ["<corpus>/out/relinked/relink/v25_h264_aac.mp4"]:
        problems.append(f"readback links {links}")
    return problems


@claim("paths.offline", cases=r"^paths/offline$")
def _(p):
    """Offline media keeps FilePath and ActualMediaFilePath, loses RelativePath (and its
    AudioStream's ConformedAudioPath and PeakFilePath) and gains OfflineReason (5 when set
    through the API); the readback reports it offline."""
    m = one_media(p)
    if m is None:
        return ["not exactly one Media for the clip"]
    problems = [f"lacks {f}" for f in ("FilePath", "ActualMediaFilePath") if p.one(m, f) is None]
    if p.one(m, "RelativePath") is not None:
        problems.append("RelativePath still present")
    stream = p.one(m, "AudioStream>")
    problems += [f"AudioStream keeps {f}" for f in ("ConformedAudioPath", "PeakFilePath") if p.one(stream, f) is not None]
    if p.text(m, "OfflineReason") != "5":
        problems.append(f"OfflineReason {p.text(m, 'OfflineReason')}")
    if [b.get("offline") for b in readback_item(p)] != [True]:
        problems.append("readback not offline")
    return problems


def linked(p: Project) -> set:
    """Every file the readback says the clip is linked to (project item and timeline clips)."""
    found = {b.get("mediaPath") for b in readback_item(p)}
    found |= {c.get("media") for s in p.truth.get("sequences", [])
              for tr in s["videoTracks"] + s["audioTracks"] for c in tr["clips"]}
    return found


def field_target(p: Project, field: str) -> str | None:
    change = p.case.get("changes", {}).get(field, {})
    return f"<corpus>/{change['points to']}" if change.get("points to") else None


@claim("paths.perturbed-unsaved", cases=r"^paths/", perturbed=True)
def _(p):
    """Premiere opened each perturbed copy without saving over it: the file still has the hash
    perturb.py recorded."""
    expected = (p.request or {}).get("case", {}).get("projectSha256")
    if expected is None:
        return ["no projectSha256 in the request (rerun perturb.py)"]
    return [] if hashlib.sha256(p.path.read_bytes()).hexdigest() == expected else ["changed after perturb.py wrote it"]


@claim("paths.filepath-wins", cases=r"^paths/", perturbed=True)
def _(p):
    """When FilePath names an existing file, Premiere links that file, whatever
    ActualMediaFilePath and RelativePath say and whether the project looks moved."""
    if p.case["variant"]["FilePath"] != "here":
        return None
    want, got = field_target(p, "FilePath"), linked(p)
    return [] if got == {want} else [f"linked {sorted(map(str, got))}, FilePath {want}"]


@claim("paths.fallback-not-from-fields", cases=r"^paths/", perturbed=True, group=True)
def _(projects):
    """With FilePath missing, every variant of a set linked the same existing file whatever
    ActualMediaFilePath and RelativePath said, none went offline, and in each set at least one
    variant linked a file none of its own fields named: the fallback comes from state outside
    the project file."""
    sets: dict[str, list[Project]] = {}
    for p in projects:
        if p.case["variant"]["FilePath"] == "missing":
            sets.setdefault(p.id.split("--")[0], []).append(p)
    if not sets:
        return None
    problems = []
    for name, members in sorted(sets.items()):
        files = set().union(*(linked(p) for p in members))
        offline = [p.id for p in members if any(b.get("offline") for b in readback_item(p))]
        foreign = [p.id for p in members
                   if not linked(p) & {field_target(p, f) for f in ("ActualMediaFilePath", "RelativePath")}]
        if len(files) != 1:
            problems.append(f"{name}: variants linked {sorted(map(str, files))}")
        if offline:
            problems.append(f"{name}: offline in {offline}")
        if not foreign:
            problems.append(f"{name}: every variant linked a file one of its fields named")
        root = members[0].path.parents[3]
        for f in files:
            if not f or not f.startswith("<corpus>/") or not (root / f[len("<corpus>/"):]).exists():
                problems.append(f"{name}: linked file {f} does not exist")
    return problems


# ------------------------------------------------------------------ running


def run(projects: list[Project], perturbed: list[Project], verbose: bool) -> tuple[bool, dict[str, str]]:
    ok_all = True
    results = {}
    for c in CLAIMS:
        applied, failed = 0, []
        pool = [p for p in (perturbed if c.perturbed else projects) if c.cases.search(p.id)]
        units = [("group", pool)] if c.group and pool else [(p.id, p) for p in pool]
        for pid, unit in units:
            try:
                problems = c.check(unit)
            except Exception as e:  # a claim that can't be checked is a failure, not a crash
                problems = [f"check raised {type(e).__name__}: {e}"]
            if problems is None:
                continue
            applied += 1
            if problems:
                failed.append((pid, problems))
        if applied == 0:
            status = "NO EVIDENCE"
        elif failed:
            status = "FAIL"
        else:
            status = "ok"
        ok_all &= status == "ok"
        results[c.id] = status
        what = f"{len(pool)} projects as a group" if c.group else f"{applied - len(failed)}/{applied} projects"
        print(f"{status:<11} {c.id:<36} {what}{' (perturbed)' if c.perturbed else ''}")
        for pid, problems in failed if (failed and (verbose or status == "FAIL")) else []:
            for problem in problems[:3]:
                print(f"            {pid}: {problem}")
    return ok_all, results


ROW = re.compile(r"^\|(?P<claim>.*?)\|(?P<status>[^|]*)\|(?P<evidence>[^|]*)\|\s*$")
CITE = re.compile(r"\[claim: ([a-z0-9.\-]+)\]")


def check_doc(doc: Path) -> bool:
    """Every confirmed/settled row cites a claim; every citation and every claim match up."""
    ids = {c.id for c in CLAIMS}
    cited: set[str] = set()
    ok = True
    for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        m = ROW.match(line)
        if not m or m["status"].strip() in ("Status", "---"):
            continue
        cites = CITE.findall(line)
        cited.update(cites)
        status = m["status"].lower()
        if ("confirmed" in status or "settled" in status) and not cites:
            print(f"doc:{n}: confirmed/settled row cites no claim")
            ok = False
        for cite in cites:
            if cite not in ids:
                print(f"doc:{n}: cites unknown claim {cite}")
                ok = False
    for missing in sorted(ids - cited):
        print(f"doc: claim {missing} is not cited")
        ok = False
    return ok


# Self-test: each mutation changes one fact in a copy of a corpus project (in memory) and names
# the claim that must then fail. A claim that can't fail proves nothing.
MUTATIONS = [
    ("trim__in-out", "clip end moved by one tick", "clips.timeline-matches-readback",
     lambda x: x.replace("<End>812851200000</End>", "<End>812851200001</End>", 1)),
    ("basic__single-clip", "explicit Start of 0 written", "time.start-omitted",
     lambda x: re.sub(r"(<TrackItem Version=\"\d+\">)(\s*<End>)", r"\1<Start>0</Start>\2", x, count=1)),
    ("clip__disabled-renamed", "IsMuted removed from the disabled clip", "clips.disabled",
     lambda x: re.sub(r"\s*<IsMuted>true</IsMuted>", "", x, count=1)),
    ("tracks__names", "MZ.TrackName renamed", "tracks.name",
     lambda x: x.replace("MZ.TrackName", "MZ.TrackTitle")),
    ("project__bins-labels", "label 2 stored as 4 in the Project panel column", "labels.stored-index",
     lambda x: re.sub(r"(<Column\.PropertyText\.Label>BE\.Prefs\.LabelColors\.)2", r"\g<1>4", x, count=1)),
    ("transitions__cross-dissolve--20", "Alignment set to 0", "transitions.centred",
     lambda x: x.replace("<Alignment>101606400000</Alignment>", "<Alignment>0</Alignment>", 1)),
    ("transitions__cross-dissolve--20", "HeadTransition reference dropped", "transitions.listed-and-referenced",
     lambda x: re.sub(r"\s*<HeadTransition ObjectRef=\"\d+\"/>", "", x, count=1)),
    ("markers__colour--3", "marker colour changed", "markers.colour",
     lambda x: x.replace(str(MARKER_COLOURS[3]), str(MARKER_COLOURS[4]))),
    ("rates__frame-sizes", "sequence frame rect changed", "sequence.frame-size",
     lambda x: x.replace("<FrameRect>0,0,1280,720</FrameRect>", "<FrameRect>0,0,1280,721</FrameRect>")),
    ("labels__index--9", "label colour integer updated to another label's", "labels.colour-from-import",
     lambda x: x.replace("<asl.clip.label.color>6769408<", "<asl.clip.label.color>480554<", 1)),
    ("labels__index--9", "label 9 stored as 8 on a master clip", "labels.stored-index",
     lambda x: x.replace("<asl.clip.label.name>BE.Prefs.LabelColors.9<", "<asl.clip.label.name>BE.Prefs.LabelColors.8<", 1)),
    ("paths__relink", "relinked FilePath left elsewhere", "paths.relink-rewrites-all",
     lambda x: re.sub(r"<FilePath>[^<]*relinked[^<]*</FilePath>", r"<FilePath>H:\\elsewhere\\v25_h264_aac.mp4</FilePath>", x, count=1)),
    ("paths__offline", "OfflineReason removed", "paths.offline",
     lambda x: re.sub(r"\s*<OfflineReason>5</OfflineReason>", "", x, count=1)),
    ("basic__two-clips-gap", "list Index renumbered", "objects.list-index",
     lambda x: x.replace('<TrackItem Index="1" ObjectRef=', '<TrackItem Index="2" ObjectRef=', 1)),
]


def self_test(out: Path) -> bool:
    ok = True
    by_id = {c.id: c for c in CLAIMS}
    for stem, what, expected, mutate in MUTATIONS:
        path = out / f"{stem}.prproj"
        if not path.exists():
            print(f"skip       {stem}: not generated")
            ok = False
            continue
        text = gzip.decompress(path.read_bytes()).decode("utf-8")
        changed = mutate(text)
        if changed == text:
            print(f"BROKEN     {stem}: mutation '{what}' changed nothing")
            ok = False
            continue
        original, mutant = Project(path), Project(path, gzip.compress(changed.encode("utf-8")))
        caught = []
        for c in CLAIMS:
            if c.perturbed or c.group or not c.cases.search(mutant.id):
                continue
            before = c.check(original)
            try:
                after = c.check(mutant)
            except Exception as e:
                after = [str(e)]
            if not before and after:
                caught.append(c.id)
        hit = expected in caught
        ok &= hit and expected in by_id
        print(f"{'caught' if hit else 'MISSED':<10} {what} ({stem}) -> {', '.join(caught) or 'no claim failed'}")
    return ok


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    verbose = "-v" in argv
    doc = None
    if "--doc" in argv:
        i = argv.index("--doc")
        doc = Path(argv[i + 1])
        args.remove(argv[i + 1])
    out = Path(args[0]) if args else Path("out/premiere/27")
    if "--self-test" in argv:
        return 0 if self_test(out) else 1
    files = sorted(out.glob("*.prproj"))
    projects = [Project(f) for f in files]
    projects = [p for p in projects if not p.id.startswith("probe/")]
    if not projects:
        print(f"no projects in {out}")
        return 1
    # Perturbed copies live next to the generated projects: out/perturbed/<major>/ for out/premiere/<major>/.
    perturbed_dir = out.parent.parent / "perturbed" / out.name
    perturbed = [Project(f) for f in sorted(perturbed_dir.glob("*.prproj")) if f.with_suffix(".json").exists()]
    versions = sorted({str(p.truth.get("premiere", {}).get("version")) for p in projects + perturbed})
    print(f"{len(projects)} projects in {out}, {len(perturbed)} perturbed in {perturbed_dir} "
          f"(Premiere {', '.join(versions)}), {len(CLAIMS)} claims\n")
    ok, _ = run(projects, perturbed, verbose)
    if doc is not None:
        print()
        doc_ok = check_doc(doc)
        print("doc: ok" if doc_ok else "doc: FAIL")
        ok &= doc_ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
