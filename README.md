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
  plugin/        UXP plugin that builds, saves, reopens and reads back each case (to do)
  cases/         case definitions, as data (to do)
media/           generated synthetic media (not committed until the size budget is settled)
premiere/<version>/<area>/<case>.prproj + <case>.json   generated projects (to do)
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

## Licence

MIT OR Apache-2.0 for the scripts; generated media and projects CC0-1.0 (to be confirmed with
maintainers before publishing).
