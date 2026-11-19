#!/usr/bin/env python3
"""Generate the corpus's synthetic media with ffmpeg (used only as a fixture generator).

Every file is made from ffmpeg's built-in sources: `testsrc` (colour pattern with a frame
counter drawn without fonts) and `sine`. No external media, fonts or presets are read.
Outputs are written bit-exact (no encoder tags, no timestamps, single-threaded x264), so a
rerun with the same ffmpeg build gives the same bytes; `--check` verifies that.

Usage:
    python media.py [--out DIR] [--check]

MIT OR Apache-2.0.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "media"
MANIFEST = "SHA256SUMS"

# Realistic frame sizes: sequences made from this media get the settings real projects have, and
# Motion / scale parameters are in the pixel ranges an importer will meet. The test pattern
# compresses well, so H.264 stays small; ProRes uses the Proxy profile for the same reason.
HD = "1920x1080"
SECONDS = 4

BITEXACT = ["-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]


@dataclass(frozen=True)
class Recipe:
    name: str
    description: str
    args: tuple[str, ...]


def video_src(rate: str, size: str = HD) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={SECONDS}"]


def tone_src(freq: int, channels: int = 2, rate: int = 48000) -> list[str]:
    layout = "stereo" if channels == 2 else "mono"
    return [
        "-f", "lavfi", "-i",
        f"sine=frequency={freq}:sample_rate={rate}:duration={SECONDS},"
        f"aformat=channel_layouts={layout}",
    ]


X264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "1",
        "-x264-params", "threads=1:lookahead-threads=1"]
# Profile 0 = ProRes 422 Proxy: still 10-bit 4:2:2, at a fraction of 422 HQ's size.
PRORES = ["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]

RECIPES: tuple[Recipe, ...] = (
    Recipe("v25_h264_aac.mp4", "25 fps H.264 + 1 kHz stereo AAC",
           tuple(video_src("25") + tone_src(1000) + X264 + ["-c:a", "aac", "-b:a", "128k"])),
    Recipe("v2398_prores_pcm.mov", "23.976 fps ProRes 422 Proxy + 500 Hz stereo 24-bit PCM",
           tuple(video_src("24000/1001") + tone_src(500) + PRORES + ["-c:a", "pcm_s24le"])),
    Recipe("v2997_h264_aac.mp4", "29.97 fps H.264 + 750 Hz stereo AAC",
           tuple(video_src("30000/1001") + tone_src(750) + X264 + ["-c:a", "aac", "-b:a", "128k"])),
    Recipe("v5994_h264.mp4", "59.94 fps H.264, no audio",
           tuple(video_src("60000/1001") + X264)),
    Recipe("v25_h264_720p.mp4", "1280x720 25 fps H.264, no audio",
           tuple(video_src("25", "1280x720") + X264)),
    Recipe("v25_h264_uhd.mp4", "3840x2160 25 fps H.264, no audio",
           tuple(video_src("25", "3840x2160") + X264)),
    Recipe("v25_h264_vertical.mp4", "1080x1920 (vertical) 25 fps H.264, no audio",
           tuple(video_src("25", "1080x1920") + X264)),
    Recipe("a48k_440_stereo.wav", "440 Hz stereo, 48 kHz 24-bit",
           tuple(tone_src(440) + ["-c:a", "pcm_s24le"])),
    Recipe("a48k_880_mono.wav", "880 Hz mono, 48 kHz 24-bit",
           tuple(tone_src(880, channels=1) + ["-c:a", "pcm_s24le"])),
    Recipe("still_1920x1080.png", "single 1920x1080 test-pattern frame",
           ("-f", "lavfi", "-i", f"testsrc=size={HD}:rate=1:duration=1", "-frames:v", "1")),
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def generate(out: Path) -> dict[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg not found on PATH")
    out.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for recipe in RECIPES:
        target = out / recipe.name
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *recipe.args, *BITEXACT, str(target)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"{recipe.name}: ffmpeg failed\n{result.stderr.strip()}")
        hashes[recipe.name] = sha256(target)
        print(f"ok   {recipe.name:<24} {target.stat().st_size:>8} B  {recipe.description}")
    return hashes


def write_manifest(out: Path, hashes: dict[str, str]) -> None:
    lines = [f"{digest}  {name}\n" for name, digest in sorted(hashes.items())]
    (out / MANIFEST).write_text("".join(lines), encoding="utf-8", newline="\n")


def read_manifest(out: Path) -> dict[str, str]:
    path = out / MANIFEST
    if not path.exists():
        return {}
    pairs = (line.split(None, 1) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {name.strip(): digest for digest, name in pairs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="regenerate into a temp folder and compare with the manifest")
    args = parser.parse_args()

    if not args.check:
        write_manifest(args.out, generate(args.out))
        print(f"wrote {args.out / MANIFEST}")
        return 0

    expected = read_manifest(args.out)
    if not expected:
        print(f"no {MANIFEST} in {args.out}; run without --check first", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        actual = generate(Path(tmp))
    bad = sorted(n for n in expected.keys() | actual.keys() if expected.get(n) != actual.get(n))
    for name in bad:
        print(f"diff {name}: manifest {expected.get(name, '-')[:12]} regenerated {actual.get(name, '-')[:12]}")
    print("reproducible" if not bad else f"{len(bad)} file(s) differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
