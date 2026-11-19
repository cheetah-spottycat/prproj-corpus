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

document.getElementById("choose").addEventListener("click", async () => {
  const folder = await fs.getFolder();
  if (folder) {
    corpusRoot = folder;
    document.getElementById("root").textContent = folder.nativePath;
  }
});

document.getElementById("run").addEventListener("click", async () => {
  if (!corpusRoot) {
    log("Choose the corpus folder first (the folder that holds tools/ and media/).");
    return;
  }
  if (running) return;
  running = true;
  const runButton = document.getElementById("run");
  runButton.setAttribute("disabled", "");
  const pattern = document.getElementById("filter").value || "";
  logLines.length = 0;
  document.getElementById("log").textContent = "";
  const started = Date.now();
  setStatus("running", "Starting…", "Reading cases", 0);
  try {
    const { ok, total, failed } = await runAll(pattern, started);
    if (total === 0) {
      setStatus("done-fail", "Nothing to run", "No case id matches the filter.", 0);
    } else if (failed.length === 0) {
      setStatus("done-ok", `Finished: ${ok}/${total} ok`, `Took ${elapsed(started)}. Output in out/premiere/.`, 1);
    } else {
      setStatus("done-fail", `Finished: ${failed.length} of ${total} failed`, `Failed: ${failed.join(", ")} (${elapsed(started)})`, 1);
    }
  } catch (e) {
    log(`fatal: ${e && e.stack ? e.stack : e}`);
    setStatus("done-fail", "Stopped by an error", String(e && e.message ? e.message : e), 1);
  } finally {
    running = false;
    runButton.removeAttribute("disabled");
  }
  // Keep a copy of the log next to the output so it can be read outside Premiere.
  try {
    const outFolder = await subfolder(corpusRoot, ["out"], true);
    await writeText(outFolder, "last-run.log", logLines.join("\n") + "\n");
  } catch (e) {
    log(`could not write out/last-run.log: ${e}`);
  }
});

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
  async move(ctx, op) {
    const clip = await clipOf(ctx.sequence, op.clip);
    transact(ctx.project, "move", () => [clip.createMoveAction(timeFrom(op.to, ctx.rate))]);
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
};

// ---------------------------------------------------------------- readback

async function readClips(track, mediaRoot) {
  const out = [];
  const items = await track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
  for (const item of items) {
    const projectItem = await item.getProjectItem();
    let media = null;
    try {
      const path = await ppro.ClipProjectItem.cast(projectItem).getMediaFilePath();
      media = path && path.startsWith(mediaRoot) ? path.slice(mediaRoot.length).replace(/^[\\/]+/, "") : path;
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

async function readTransitions(track) {
  const out = [];
  const items = await track.getTrackItems(ppro.Constants.TrackItemType.TRANSITION, false);
  for (const item of items) {
    out.push({
      name: await item.getName(),
      matchName: await item.getMatchName(),
      start: tick(await item.getStartTime()),
      end: tick(await item.getEndTime()),
    });
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

async function readBin(folder) {
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
    if (isBin) entry.children = await readBin(ppro.FolderItem.cast(item));
    out.push(entry);
  }
  out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  return out;
}

async function readProject(project, mediaRoot) {
  const sequences = [];
  for (const sequence of await project.getSequences()) sequences.push(await readSequence(sequence, mediaRoot));
  sequences.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  return { sequences, bins: await readBin(await project.getRootItem()) };
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

async function runAll(pattern, started) {
  const cases = await loadCases(pattern);
  const major = String(uxp.host.version).split(".")[0];
  const outFolder = await subfolder(corpusRoot, ["out", "premiere", major], true);
  const mediaFolder = await subfolder(corpusRoot, MEDIA_DIR, false);
  log(`Premiere ${uxp.host.version}: ${cases.length} case(s) -> out/premiere/${major}/`);
  let ok = 0;
  const failed = [];
  for (const [i, def] of cases.entries()) {
    const fails = failed.length ? `, ${failed.length} failed` : "";
    setStatus("running", `Running ${i + 1} of ${cases.length}${fails}`, `${def.id} (${elapsed(started)} so far)`, i / cases.length);
    if (await runCase(def, outFolder, mediaFolder)) ok++;
    else failed.push(def.id);
  }
  log(`done: ${ok}/${cases.length} ok`);
  return { ok, total: cases.length, failed };
}
