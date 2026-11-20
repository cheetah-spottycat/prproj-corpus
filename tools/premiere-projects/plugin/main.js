// FilmCraft corpus generator: builds each case in Premiere Pro through the public UXP API,
// saves it, reopens it and writes Premiere's readback as truth JSON next to the project.
//
// Cases are data (tools/premiere-projects/cases/*.json) using the fixed operation set in OPS
// below. Nothing in a case is evaluated as code.
//
// Output: <corpus>/out/premiere/<major>/<case-id>.prproj + .json (out/ is gitignored).
//
// MIT OR Apache-2.0.
"use strict";

const ppro = require("premierepro");
const uxp = require("uxp");
const fs = uxp.storage.localFileSystem;

const GENERATOR_VERSION = "0.1.0";
const CASES_DIR = ["tools", "premiere-projects", "cases"];
const MEDIA_DIR = ["media"];

let corpusRoot = null;

// ---------------------------------------------------------------- logging and UI

const logLines = [];

function log(line) {
  logLines.push(line);
  const el = document.getElementById("log");
  el.textContent += line + "\n";
  el.scrollTop = el.scrollHeight;
}

// Status box: state is "idle", "running", "done-ok" or "done-fail".
function setStatus(state, title, detail, fraction) {
  const box = document.getElementById("status");
  box.className = state === "idle" ? "" : state;
  document.getElementById("status-title").textContent = title;
  document.getElementById("status-detail").textContent = detail || "";
  const pct = Math.max(0, Math.min(1, fraction || 0)) * 100;
  document.getElementById("bar-fill").style.width = `${pct.toFixed(1)}%`;
}

function elapsed(since) {
  const s = Math.round((Date.now() - since) / 1000);
  return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
}

let running = false;

// Every case opens a project. With "import workspace with projects" on, Premiere switches to the
// layout stored in that project, which doesn't contain this panel, so the panel disappears
// mid-run. Turn the preference off for the run and put the user's value back afterwards.
function keepWorkspaceDuringRun() {
  const pref = ppro.AppPreference;
  const key = ppro.Constants.PreferenceKey.IMPORT_WORKSPACE;
  let original;
  try {
    original = pref.getValue(key);
  } catch (e) {
    log(`note: could not read the import-workspace preference (${e}); leaving it alone`);
    return () => {};
  }
  try {
    pref.setValue(key, false, ppro.Constants.PropertyType.PERSISTENT);
    log(`note: "import workspace with projects" off for this run (was ${original})`);
  } catch (e) {
    log(`note: could not switch off "import workspace with projects" (${e})`);
    return () => {};
  }
  return () => {
    const value = original === "true" ? true : original === "false" ? false : original;
    try {
      pref.setValue(key, value, ppro.Constants.PropertyType.PERSISTENT);
      log(`note: "import workspace with projects" restored to ${original}`);
    } catch (e) {
      log(`WARNING: could not restore "import workspace with projects" to ${original}: ${e}`);
    }
  };
}

// The chosen corpus folder is remembered across sessions as a UXP persistent token (an opaque
// grant for that folder, kept in the plugin's own localStorage), so it needn't be picked each time.
// If the token no longer works (folder moved, plugin reinstalled), the panel asks again.
const ROOT_TOKEN_KEY = "corpusRootToken";

function showRoot(folder, note) {
  document.getElementById("root").textContent = folder ? `${folder.nativePath}${note ? ` (${note})` : ""}` : "(not chosen)";
}

async function restoreCorpusRoot() {
  let token = null;
  try {
    token = localStorage.getItem(ROOT_TOKEN_KEY);
    if (!token) return;
    const folder = await fs.getEntryForPersistentToken(token);
    if (!folder || !folder.isFolder) throw new Error("not a folder");
    corpusRoot = folder;
    showRoot(folder, "remembered");
  } catch (e) {
    if (token) {
      log(`note: the remembered corpus folder is no longer available (${e && e.message ? e.message : e}); choose it again`);
      try {
        localStorage.removeItem(ROOT_TOKEN_KEY);
      } catch (_) {
        // Nothing more to do; the panel asks for the folder either way.
      }
    }
  }
}

document.getElementById("choose").addEventListener("click", async () => {
  const folder = await fs.getFolder();
  if (!folder) return;
  corpusRoot = folder;
  showRoot(folder);
  try {
    localStorage.setItem(ROOT_TOKEN_KEY, await fs.createPersistentToken(folder));
  } catch (e) {
    log(`note: could not remember the corpus folder (${e && e.message ? e.message : e}); it will be asked for next time`);
  }
});

restoreCorpusRoot();

document.getElementById("run").addEventListener("click", () =>
  runTask((pattern) => runAll(pattern), "out/premiere/", "last-run.log",
    "No case in tools/premiere-projects/cases/ matches the filter. Perturbed copies (out/perturbed/) are opened with Open perturbed."));
document.getElementById("open-perturbed").addEventListener("click", () =>
  runTask((pattern) => openPerturbed(pattern), "out/perturbed/", "last-open-perturbed.log",
    "No perturbed copy in out/perturbed/ matches the filter. Make them with python tools/premiere-projects/perturb.py; generated cases run with Run."));

// Runs one batch (Run or Open perturbed) with the filter, status box and log file around it.
// Each button keeps its own log file, so one doesn't overwrite the other's.
async function runTask(work, outLabel, logName, nothingMessage) {
  if (!corpusRoot) {
    log("Choose the corpus folder first (the folder that holds tools/ and media/).");
    return;
  }
  if (running) {
    // The panel isn't redrawn during a run, so a click can arrive while one is still going.
    log("note: a run is still going; click again when the status shows it has finished");
    return;
  }
  running = true;
  const buttons = ["run", "open-perturbed"].map((id) => document.getElementById(id));
  for (const b of buttons) b.setAttribute("disabled", "");
  const pattern = document.getElementById("filter").value || "";
  logLines.length = 0;
  document.getElementById("log").textContent = "";
  const started = Date.now();
  // Premiere 27 doesn't redraw the panel while a run works on projects (tried: per-case updates,
  // pixel widths, a timer between cases), so there is no live progress: the result shows at the end.
  setStatus("running", "Running…", "The panel updates when the run is done.", 0);
  const restoreWorkspacePref = keepWorkspaceDuringRun();
  try {
    const { ok, total, failed } = await work(pattern);
    if (total === 0) {
      setStatus("done-fail", "Nothing to run", nothingMessage, 0);
    } else if (failed.length === 0) {
      setStatus("done-ok", `Finished: ${ok}/${total} ok`, `Took ${elapsed(started)}. Output in ${outLabel}.`, 1);
    } else {
      setStatus("done-fail", `Finished: ${failed.length} of ${total} failed`, `Failed: ${failed.join(", ")} (${elapsed(started)})`, 1);
    }
  } catch (e) {
    log(`fatal: ${e && e.stack ? e.stack : e}`);
    setStatus("done-fail", "Stopped by an error", String(e && e.message ? e.message : e), 1);
  } finally {
    restoreWorkspacePref();
    running = false;
    for (const b of buttons) b.removeAttribute("disabled");
  }
  // Keep a copy of the log next to the output so it can be read outside Premiere.
  try {
    const outFolder = await subfolder(corpusRoot, ["out"], true);
    await writeText(outFolder, logName, logLines.join("\n") + "\n");
  } catch (e) {
    log(`could not write out/${logName}: ${e}`);
  }
}

// ---------------------------------------------------------------- file helpers

async function subfolder(base, segments, create) {
  let folder = base;
  for (const name of segments) {
    let next = null;
    try {
      next = await folder.getEntry(name);
    } catch (_) {
      next = null;
    }
    if (!next) {
      if (!create) throw new Error(`missing folder ${segments.join("/")}`);
      next = await folder.createFolder(name);
    }
    if (!next.isFolder) throw new Error(`${name} is not a folder`);
    folder = next;
  }
  return folder;
}

function joinNative(folder, name) {
  const sep = folder.nativePath.includes("\\") ? "\\" : "/";
  return folder.nativePath.replace(/[\\/]+$/, "") + sep + name;
}

async function writeText(folder, name, text) {
  const file = await folder.createFile(name, { overwrite: true });
  await file.write(text);
}

// ---------------------------------------------------------------- cases

function getPath(obj, dotted) {
  return dotted.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

function setPath(obj, dotted, value) {
  const keys = dotted.split(".");
  const last = keys.pop();
  const target = keys.reduce((o, k) => o[k], obj);
  target[last] = value;
}

// A case with "vary" expands into one case per value, identical except for that parameter.
function expand(def) {
  if (!def.vary) return [def];
  const { param, values } = def.vary;
  if (getPath(def, param) === undefined) throw new Error(`${def.id}: vary.param ${param} not found`);
  return values.map((value) => {
    const copy = JSON.parse(JSON.stringify(def));
    delete copy.vary;
    setPath(copy, param, value);
    copy.id = `${def.id}--${String(value).replace(/[^A-Za-z0-9.+-]/g, "_")}`;
    copy.variedFrom = { id: def.id, param, value };
    return copy;
  });
}

async function loadCases(pattern) {
  const folder = await subfolder(corpusRoot, CASES_DIR, false);
  const entries = (await folder.getEntries()).filter((e) => e.isFile && e.name.endsWith(".json"));
  entries.sort((a, b) => (a.name < b.name ? -1 : 1));
  const re = new RegExp(pattern);
  const cases = [];
  for (const entry of entries) {
    const def = JSON.parse(await entry.read());
    for (const c of expand(def)) if (re.test(c.id)) cases.push(c);
  }
  return cases;
}

// ---------------------------------------------------------------- time

function tick(t) {
  return { ticks: String(t.ticks) };
}

async function frameRateOf(sequence) {
  const settings = await sequence.getSettings();
  return await settings.getVideoFrameRate();
}

function timeFrom(spec, frameRate) {
  if (spec == null) return null;
  if (spec.ticks != null) return ppro.TickTime.createWithTicks(String(spec.ticks));
  if (spec.frames != null) return ppro.TickTime.createWithFrameAndFrameRate(spec.frames, frameRate);
  throw new Error(`time needs "frames" or "ticks": ${JSON.stringify(spec)}`);
}

// ---------------------------------------------------------------- project helpers

function transact(project, label, build) {
  let ok = false;
  project.lockedAccess(() => {
    ok = project.executeTransaction((compound) => {
      for (const action of build()) compound.addAction(action);
    }, label);
  });
  if (!ok) throw new Error(`transaction failed: ${label}`);
}

async function findItem(folder, name) {
  for (const item of await folder.getItems()) {
    if (item.name === name) return item;
    if (item.type === ppro.Constants.ProjectItemType.BIN) {
      const found = await findItem(ppro.FolderItem.cast(item), name);
      if (found) return found;
    }
  }
  return null;
}

async function mediaItem(project, name) {
  const item = await findItem(await project.getRootItem(), name);
  if (!item) throw new Error(`media ${name} not in project`);
  return item;
}

async function trackOf(sequence, kind, index) {
  if (kind === "video") return sequence.getVideoTrack(index);
  if (kind === "audio") return sequence.getAudioTrack(index);
  throw new Error(`kind must be video or audio, got ${kind}`);
}

async function clipOf(sequence, ref) {
  const track = await trackOf(sequence, ref.kind, ref.track);
  const items = await track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
  const item = items[ref.index];
  if (!item) throw new Error(`no ${ref.kind} clip ${ref.index} on track ${ref.track}`);
  return item;
}

const MARKER_TYPES = {
  comment: () => ppro.Marker.MARKER_TYPE_COMMENT,
  chapter: () => ppro.Marker.MARKER_TYPE_CHAPTER,
  weblink: () => ppro.Marker.MARKER_TYPE_WEBLINK,
  cuepoint: () => ppro.Marker.MARKER_TYPE_FLVCUEPOINT,
};

// ---------------------------------------------------------------- operations

const OPS = {
  async overwrite(ctx, op) {
    const editor = ppro.SequenceEditor.getEditor(ctx.sequence);
    const item = await mediaItem(ctx.project, op.media);
    const at = timeFrom(op.at, ctx.rate);
    transact(ctx.project, "overwrite", () => [editor.createOverwriteItemAction(item, at, op.video, op.audio)]);
  },
  async insert(ctx, op) {
    const editor = ppro.SequenceEditor.getEditor(ctx.sequence);
    const item = await mediaItem(ctx.project, op.media);
    const at = timeFrom(op.at, ctx.rate);
    transact(ctx.project, "insert", () => [
      editor.createInsertProjectItemAction(item, at, op.video, op.audio, Boolean(op.limitShift)),
    ]);
  },
  async trim(ctx, op) {
    const clip = await clipOf(ctx.sequence, op.clip);
    const inPoint = op.in ? timeFrom(op.in, ctx.rate) : null;
    const outPoint = op.out ? timeFrom(op.out, ctx.rate) : null;
    // Actions must be created inside lockedAccess, so build them in the callback.
    transact(ctx.project, "trim", () => {
      const actions = [];
      if (inPoint) actions.push(clip.createSetInPointAction(inPoint));
      if (outPoint) actions.push(clip.createSetOutPointAction(outPoint));
      return actions;
    });
  },
  // createMoveAction shifts a clip by an offset. `by` passes the offset through; `to` is an absolute
  // start time, turned into the offset from the clip's current start.
  async move(ctx, op) {
    const clip = await clipOf(ctx.sequence, op.clip);
    let shift;
    if (op.by) {
      shift = timeFrom(op.by, ctx.rate);
    } else if (op.to) {
      const target = BigInt(timeFrom(op.to, ctx.rate).ticks);
      const start = BigInt((await clip.getStartTime()).ticks);
      shift = ppro.TickTime.createWithTicks(String(target - start));
    } else {
      throw new Error('move needs "to" or "by"');
    }
    transact(ctx.project, "move", () => [clip.createMoveAction(shift)]);
  },
  async disable(ctx, op) {
    const clip = await clipOf(ctx.sequence, op.clip);
    transact(ctx.project, "disable", () => [clip.createSetDisabledAction(op.disabled !== false)]);
  },
  async rename(ctx, op) {
    const clip = await clipOf(ctx.sequence, op.clip);
    transact(ctx.project, "rename", () => [clip.createSetNameAction(op.name)]);
  },
  async trackName(ctx, op) {
    const track = await trackOf(ctx.sequence, op.kind, op.track);
    transact(ctx.project, "track name", () => [track.createSetNameAction(op.name)]);
  },
  async marker(ctx, op) {
    const markers = await ppro.Markers.getMarkers(ctx.sequence);
    const type = MARKER_TYPES[op.type || "comment"];
    if (!type) throw new Error(`unknown marker type ${op.type}`);
    const at = timeFrom(op.at, ctx.rate);
    const duration = op.duration ? timeFrom(op.duration, ctx.rate) : ppro.TickTime.TIME_ZERO;
    transact(ctx.project, "marker", () => [
      markers.createAddMarkerAction(op.name || "", type(), at, duration, op.comment || ""),
    ]);
    if (op.color != null) {
      const all = markers.getMarkers();
      const added = all.find((m) => m.getStart().equals(at) && m.getName() === (op.name || ""));
      if (!added) throw new Error("added marker not found for colour");
      transact(ctx.project, "marker colour", () => [added.createSetColorByIndexAction(op.color)]);
    }
  },
  async bin(ctx, op) {
    const root = await ctx.project.getRootItem();
    const parent = op.parent ? ppro.FolderItem.cast(await mediaItem(ctx.project, op.parent)) : root;
    transact(ctx.project, "bin", () => [parent.createBinAction(op.name, false)]);
    const bin = ppro.FolderItem.cast(await findItem(parent, op.name));
    for (const name of op.items || []) {
      const item = await mediaItem(ctx.project, name);
      const from = item.getParentBin();
      transact(ctx.project, "file into bin", () => [from.createMoveItemAction(item, bin)]);
    }
  },
  async label(ctx, op) {
    const item = await mediaItem(ctx.project, op.media);
    transact(ctx.project, "label", () => [item.createSetColorLabelAction(op.color)]);
  },
  // Relinks the media to an identical copy of its file in out/relinked/<into>/ (copied by the
  // plugin), through the API's changeMediaFilePath: shows what relinking writes.
  async relink(ctx, op) {
    const item = ppro.ClipProjectItem.cast(await mediaItem(ctx.project, op.media));
    if (!(await item.canChangeMediaPath())) throw new Error(`relink: Premiere says ${op.media} can't change its media path`);
    if (typeof op.into !== "string" || !/^[A-Za-z0-9_-]+$/.test(op.into)) throw new Error('relink needs "into", a plain folder name');
    const source = await (await subfolder(corpusRoot, MEDIA_DIR, false)).getEntry(op.media);
    const dest = await subfolder(corpusRoot, ["out", "relinked", op.into], true);
    const copy = await source.copyTo(dest, { overwrite: true });
    if (!(await item.changeMediaFilePath(copy.nativePath, false))) throw new Error("relink: changeMediaFilePath returned false");
  },
  async offline(ctx, op) {
    const item = ppro.ClipProjectItem.cast(await mediaItem(ctx.project, op.media));
    transact(ctx.project, "offline", () => [item.createSetOfflineAction()]);
  },
  // Diagnostic: logs the member names the public API offers on tracks, a clip track item, a
  // project item and the sequence, and the module's exports, so cases are written for what the
  // API can actually do. Changes nothing.
  async probe(ctx, op) {
    const targets = {
      sequence: async () => ctx.sequence,
      videoTrack: () => trackOf(ctx.sequence, "video", 0),
      audioTrack: () => trackOf(ctx.sequence, "audio", 0),
      clipTrackItem: () => clipOf(ctx.sequence, { kind: "video", track: 0, index: 0 }),
      projectItem: () => mediaItem(ctx.project, op.media),
      clipProjectItem: async () => ppro.ClipProjectItem.cast(await mediaItem(ctx.project, op.media)),
    };
    for (const [label, get] of Object.entries(targets)) {
      try {
        log(`probe ${label}: ${memberNames(await get()).join(", ")}`);
      } catch (e) {
        log(`probe ${label}: unavailable (${e && e.message ? e.message : e})`);
      }
    }
    log(`probe premierepro: ${Object.keys(ppro).sort().join(", ")}`);
    log(`probe Constants: ${Object.keys(ppro.Constants || {}).sort().join(", ")}`);
  },
  // Video transition on one end of a clip. `match` is a case-insensitive regex that must pick out
  // exactly one of Premiere's installed video transition match names; on a miss the error lists
  // them, so a case can be fixed without guessing.
  async transition(ctx, op) {
    if (op.clip.kind !== "video") throw new Error("transition: only video clips are supported");
    const clip = await clipOf(ctx.sequence, op.clip);
    const matchName = await transitionMatchName(op.match);
    const duration = op.duration ? timeFrom(op.duration, ctx.rate) : null;
    // Actions (and the objects they take) are created inside lockedAccess.
    transact(ctx.project, "transition", () => {
      const options = new ppro.AddTransitionOptions().setApplyToStart(op.end === "start");
      if (duration) options.setDuration(duration);
      if (op.singleSided != null) options.setForceSingleSided(Boolean(op.singleSided));
      if (op.alignment != null) options.setTransitionAlignment(op.alignment);
      return [clip.createAddVideoTransitionAction(ppro.TransitionFactory.createVideoTransition(matchName), options)];
    });
  },
};

// Every property name on an object and its prototypes, without Object's own.
function memberNames(obj) {
  if (!obj) return ["(null)"];
  const names = new Set();
  for (let o = obj; o && o !== Object.prototype; o = Object.getPrototypeOf(o)) {
    for (const name of Object.getOwnPropertyNames(o)) if (name !== "constructor") names.add(name);
  }
  return [...names].sort();
}

async function transitionMatchName(pattern) {
  if (typeof pattern !== "string" || !pattern) throw new Error('transition needs "match"');
  const names = await ppro.TransitionFactory.getVideoTransitionMatchNames();
  const re = new RegExp(pattern, "i");
  const hits = names.filter((n) => re.test(n));
  if (hits.length === 1) return hits[0];
  const why = hits.length ? `matches ${hits.length}: ${hits.join(", ")}` : `matches none of: ${names.join(", ")}`;
  throw new Error(`transition match /${pattern}/ ${why}`);
}

// ---------------------------------------------------------------- readback

// A media path as written to truth JSON: relative to media/ (as before) for the corpus media,
// `<corpus>/…` for files elsewhere in the corpus folder (copies made for relinking and
// perturbation), so truth never holds the machine's absolute corpus path.
function shownPath(path, mediaRoot) {
  if (!path) return path;
  const under = (root) => {
    if (!root) return false;
    const p = path.toLowerCase();
    const r = root.toLowerCase();
    return p.startsWith(r + "\\") || p.startsWith(r + "/");
  };
  if (under(mediaRoot)) return path.slice(mediaRoot.length).replace(/^[\\/]+/, "");
  const corpus = corpusRoot ? corpusRoot.nativePath.replace(/[\\/]+$/, "") : null;
  if (corpus && under(corpus)) return "<corpus>/" + path.slice(corpus.length).replace(/^[\\/]+/, "").replace(/\\/g, "/");
  return path;
}

async function readClips(track, mediaRoot) {
  const out = [];
  const items = await track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
  for (const item of items) {
    const projectItem = await item.getProjectItem();
    let media = null;
    try {
      media = shownPath(await ppro.ClipProjectItem.cast(projectItem).getMediaFilePath(), mediaRoot);
    } catch (_) {
      media = null;
    }
    out.push({
      name: await item.getName(),
      start: tick(await item.getStartTime()),
      end: tick(await item.getEndTime()),
      inPoint: tick(await item.getInPoint()),
      outPoint: tick(await item.getOutPoint()),
      speed: await item.getSpeed(),
      reversed: Boolean(await item.isSpeedReversed()),
      disabled: await item.isDisabled(),
      projectItem: projectItem ? projectItem.name : null,
      media,
    });
  }
  return out;
}

// Premiere 27.0 returns one entry per transition but every entry is null (the UXP API has no
// transition track item type yet), so only the count is known. Each entry is recorded as
// {unreadable: true} rather than dropped, so the truth still says how many transitions exist;
// fields are filled in if a later version returns real objects.
async function readTransitions(track) {
  const out = [];
  const items = await track.getTrackItems(ppro.Constants.TrackItemType.TRANSITION, false);
  for (const item of items || []) {
    if (!item) {
      out.push({ unreadable: true });
      continue;
    }
    try {
      out.push({
        name: await item.getName(),
        matchName: await item.getMatchName(),
        start: tick(await item.getStartTime()),
        end: tick(await item.getEndTime()),
      });
    } catch (e) {
      out.push({ unreadable: true, error: String(e && e.message ? e.message : e) });
    }
  }
  return out;
}

async function readTracks(sequence, kind, mediaRoot) {
  const count = kind === "video" ? await sequence.getVideoTrackCount() : await sequence.getAudioTrackCount();
  const tracks = [];
  for (let i = 0; i < count; i++) {
    const track = await trackOf(sequence, kind, i);
    const entry = { index: i, name: track.name, muted: await track.isMuted(), clips: await readClips(track, mediaRoot) };
    if (kind === "video") entry.transitions = await readTransitions(track);
    tracks.push(entry);
  }
  return tracks;
}

async function readSequence(sequence, mediaRoot) {
  const rate = await frameRateOf(sequence);
  const settings = await sequence.getSettings();
  const rect = await settings.getVideoFrameRect();
  const item = await sequence.getProjectItem();
  const markers = (await ppro.Markers.getMarkers(sequence)).getMarkers().map((m) => ({
    name: m.getName(),
    type: m.getType(),
    start: tick(m.getStart()),
    duration: tick(m.getDuration()),
    comment: m.getComments(),
    colorIndex: m.getColorIndex(),
  }));
  markers.sort((a, b) => (BigInt(a.start.ticks) < BigInt(b.start.ticks) ? -1 : 1));
  return {
    name: item ? item.name : null,
    frameRate: rate.value,
    ticksPerFrame: rate.ticksPerFrame,
    width: rect.width,
    height: rect.height,
    end: tick(await sequence.getEndTime()),
    videoTracks: await readTracks(sequence, "video", mediaRoot),
    audioTracks: await readTracks(sequence, "audio", mediaRoot),
    markers,
  };
}

async function readBin(folder, mediaRoot) {
  const out = [];
  for (const item of await folder.getItems()) {
    const isBin = item.type === ppro.Constants.ProjectItemType.BIN;
    let colorLabel = null;
    try {
      colorLabel = await item.getColorLabelIndex();
    } catch (_) {
      // Premiere 27.0 throws "Failed to get color label property" for some items (seen on bins).
      colorLabel = null;
    }
    const entry = { name: item.name, type: isBin ? "bin" : "item", colorLabel };
    if (isBin) {
      entry.children = await readBin(ppro.FolderItem.cast(item), mediaRoot);
    } else {
      // Which file the item is linked to, and whether it is offline (what path precedence is about).
      try {
        const clip = ppro.ClipProjectItem.cast(item);
        if (!(await clip.isSequence())) {
          entry.mediaPath = shownPath(await clip.getMediaFilePath(), mediaRoot);
          entry.offline = Boolean(await clip.isOffline());
        }
      } catch (e) {
        entry.mediaError = String(e && e.message ? e.message : e);
      }
    }
    out.push(entry);
  }
  out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  return out;
}

async function readProject(project, mediaRoot) {
  const sequences = [];
  for (const sequence of await project.getSequences()) sequences.push(await readSequence(sequence, mediaRoot));
  sequences.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  return { sequences, bins: await readBin(await project.getRootItem(), mediaRoot) };
}

// ---------------------------------------------------------------- one case

async function closeQuietly(project) {
  if (!project) return;
  try {
    await project.close(new ppro.CloseProjectOptions().setPromptIfDirty(false).setShowCancelButton(false));
  } catch (_) {
    // Already closed or Premiere refused; the next case creates its own project.
  }
}

async function runCase(def, outFolder, mediaFolder) {
  const fileStem = def.id.replace(/\//g, "__");
  const projectPath = joinNative(outFolder, `${fileStem}.prproj`);
  const mediaRoot = mediaFolder.nativePath.replace(/[\\/]+$/, "");
  let project = null;
  try {
    project = await ppro.Project.createProject(projectPath);
    if (!project) throw new Error("createProject returned nothing");

    const paths = (def.media || []).map((name) => joinNative(mediaFolder, name));
    if (paths.length && !(await project.importFiles(paths, true, await project.getRootItem(), false))) {
      throw new Error("importFiles failed");
    }

    const sequences = [];
    for (const spec of def.sequences || []) {
      if (!spec.fromMedia) throw new Error(`sequence ${spec.name}: only "fromMedia" is supported so far`);
      const clip = ppro.ClipProjectItem.cast(await mediaItem(project, spec.fromMedia));
      const sequence = await project.createSequenceFromMedia(spec.name, [clip]);
      if (!sequence) throw new Error(`createSequenceFromMedia failed for ${spec.name}`);
      sequences.push(sequence);
    }

    for (const [i, op] of (def.ops || []).entries()) {
      const handler = OPS[op.op];
      if (!handler) throw new Error(`op ${i}: unknown operation "${op.op}"`);
      const sequence = sequences[op.sequence || 0];
      const ctx = { project, sequence, rate: sequence ? await frameRateOf(sequence) : null };
      await handler(ctx, op);
    }

    if (!(await project.save())) throw new Error("save failed");
    await closeQuietly(project);
    project = null;

    // Read back from the saved file, not from the in-memory project.
    project = await ppro.Project.open(projectPath, new ppro.OpenProjectOptions().setAddToMRUList(false));
    const readback = await readProject(project, mediaRoot);
    await closeQuietly(project);
    project = null;

    const truth = {
      generator: { name: "filmcraft-corpus/premiere-projects", version: GENERATOR_VERSION },
      premiere: { version: uxp.host.version },
      case: def,
      ...readback,
    };
    await writeText(outFolder, `${fileStem}.json`, JSON.stringify(truth, null, 2) + "\n");
    log(`ok   ${def.id}`);
    return true;
  } catch (e) {
    log(`FAIL ${def.id}: ${e && e.message ? e.message : e}`);
    if (e && e.stack) log(e.stack);
    return false;
  } finally {
    await closeQuietly(project);
  }
}

// Each case closes its project. When the last open project closes, Premiere goes back to its
// start screen and hides every panel, this one included. An empty placeholder project in tmp/
// (gitignored, never part of the corpus) stays open during the run so that never happens. It is
// left open afterwards too, so the finished status stays visible; close it whenever you like.
const HOST_PROJECT = "run-host.prproj";

async function openHostProject() {
  try {
    const tmp = await subfolder(corpusRoot, ["tmp"], true);
    let exists = false;
    try {
      exists = Boolean(await tmp.getEntry(HOST_PROJECT));
    } catch (_) {
      exists = false;
    }
    const path = joinNative(tmp, HOST_PROJECT);
    const host = exists
      ? await ppro.Project.open(path, new ppro.OpenProjectOptions().setAddToMRUList(false))
      : await ppro.Project.createProject(path);
    if (!host) throw new Error("no project returned");
    log(`note: placeholder project tmp/${HOST_PROJECT} open for this run (left open afterwards)`);
    return host;
  } catch (e) {
    log(`note: could not open a placeholder project (${e && e.message ? e.message : e}); the panel may be hidden during the run`);
    return null;
  }
}

async function runAll(pattern) {
  const cases = await loadCases(pattern);
  const major = String(uxp.host.version).split(".")[0];
  const outFolder = await subfolder(corpusRoot, ["out", "premiere", major], true);
  const mediaFolder = await subfolder(corpusRoot, MEDIA_DIR, false);
  log(`Premiere ${uxp.host.version}: ${cases.length} case(s) -> out/premiere/${major}/`);
  let ok = 0;
  const failed = [];
  if (cases.length) await openHostProject();
  for (const [i, def] of cases.entries()) {
    if (await runCase(def, outFolder, mediaFolder)) ok++;
    else failed.push(def.id);
  }
  log(`done: ${ok}/${cases.length} ok`);
  return { ok, total: cases.length, failed };
}

// ---------------------------------------------------------------- perturbed projects

// Options for opening an edited copy: no recent-files entry, and no dialogs where this Premiere
// lets the API turn them off (a "where is this media?" dialog would stop the run until answered).
function perturbedOpenOptions() {
  let options = new ppro.OpenProjectOptions().setAddToMRUList(false);
  const applied = [];
  const missing = [];
  for (const name of ["setShowLocateFileDialog", "setShowWarningDialog", "setShowConvertProjectDialog"]) {
    if (typeof options[name] === "function") {
      options = options[name](false) || options;
      applied.push(name);
    } else {
      missing.push(name);
    }
  }
  log(`note: open options: ${applied.join(", ") || "none"} set to false${missing.length ? `; not offered: ${missing.join(", ")}` : ""}`);
  log(`note: OpenProjectOptions offers: ${memberNames(options).join(", ")}`);
  return options;
}

// Opens each copy made by tools/premiere-projects/perturb.py, reads it back and closes it without
// saving. The copy is never written by Premiere; its <stem>.json records the request and readback.
async function openPerturbed(pattern) {
  const major = String(uxp.host.version).split(".")[0];
  let folder;
  try {
    folder = await subfolder(corpusRoot, ["out", "perturbed", major], false);
  } catch (_) {
    throw new Error(`no out/perturbed/${major}/: run python tools/premiere-projects/perturb.py first`);
  }
  const mediaRoot = (await subfolder(corpusRoot, MEDIA_DIR, false)).nativePath.replace(/[\/]+$/, "");
  const re = new RegExp(pattern);
  const requests = [];
  for (const entry of await folder.getEntries()) {
    if (!entry.isFile || !entry.name.endsWith(".request.json")) continue;
    const request = JSON.parse(await entry.read());
    if (re.test(request.case.id)) requests.push({ stem: entry.name.slice(0, -".request.json".length), request });
  }
  // Copies whose media can't be found anywhere go last: they are the likeliest to raise a dialog.
  const reachable = (r) => Object.values(r.request.case.changes || {}).some((c) => c && c.exists === true);
  requests.sort((a, b) => Number(!reachable(a)) - Number(!reachable(b)) || (a.stem < b.stem ? -1 : a.stem > b.stem ? 1 : 0));
  log(`Premiere ${uxp.host.version}: ${requests.length} perturbed project(s) in out/perturbed/${major}/`);
  if (requests.length) await openHostProject();
  const options = perturbedOpenOptions();
  let ok = 0;
  const failed = [];
  for (const { stem, request } of requests) {
    let project = null;
    try {
      project = await ppro.Project.open(joinNative(folder, `${stem}.prproj`), options);
      if (!project) throw new Error("open returned nothing");
      const readback = await readProject(project, mediaRoot);
      await closeQuietly(project);
      project = null;
      const truth = {
        generator: { name: "filmcraft-corpus/premiere-projects", version: GENERATOR_VERSION, mode: "open perturbed" },
        premiere: { version: uxp.host.version },
        case: request.case,
        ...readback,
      };
      await writeText(folder, `${stem}.json`, JSON.stringify(truth, null, 2) + "\n");
      log(`ok   ${request.case.id}`);
      ok++;
    } catch (e) {
      log(`FAIL ${request.case.id}: ${e && e.message ? e.message : e}`);
      failed.push(request.case.id);
    } finally {
      await closeQuietly(project);
    }
  }
  log(`done: ${ok}/${requests.length} ok`);
  return { ok, total: requests.length, failed };
}
