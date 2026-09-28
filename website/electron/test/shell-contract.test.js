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
  // Build-time inputs are staged by build-desktop.sh, not present in a checkout.
  const { BUILD_TIME_INPUTS: buildTimeInputs } = require("./build-time-inputs");
  const stale = listed.filter((f) => !buildTimeInputs.has(f) && !fs.existsSync(path.join(ROOT, f)));
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

// The three-gate local-dashboard check guards `wsl:detect`, both
// `crash-reports:*` channels and `pane:clear-http-cache`. It was briefly written out twice, and two
// hand-maintained spellings of a security check is one of them being tightened
// while the other is not. The port probe is gate 3 and appears in no other code
// path, so counting its call sites counts the copies.
test("the local-dashboard gate has exactly one spelling", () => {
  const probes = IPC_REGISTRAR_SOURCE.match(/probePrimaryPortOwner\(\)/g) || [];
  assert.strictEqual(
    probes.length,
    1,
    "gate 3 must be reached through the single assertLocalDashboard helper; "
      + `found ${probes.length} probe call sites, so the gate has been copied`,
  );
  for (const channel of ["wsl:detect", "crash-reports:get", "crash-reports:reveal", "pane:clear-http-cache"]) {
    assert.match(
      IPC_REGISTRAR_SOURCE,
      new RegExp(`assertLocalDashboard\\(event, "${channel}"\\)`),
      `${channel} must route through the shared local-dashboard gate`,
    );
  }
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

// --- Frameless window-drag band --------------------------------------------

// A 42px `-webkit-app-region: drag` strip at the top of the renderer, with a
// document-wide `no-drag` list exempting controls from it. Every part of it
// fails SILENTLY and only in a native window: a wrong height, a dropped
// exemption or a changed insertion point still renders perfectly, and a
// headless display resolves no app-region set at all, so CI can guard the
// source and nothing else. The trade this band makes, and the manual check for
// it, are in docs/build/desktop-app.md.

const TRANSCRIPT_SHELL = path.join(ROOT, "..", "src", "pages", "chat", "TranscriptScrollShell.tsx");

/** The band's injected stylesheet, verbatim, template interpolations intact. */
function dragBandCss() {
  const start = WINDOW_SOURCE.indexOf("#electron-drag-bar {");
  assert.ok(start > 0, "the drag band's CSS must still be injected from window-lifecycle.js");
  const end = WINDOW_SOURCE.indexOf("`);", start);
  assert.ok(end > start, "the drag band's insertCSS template must terminate");
  return WINDOW_SOURCE.slice(start, end);
}

/** The same stylesheet as selector -> body, with template interpolations
 *  flattened so every rule body is brace-free. The interpolated values are
 *  therefore invisible here and are asserted against dragBandCss() instead. */
function dragBandRules() {
  const css = dragBandCss().replace(/\$\{[^}]*\}/g, "0");
  const rules = new Map();
  for (const rule of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    rules.set(rule[1].trim().replace(/\s+/g, " "), rule[2]);
  }
  // Non-vacuity: every assertion below is a lookup in this map, so an empty or
  // truncated parse would satisfy them for the wrong reason.
  assert.ok(rules.size >= 4, `expected the band's four rules, parsed ${rules.size}`);
  return rules;
}

test("the drag band keeps its height, its caption inset, and its focus-mode collapse", () => {
  const rules = dragBandRules();
  for (const [selector, height] of [
    ["#electron-drag-bar", /height:\s*42px/],
    ["body.mc-focus-mode #electron-drag-bar", /height:\s*0\s*;/],
    ["body.mc-focus-mode.mc-focus-chrome #electron-drag-bar", /height:\s*42px/],
  ]) {
    assert.ok(
      rules.has(selector),
      `the band's \`${selector}\` rule is gone: these three carry its height, and `
        + `the band has to exist exactly when a header does`,
    );
    assert.match(
      rules.get(selector),
      height,
      `\`${selector}\` must keep its height. The docked band and the peeked `
        + `focus-mode band are both the header's own 42px, and focus mode `
        + `without chrome is 0`,
    );
  }
  assert.match(
    rules.get("#electron-drag-bar"),
    /-webkit-app-region:\s*drag/,
    "the band is the window's drag surface on every frameless platform",
  );
  // The right inset is the rest of the band's geometry, and it is the only thing
  // keeping a drag rectangle off the platform caption controls: over Close, a
  // drag rectangle moves the window instead of closing it. It is a template
  // interpolation, so it is asserted on the raw stylesheet, which is also why
  // the rule-body assertions above cannot see it.
  assert.match(
    dragBandCss(),
    /right:\s*\$\{IS_WIN \? "138px" : LINUX_FRAMELESS \? "108px" : "0"\}/,
    "the band must keep its per-platform right inset: full width on macOS, clear "
      + "of the Windows caption overlay at 138px, and clear of the injected Linux "
      + "window controls at 108px",
  );
});

test("the band's no-drag list still exempts the transcript scroller", () => {
  const rules = dragBandRules();
  const exemption = [...rules].find(([, body]) => /-webkit-app-region:\s*no-drag/.test(body));
  assert.ok(
    exemption,
    "the band's injected CSS must keep a no-drag rule: without one, every control "
      + "under the top 42px stops taking the pointer",
  );
  const exempt = exemption[0].split(",").map((s) => s.trim());
  // Enumerated rather than counted. Each entry is a control class that sits in
  // the top band somewhere in the app, and app-region is resolved
  // geometrically, so the list stays document-wide: a control that merely
  // OVERLAPS the band needs the exemption, whatever its DOM position.
  for (const selector of ["a", "button", "input", "select", "textarea", '[role="button"]', "[tabindex]", "iframe"]) {
    assert.ok(
      exempt.includes(selector),
      `\`${selector}\` dropped from the band's no-drag list, so anything matching `
        + `it goes dead under the top 42px. Found: ${exempt.join(", ")}`,
    );
  }
  // The other half of one invariant: `[tabindex]` is what subtracts the
  // conversation column from the band, and it can only do that while the
  // scroller carries the attribute. The two move together or not at all.
  assert.ok(
    fs.existsSync(TRANSCRIPT_SHELL),
    "the transcript scroller is not at src/pages/chat/TranscriptScrollShell.tsx; "
      + "re-point this guard at its current home instead of dropping it",
  );
  assert.match(
    fs.readFileSync(TRANSCRIPT_SHELL, "utf-8"),
    /tabIndex=\{-1\}/,
    "the transcript scroller must keep tabIndex={-1}: it is what matches the band's "
      + "[tabindex] exemption, which is what leaves conversation text selectable "
      + "under a peeked header",
  );
});

test("the drag band is inserted once, at the front of the body", () => {
  assert.match(
    WINDOW_SOURCE,
    /if \(!document\.getElementById\('electron-drag-bar'\)\)/,
    "the injection must stay idempotent: did-finish-load fires again on every "
      + "reload and navigation, and a second bar is a second drag rect",
  );
  assert.match(
    WINDOW_SOURCE,
    /document\.body\.prepend\(bar\)/,
    "the bar belongs at the FRONT of the body. The draggable region accumulates "
      + "in the order the renderer reports rectangles, so from there every "
      + "no-drag element in the app subtracts from the band",
  );
  assert.doesNotMatch(
    WINDOW_SOURCE,
    /document\.body\.append(?:Child)?\(bar\)/,
    "appending the bar instead would re-add the whole 42px strip on top of every "
      + "exemption, taking the pointer away from controls and text alike",
  );
});
