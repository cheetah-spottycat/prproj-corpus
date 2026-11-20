# prproj-corpus

Generated Premiere Pro projects for testing `.prproj` importers, made with FilmCraft's importers in
mind, plus a claim-by-claim description of the format checked against them
([docs/format/prproj.md](docs/format/prproj.md)). Each project is made from scratch by the scripts
under `tools/`, and each comes with Premiere's own readback of what it contains (truth JSON).

**Status: early.** The generator, 20 case files and the format notes work and are checked
(`tools/format/claims.py`). The generated projects and media aren't committed yet: run the
generator to produce them. Open questions are listed at the end of the format notes. Not yet
proposed to the FilmCraft project; the layout follows `storytold/photocraft-corpus` so it could
become a shared corpus later.

**Not affiliated.** This is an independent project, not affiliated with or endorsed by Adobe or
by the FilmCraft / storytold project. Adobe and Premiere Pro are trademarks of Adobe. Premiere is
only used through its public scripting API; nothing from inside the application is read or
included (see "Rules").

## Layout

```text
tools/premiere-projects/
  media.py       synthetic media recipes (ffmpeg as a fixture generator)
  plugin/        UXP plugin that builds, saves, reopens and reads back each case
  cases/         case definitions, as data (see "Cases" below)
  check.py       privacy and truth checks on generated projects
  perturb.py     edited copies of generated projects, to see what Premiere does on open
  perturbations/ perturbation definitions, as data (see "Perturbed projects" below)
tools/format/    claims.py (checks docs/format/ against the corpus), walk.py (inspect a project),
                 inventory.py (names each importer reads)
docs/format/     what is known about the .prproj format, claim by claim
media/           generated synthetic media (not committed until the size budget is settled)
out/premiere/<major>/<area>__<case>.prproj + .json   local runs (gitignored)
out/perturbed/<major>/…   perturbed copies and their readback (gitignored)
premiere/<major>/…   published projects (later)
```

## Rules

- **Only generator-made files.** No hand-made, downloaded or personal files.
- **Clean room.** Premiere is only *used* (through its public scripting API); nothing inside the
  application is read. No Adobe sample media, presets, LUTs, templates, MOGRTs or fonts.
- **No personal details.** Saved projects must not contain user names, host names or home paths.
  Saved files are never edited afterwards; if a field leaks, change the case or the run setup.
- **Perturbed copies are labelled as such.** The one exception to "never edited" is
  `perturb.py`: it writes *copies* of generated projects with named fields changed, kept apart in
  `out/perturbed/`, each with a request file that lists every change. Premiere opens them but
  never saves them. They answer what Premiere does with what it reads, never what it writes.
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
that test ignores them. To run it, from a checkout of that pull request's branch:

```sh
FILMCRAFT_PREMIERE_TRUTH=<corpus>/out/premiere/27 cargo test -p filmcraft-interchange --lib \
  premiere_corpus_matches_scripting_api_truth -- --ignored --nocapture
```

Result with Premiere 27.0 (22 projects, 27 sequences): ranges, in points and markers 27/27.

Transitions: Premiere 27.0's API returns them as null, so the truth only records how many there
are (`{"unreadable": true}` each). The saved XML shows where they landed: the 10- and 20-frame
cross dissolves sit at frames 75–85 and 70–90, centred on the cut at 80.

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
   then **Load** with Premiere running. Open any project first, then open the panel (Window ▸ UXP
   Plugins ▸ FilmCraft corpus) and **dock it** into a panel group. A floating panel, or one opened
   on the start screen with no project open, disappears as soon as the run opens its first project.
   Window ▸ Workspaces ▸ Save Changes to This Workspace keeps it docked for next time.
3. **Corpus folder…**: choose this repository's root (once: the panel remembers it, and asks
   again only if the folder is no longer there). Optionally type a case-id regex, then **Run**.
   Each case gets a new project in `out/premiere/<major>/`, is saved, closed, reopened, read back and
   closed again. Premiere doesn't redraw the panel during a run (about a minute for all cases), so
   it shows the result, and the log with `ok` or `FAIL <reason>` per case, only at the end. The log
   is also written to `out/last-run.log` (Open perturbed: `out/last-open-perturbed.log`).
4. `python tools/premiere-projects/check.py` before sharing anything, and
   `python tools/format/claims.py out/premiere/<major> --doc docs/format/prproj.md` to check the
   format notes against what you generated.

The plugin creates, saves and closes projects. Close your own projects first. It keeps an empty
placeholder project (`tmp/run-host.prproj`, gitignored) open during the run, because Premiere hides
all panels when the last project closes, and leaves it open afterwards so the result stays visible.

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
`insert`, `trim` (moving the in point also moves the clip's start), `move` (`to` an absolute start
or `by` an offset; Premiere's action itself only shifts), `disable`, `rename`, `trackName`, `marker`, `bin`, `label`, `relink`, `offline`,
`transition` (video only: `end` "start" or "end", `match` a regex that must pick exactly one of
Premiere's video transition match names, optional `duration`, `singleSided`, `alignment`). An op works on
`sequences[op.sequence || 0]`. A case may have no sequences. `vary` makes one project per value.
`probe` logs the API's member names for tracks, track items and project items; cases under
`probe/` are diagnostics, not corpus, and `claims.py` skips them. The case is copied into the
truth JSON as `case`, so the request and Premiere's readback can be compared.

`relink` copies the media file into `out/relinked/<into>/` and relinks the item to the copy
(the API's `changeMediaFilePath`); `offline` makes the item offline.

## Perturbed projects

Some questions are about what Premiere *reads*: which of `FilePath`, `ActualMediaFilePath` and
`RelativePath` wins when they disagree, for example. No project Premiere saves can show that,
because it writes them consistently. So:

1. Generate the base cases as usual (Run).
2. `python tools/premiere-projects/perturb.py` writes the variants each definition in
   `tools/premiere-projects/perturbations/` asks for: an edited copy, a `.request.json` with
   every change, and identical media copies in `out/perturbed/media/`.
3. In the panel, **Open perturbed** (the case filter applies). Each copy is opened, read back and
   closed without saving; the readback, including the file each item is linked to
   (`bins[].mediaPath`, paths in the corpus shown as `<corpus>/…`) and whether it is offline,
   goes to `<stem>.json` next to it. The log notes which open dialogs the API lets the plugin
   suppress. If Premiere still asks where media is, answer the dialog (Offline All); variants
   with no reachable media run last.

Not reachable through UXP yet: setting clip speed and time remapping (speed can only be read).
Those need the FilmCraft round-trip route (FCP7 XML / OTIO imported into Premiere). Audio
transitions have no UXP action yet.

## Licence

Scripts, plugin and documentation: MIT OR Apache-2.0, at your option ([LICENSE-MIT](LICENSE-MIT),
[LICENSE-APACHE](LICENSE-APACHE)). Generated media and projects, once published here, are meant
to be CC0-1.0.
