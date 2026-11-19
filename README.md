# FilmCraft corpus (local prototype)

Generated Premiere Pro projects for testing FilmCraft's `.prproj` import. Each project is made from
scratch by the scripts under `tools/`, and each comes with Premiere's own readback of what it
contains (truth JSON).

**Status: local prototype, not published and not proposed yet.** The plan is to publish once there
is something others can build on, modelled on `storytold/photocraft-corpus`.

## Layout

```text
tools/premiere-projects/
  media.py       synthetic media recipes (ffmpeg as a fixture generator)
  plugin/        UXP plugin that builds, saves, reopens and reads back each case
  cases/         case definitions, as data (see "Cases" below)
  check.py       privacy and truth checks on generated projects
media/           generated synthetic media (not committed until the size budget is settled)
out/premiere/<major>/<area>__<case>.prproj + .json   local runs (gitignored)
premiere/<major>/…   published projects (later)
```

## Rules

- **Only generator-made files.** No hand-made, downloaded or personal files.
- **Clean room.** Premiere is only *used* (through its public scripting API); nothing inside the
  application is read. No Adobe sample media, presets, LUTs, templates, MOGRTs or fonts.
- **No personal details.** Saved projects must not contain user names, host names or home paths.
  Saved files are never edited afterwards; if a field leaks, change the case or the run setup.
- **Media comes from ffmpeg only**, as a fixture generator. It is never linked or shipped.

## Truth JSON

Compatible with the opt-in test `premiere_corpus_matches_scripting_api_truth` in
storytold/filmcraft#4504 (`FILMCRAFT_PREMIERE_TRUTH=<folder>`), which reads `<stem>.prproj` next to
`<stem>.json`:

```json
{
  "sequences": [{
    "name": "…",
    "videoTracks": [{ "clips": [{ "name": "…", "start": {"ticks": "0"}, "end": {"ticks": "…"}, "inPoint": {"ticks": "…"} }] }],
    "audioTracks": [ … ],
    "markers": [{ "start": {"ticks": "…"} }]
  }]
}
```

Extra fields (speed, enabled, bins, labels, component parameters, the case's intent) are allowed;
that test ignores them.

## Generating media

```sh
python tools/premiere-projects/media.py            # writes media/
python tools/premiere-projects/media.py --check    # regenerates into a temp folder and compares hashes
```

Needs ffmpeg with libx264 and prores_ks on PATH.

## Running the generator

Needs Premiere Pro 25.6 or later and Adobe's **UXP Developer Tool** (from the Creative Cloud app).

0. **One-time Premiere setup** (the API can't change these):
   - **Settings ▸ Plugins ▸ Enable Developer Mode**, then restart Premiere (needed for the UXP
     Developer Tool). Switch it off again when you're not generating.
   - **Settings ▸ Media Cache**: point the media cache at a neutral folder outside your user
     profile (for example `H:\fc-media-cache`). Premiere writes the peak-file and conformed-audio
     paths into every project, and the default is under `C:\Users\<you>\…`; `check.py` fails the
     run if your user name appears.
1. Quit Premiere if it has `media/` open, then `python tools/premiere-projects/media.py`.
2. In the UXP Developer Tool: **Add Plugin**, pick `tools/premiere-projects/plugin/manifest.json`,
   then **Load** with Premiere running. Open the panel (Window ▸ UXP Plugins ▸ FilmCraft corpus).
3. **Corpus folder…**: choose this repository's root. Optionally type a case-id regex, then **Run**.
   Each case gets a new project in `out/premiere/<major>/`, is saved, closed, reopened, read back and
   closed again. The panel logs `ok` or `FAIL <reason>` per case.
4. `python tools/premiere-projects/check.py` before sharing anything.

The plugin creates, saves and closes projects. Close your own projects first.

Quirk seen in Premiere 27.0: `createSequenceFromMedia` drops everything after the last dot in the
sequence name ("23.976" is stored as "23"), so case sequence names avoid dots.

Window layout: with **Import workspace from projects** on, Premiere also saves the current window
layout (`WorkspaceDefinition`, several MB, including monitor geometry) into every project, and it
grows from case to case. The plugin switches that preference off for the run and restores it
afterwards (both steps are logged), which also keeps its panel open. Projects then hold no
`WorkspaceDefinition` at all and are about 85 KB of XML (12 KB gzipped) each.

## Cases

One JSON file per case in `tools/premiere-projects/cases/`, read as data:

```json
{
  "id": "trim/in-out",
  "description": "…",
  "media": ["v25_h264_aac.mp4"],
  "sequences": [{ "name": "Main", "fromMedia": "v25_h264_aac.mp4" }],
  "ops": [{ "op": "trim", "clip": { "kind": "video", "track": 0, "index": 0 }, "in": { "frames": 10 } }],
  "vary": { "param": "ops.0.in.frames", "values": [0, 5, 10] }
}
```

Times are `{"frames": n}` at the sequence rate or `{"ticks": "…"}`. Operations: `overwrite`,
`insert`, `trim`, `move`, `disable`, `rename`, `trackName`, `marker`, `bin`, `label`. An op works on
`sequences[op.sequence || 0]`. `vary` makes one project per value. The case is copied into the
truth JSON as `case`, so the request and Premiere's readback can be compared.

Not reachable through UXP yet: setting clip speed and time remapping (speed can only be read).
Those need the FilmCraft round-trip route (FCP7 XML / OTIO imported into Premiere). Adding
transitions is in the API (`TransitionFactory`, `createAddVideoTransitionAction`) but not
implemented here yet.

## Licence

MIT OR Apache-2.0 for the scripts; generated media and projects CC0-1.0 (to be confirmed with
maintainers before publishing).
