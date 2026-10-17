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

describe("early boot guard in main.js", () => {
  // Everything between the top of the file and `app.whenReady()` runs
  // synchronously at module load, before Chromium is ready and before any
  // window, tray, or (for most of that span) crash reporter exists. An uncaught
  // throw in that span has no surface to land on: no window, no dialog, and
  // before the log sink is armed, no log line. early-boot-guard.js is the ONE
  // mechanism that covers the span; early-boot-guard.test.js proves what it
  // does. What a unit test cannot see is WHERE main.js installs and releases
  // it, and the position is the whole behaviour: installed after the first
  // guarded statement, the guard misses it; released before the post-ready
  // safety net exists, an exception between the two is fatal again.

  const whenReadyOffset = CODE.search(/app\.whenReady\(\)\.then\(/);
  const installOffset = callOffset(CODE, "installEarlyBootGuard");

  it("finds app.whenReady().then( at all", () => {
    assert.notEqual(whenReadyOffset, -1, "main.js must call app.whenReady().then(");
  });

  it("installs the guard before the first electron call of the boot span", () => {
    const seedOffset = CODE.search(/\bseedRenamedStore\(/);
    assert.notEqual(seedOffset, -1, "main.js must call seedRenamedStore");
    assert.ok(installOffset < seedOffset, "the guard must precede seedRenamedStore(app.getPath(...))");
  });

  it("installs the guard before armCrashCollector and the single-instance lock", () => {
    const lock = CODE.search(/if\s*\(!app\.requestSingleInstanceLock\(\)\)/);
    assert.ok(installOffset < lock, "the guard must precede the single-instance lock");
    assert.ok(installOffset < callOffset(CODE, "armCrashCollector"), "the guard must precede armCrashCollector");
  });

  it("hands the guard app, dialog, glog and the log path", () => {
    const wiring = CODE.slice(installOffset, CODE.indexOf("});", installOffset));
    for (const dep of [/\bapp,/, /\bdialog,/, /\bglog,/, /logPath:\s*gatewayLogPath/]) {
      assert.match(wiring, dep, `installEarlyBootGuard must receive ${dep}`);
    }
  });

  it("installs the guard exactly once and keeps its release handle", () => {
    const installs = [...CODE.matchAll(/installEarlyBootGuard\(/g)];
    assert.equal(installs.length, 1, "expected exactly one installEarlyBootGuard call");
    assert.match(
      CODE,
      /const releaseEarlyBootGuard = installEarlyBootGuard\(\{/,
      "the release function must be kept for the ready handler",
    );
  });

  it("releases the guard as the first statement of the ready handler", () => {
    const releaseOffset = CODE.indexOf("releaseEarlyBootGuard();");
    assert.notEqual(releaseOffset, -1, "main.js must release the guard");
    assert.ok(whenReadyOffset < releaseOffset, "the release must sit inside app.whenReady().then(");
    const between = CODE.slice(CODE.indexOf("{", whenReadyOffset) + 1, releaseOffset);
    assert.equal(between.trim(), "", "nothing may run in the ready handler before the release");
  });

  it("releases only after the keep-alive safety net is registered", () => {
    // The post-ready net keeps the process alive so recovery can run; the
    // guard exits. Releasing before the net is registered would leave a window
    // with neither.
    const net = CODE.search(/process\.on\("uncaughtException"/);
    assert.notEqual(net, -1, "main.js must register the post-ready uncaughtException net");
    assert.ok(net < CODE.indexOf("releaseEarlyBootGuard();"));
  });

  it("keeps the boot sequence itself unwrapped: one mechanism, no per-block catches", () => {
    // The guard is a process-level listener, so the statements it covers stay
    // as module-scope declarations. A `try` around them would demote every
    // `const` inside to block scope.
    assert.match(CODE, /^const store = new Store\(/m, "store must be a module-scope const");
    assert.match(CODE, /^const KIROCREW_HOME = resolveHome\(\);/m);
    assert.match(CODE, /^const PORT = resolvePort\(\);/m);
    assert.match(CODE, /^const BACKEND_URL = /m);
    assert.doesNotMatch(CODE, /failEarlyBoot\(/, "main.js delegates reporting to the guard module");
  });

  it("keeps the boot calls present and in order", () => {
    const seedOffset = CODE.search(/\bseedRenamedStore\(/);
    const order = [
      installOffset,
      seedOffset,
      callOffset(CODE, "armCrashCollector"),
      callOffset(CODE, "initNativeLogging"),
      callOffset(CODE, "initGpuPolicy"),
    ];
    for (let i = 1; i < order.length; i += 1) {
      assert.ok(order[i - 1] < order[i], `call at index ${i - 1} must precede call at index ${i}`);
    }
    assert.ok(order[order.length - 1] < whenReadyOffset);
  });
});
