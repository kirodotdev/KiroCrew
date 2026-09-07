const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// crash-collector.js is exercised directly by crash-collector.test.js, but a unit
// test of the module cannot see WHERE main.js calls it, and here the call site's
// position is the whole behaviour. `armCrashCollector` writes the activation
// cutoff that the first scan uses to tell "this build produced it" from "this
// predates the feature"; `initNativeLogging` is what calls `crashReporter.start()`
// and so what makes Crashpad able to write a dump at all.
//
// Arm second and there is a window — short, but covering exactly the startup
// crashes this feature exists for — in which a dump lands with no cutoff on
// record. The next launch stamps a cutoff LATER than that dump's mtime, the first
// scan reads it as history, and it is marked seen without ever being surfaced. The
// crash is lost silently: no error, no log line, nothing to notice. A comment
// saying the order matters is not enough, because reordering the two calls looks
// harmless in review and the consequence never shows up in a unit test.
const MAIN = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");

// Comments stripped, because this file asserts on CODE and the code it guards is
// heavily commented — including a comment that names `crashReporter.start()` to
// explain the very ordering below, which a naive scan counts as a second call
// site. The `[^:]` guard keeps `http://` in a string literal from being read as
// the start of a line comment.
const CODE = MAIN
  .replace(/\/\*[\s\S]*?\*\//g, " ")
  .replace(/(^|[^:])\/\/.*$/gm, "$1");

/** Offset of a top-level call to `name(`, asserting it exists at all. */
function callOffset(source, name) {
  const at = source.search(new RegExp(`\\b${name}\\(\\{`));
  assert.notEqual(at, -1, `main.js must call ${name}`);
  return at;
}

describe("crash arming order in main.js", () => {
  it("arms the collector before anything can start the crash reporter", () => {
    assert.ok(
      callOffset(CODE, "armCrashCollector") < callOffset(CODE, "initNativeLogging"),
      "armCrashCollector must precede initNativeLogging, which starts Crashpad",
    );
  });

  it("starts the crash reporter only through the call that comes second", () => {
    // Guards the shape rather than the one instance above: the assertion is only
    // meaningful while `initNativeLogging` remains the sole route to
    // `crashReporter.start`. A second, earlier caller would reopen the same window
    // with the ordering above still satisfied.
    const starts = [...CODE.matchAll(/crashReporter\.start\(/g)];
    assert.equal(starts.length, 1, "expected exactly one crashReporter.start call site");
    const wiring = CODE.slice(
      callOffset(CODE, "initNativeLogging"),
      CODE.indexOf("});", callOffset(CODE, "initNativeLogging")),
    );
    assert.match(
      wiring,
      /startCrashReporter:\s*\(options\)\s*=>\s*crashReporter\.start\(options\)/,
      "the only crashReporter.start must be the one initNativeLogging is handed",
    );
  });

  it("arms inside the single-instance lock winner, not before it", () => {
    // A rejected second instance exits immediately; stamping a cutoff from it
    // would move the primary process's baseline for a run that collects nothing.
    const lock = CODE.search(/if\s*\(!app\.requestSingleInstanceLock\(\)\)/);
    assert.notEqual(lock, -1, "main.js must take the single-instance lock");
    assert.ok(lock < callOffset(CODE, "armCrashCollector"));
  });
});
