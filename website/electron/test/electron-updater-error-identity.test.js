"use strict";

// Tripwire for the one library premise the updater's error attribution rests on.
//
// `auto-update.js` tells a check's own failure from the installer's by comparing the
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

test("electron-updater emits and rejects checkForUpdates with the SAME error object", () => {
  const src = readAppUpdaterSource();
  if (src === null) {
    // A pruned install (production packaging) is not a failure of the invariant.
    // The assertion runs wherever the library is installed, which is every dev and
    // CI checkout that ran `npm ci` in website/electron.
    assert.ok(true, "electron-updater not installed here; nothing to check");
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
  const emitSites = [...src.matchAll(/this\.emit\(\s*["']error["']\s*,\s*(\w+)\b/g)];
  const identityPreserving = emitSites.filter((m) => {
    const binding = m[1];
    const after = src.slice(m.index, m.index + 400);
    return new RegExp(`throw\\s+${binding}\\s*;`).test(after);
  });

  assert.ok(
    identityPreserving.length > 0,
    "electron-updater has no code path that emits `error` with the same object it then throws.\n" +
      "The updater's error attribution in website/electron/auto-update.js compares error\n" +
      "objects by identity (abandonedError / inFlightCheckError). If this assertion fails\n" +
      "after a dependency bump, re-read AppUpdater's error paths in\n" +
      "node_modules/electron-updater/out/AppUpdater.js BEFORE shipping: a rewrapped error\n" +
      "makes a check's own failure report as an install failure, and the host's gateway\n" +
      "recovery then fires while a dispatch is still stopping the gateway.",
  );
});
