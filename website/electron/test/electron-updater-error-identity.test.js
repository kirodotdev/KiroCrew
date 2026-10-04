"use strict";

// Tripwire for the one library premise the updater's error attribution rests on.
//
// `runtime/update/feed-lane.js` tells a check's own failure from the installer's by comparing the
// error OBJECT (`err === abandonedError`, `err === inFlightCheckError`). That only
// works because electron-updater hands the SAME object to the `error` event and to
// the rejection of `checkForUpdates()`. Nothing in the library documents that, and
// every unit test in this repo mocks the updater, so a version bump that rewrapped
// the error would silently turn a check's feed failure back into a reported install
// failure — firing the host's gateway recovery mid-dispatch, which is the hazard the
// comparison exists to prevent.
//
// So assert it against the REAL installed library, at the source level. An upgrade
// that changes the shape fails here, loudly, with a pointer to what to re-read —
// rather than in production on a user's machine.

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

/** Resolve the installed library's AppUpdater source, or null when absent. */
function readAppUpdaterSource() {
  let entry;
  try {
    entry = require.resolve("electron-updater");
  } catch {
    return null;
  }
  const candidate = path.join(path.dirname(entry), "AppUpdater.js");
  return fs.existsSync(candidate) ? fs.readFileSync(candidate, "utf8") : null;
}

/**
 * The source of the class method `name() { ... }`: from its declaration to the
 * next declaration at the same indentation. Bounded by layout rather than by
 * counting braces, so a brace inside a string or regex literal in the method
 * cannot stretch the slice into a neighbouring method and let THAT method's
 * emit/throw pairing satisfy the canary. Null when the method is not found.
 */
function methodBody(src, name) {
  const decl = new RegExp(`\\n( +)${name}\\(\\)\\s*\\{`).exec(src);
  if (!decl) return null;
  const indent = decl[1];
  const next = new RegExp(`\\n${indent}(?:async\\s+)?[A-Za-z_$][\\w$]*\\s*\\([^)]*\\)\\s*\\{`, "g");
  next.lastIndex = decl.index + decl[0].length;
  const end = next.exec(src);
  return src.slice(decl.index, end ? end.index : src.length);
}

test("electron-updater emits and rejects checkForUpdates with the SAME error object", (t) => {
  const src = readAppUpdaterSource();
  if (src === null) {
    // Every CI job that runs this suite runs `npm ci` in website/electron first,
    // so a missing library there means the canary checked nothing: fail rather
    // than report a pass. Elsewhere (a pruned install) it is SKIPPED, visibly.
    assert.ok(!process.env.CI, "electron-updater is not installed, so the identity canary cannot run");
    t.skip("electron-updater not installed here; nothing to check");
    return;
  }

  // The shape under test, from `AppUpdater.checkForUpdates()`:
  //
  //     .catch((e) => {
  //         nullizePromise();
  //         this.emit("error", e, `Cannot check for updates: ...`);
  //         throw e;
  //     });
  //
  // One binding, emitted and then re-thrown. Matched forward from the emit rather
  // than by catch syntax, because the library uses a promise `.catch((e) => …)`
  // callback here and a plain `catch (e) { … }` elsewhere, and the invariant is the
  // pairing, not the enclosing form. The assertion is deliberately about that pairing
  // rather than either half alone: an emit without the re-throw, or a re-throw of a
  // different value, is exactly the regression this guards.
  // Scoped to the body of `checkForUpdates()` itself: that is the method feed-lane.js
  // awaits, so a pairing that survives only in some OTHER method must not satisfy
  // the canary while this one has been rewrapped.
  const body = methodBody(src, "checkForUpdates");
  assert.ok(body, "AppUpdater.checkForUpdates() not found -- re-read the library before trusting identity attribution");
  const emitSites = [...body.matchAll(/this\.emit\(\s*["']error["']\s*,\s*(\w+)\b/g)];
  const identityPreserving = emitSites.filter((m) => {
    const binding = m[1];
    const after = body.slice(m.index, m.index + 400);
    return new RegExp(`throw\\s+${binding}\\s*;`).test(after);
  });

  assert.ok(
    identityPreserving.length > 0,
    "electron-updater has no code path that emits `error` with the same object it then throws.\n" +
      "The updater's error attribution in website/electron/runtime/update/feed-lane.js compares error\n" +
      "objects by identity (abandonedError / inFlightCheckError). If this assertion fails\n" +
      "after a dependency bump, re-read AppUpdater's error paths in\n" +
      "node_modules/electron-updater/out/AppUpdater.js BEFORE shipping: a rewrapped error\n" +
      "makes a check's own failure report as an install failure, and the host's gateway\n" +
      "recovery then fires while a dispatch is still stopping the gateway.",
  );
});
