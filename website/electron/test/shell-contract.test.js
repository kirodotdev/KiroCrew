/**
 * Drift guards for the Electron shell.
 *
 * Both failures these catch are SILENT in development and only appear in a
 * packaged build or at runtime:
 *
 *   1. electron-builder ships an explicit per-file allowlist. A new module that
 *      is required but not listed works perfectly from source and is simply
 *      MISSING from the DMG. A renamed module leaves a stale entry.
 *   2. IPC is stringly-typed on both sides. A channel that one side sends and
 *      the other never listens for is a no-op — no error, no warning. That is
 *      exactly how sixteen pet channels went missing during the Mochi port
 *      (drag handoff, walking, bubbles) while everything still "worked".
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const WINDOW_SOURCE = fs.readFileSync(path.join(ROOT, "window-lifecycle.js"), "utf-8");
const GATEWAY_SOURCE = fs.readFileSync(path.join(ROOT, "gateway-supervisor.js"), "utf-8");
const PRELOAD_SOURCE = fs.readFileSync(path.join(ROOT, "preload.js"), "utf-8");
const IPC_REGISTRAR_SOURCE = fs.readFileSync(path.join(ROOT, "ipc-registrar.js"), "utf-8");

/** Every shipped .js under electron/ (tests and deps excluded). */
function sourceFiles() {
  const out = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === "node_modules" || entry.name === "test" || entry.name === "build") continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".js")) out.push(full);
    }
  };
  walk(ROOT);
  return out;
}

function rel(file) {
  return path.relative(ROOT, file).split(path.sep).join("/");
}

test("packaging allowlist covers every relatively-required module", () => {
  const listed = new Set(require(path.join(ROOT, "package.json")).build.files);
  const required = new Set();

  for (const file of sourceFiles()) {
    const src = fs.readFileSync(file, "utf-8");
    for (const m of src.matchAll(/require\(\s*"(\.[^"]+)"\s*\)/g)) {
      let target = path.resolve(path.dirname(file), m[1]);
      if (!target.endsWith(".js")) target += ".js";
      if (fs.existsSync(target)) required.add(rel(target));
    }
  }

  const missing = [...required].filter((f) => !listed.has(f)).sort();
  assert.deepStrictEqual(
    missing,
    [],
    `require()d but NOT in package.json build.files — these work from source and ` +
      `are absent from the packaged app: ${missing.join(", ")}`,
  );
});

test("packaging allowlist has no stale entries", () => {
  const listed = require(path.join(ROOT, "package.json")).build.files;
  const stale = listed.filter((f) => !fs.existsSync(path.join(ROOT, f)));
  assert.deepStrictEqual(
    stale,
    [],
    `listed in build.files but does not exist (left behind by a rename?): ${stale.join(", ")}`,
  );
});

test("macOS New Window opens the blank-session route on the existing gateway", () => {
  assert.match(
    WINDOW_SOURCE,
    /createConnectionWindow\(backendUrl, port, "\/chat\?new=1"\)/,
  );
  assert.match(
    WINDOW_SOURCE,
    /connectWindow\(win, backendUrl, \{ initialPath: "\/chat\?new=1" \}\)/,
  );
});

test("only the primary local window owns the gateway liveness monitor", () => {
  const guardedStarts = GATEWAY_SOURCE.match(
    /if \(targetBackendUrl === BACKEND_URL && window === mainWindow\(\)\) \{\s*startLivenessMonitor\(window\);\s*\}/g,
  ) || [];
  assert.strictEqual(guardedStarts.length, 2, "authenticated and unauthenticated handoffs stay guarded");
  const allStarts = GATEWAY_SOURCE.match(/startLivenessMonitor\(window\);/g) || [];
  assert.strictEqual(
    allStarts.length,
    guardedStarts.length,
    "every liveness start must remain inside the primary-local-window guard",
  );
});

// ── IPC channel contract ──────────────────────────────────────────────────

const PRELOADS = ["preload.js", "mochi/preload.js", "mochi/pet-preload.js"].filter((f) =>
  fs.existsSync(path.join(ROOT, f)),
);

function readAll(files) {
  return files.map((f) => fs.readFileSync(path.join(ROOT, f), "utf-8")).join("\n");
}

/** Only Mochi's namespace is asserted; the host's channels predate this guard. */
const NS = /^mochi-/;

function channels(src, pattern) {
  const found = new Set();
  for (const m of src.matchAll(pattern)) {
    if (NS.test(m[1])) found.add(m[1]);
  }
  return found;
}

test("the WSL preload invoke is registered by the IPC owner", () => {
  assert.match(
    PRELOAD_SOURCE,
    /detect:\s*\(\)\s*=>\s*ipcRenderer\.invoke\("wsl:detect"\)/,
    "window.wslAPI.detect must invoke the read-only wsl:detect channel",
  );
  assert.match(
    IPC_REGISTRAR_SOURCE,
    /ipcMain\.handle\("wsl:detect",\s*async\s*\(event\)\s*=>/,
    "ipc-registrar must own the handler exposed by preload.js",
  );
});

test("every mochi channel main SENDS is received by a preload", () => {
  const mainSrc = readAll(sourceFiles().map(rel).filter((f) => !PRELOADS.includes(f)));
  const preloadSrc = readAll(PRELOADS);

  const sent = channels(mainSrc, /(?:webContents\.send|broadcastToOverlays)\(\s*"([^"]+)"/g);
  const received = channels(preloadSrc, /ipcRenderer\.(?:on|once)\(\s*"([^"]+)"/g);

  // Non-vacuity: an empty set makes the comparison below trivially true, which
  // is the same silent-pass failure this guard exists to prevent.
  assert.ok(sent.size >= 5, `expected the main process to send channels, found ${sent.size}`);
  assert.ok(received.size >= 5, `expected preloads to receive channels, found ${received.size}`);

  const orphans = [...sent].filter((c) => !received.has(c)).sort();
  assert.deepStrictEqual(
    orphans,
    [],
    `sent by the main process but no preload listens — a silent no-op: ${orphans.join(", ")}`,
  );
});

test("every mochi channel a preload SENDS has a main-process handler", () => {
  const mainSrc = readAll(sourceFiles().map(rel).filter((f) => !PRELOADS.includes(f)));
  const preloadSrc = readAll(PRELOADS);

  const sent = channels(preloadSrc, /ipcRenderer\.(?:send|invoke)\(\s*"([^"]+)"/g);
  const handled = channels(mainSrc, /ipcMain\.(?:on|once|handle)\(\s*"([^"]+)"/g);

  assert.ok(sent.size >= 15, `expected preloads to send channels, found ${sent.size}`);
  assert.ok(handled.size >= 15, `expected ipcMain handlers, found ${handled.size}`);

  const orphans = [...sent].filter((c) => !handled.has(c)).sort();
  assert.deepStrictEqual(
    orphans,
    [],
    `exposed to the renderer but no ipcMain handler — the call silently does ` +
      `nothing: ${orphans.join(", ")}`,
  );
});

test("the movement channels the vendored hooks depend on are wired", () => {
  // Named explicitly rather than derived: these are the ones whose absence made
  // cross-display drag impossible while single-display drag still looked fine,
  // so a generic count would not have caught the regression.
  const preloadSrc = readAll(PRELOADS);
  for (const key of [
    "dragStart",
    "dragEnd",
    "dragMouseup",
    "onDragUpdate",
    "onDragEnded",
    "onDragListenMouseup",
    "onSetActive",
    "onDisplaysInfo",
    "updateHitbox",
    "savePosition",
  ]) {
    assert.ok(
      new RegExp(`\\b${key}\\s*:`).test(preloadSrc),
      `pet-preload must expose ${key} — the vendored movement hooks call it, and ` +
        `an absent key is swallowed by their optional chaining`,
    );
  }
});
