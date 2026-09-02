const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// cage-trace.js is exercised directly by cage-trace.test.js, but a unit test of a
// module cannot see whether the lifecycle owners ever CALL it. Both `stopForCrash` and
// `stopForQuit` are write-or-lose: `contentTracing.stopRecording` is the only
// thing that puts a trace on disk, and the capture window's timer is unref'd, so
// a capture whose lifecycle hook is not wired is silently discarded with no error
// anywhere. `stopForQuit` shipped exported and unit-tested but wired to nothing,
// which is the exact failure these assertions exist to prevent recurring.
const MAIN = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
const WINDOW_LIFECYCLE = fs.readFileSync(
  path.join(__dirname, "..", "window-lifecycle.js"),
  "utf8",
);

/** The body of the `<event>` handler, whether it is registered on `app` or on a
 *  window's `webContents`, so a call in some other handler cannot satisfy an
 *  assertion about this one. */
function handlerBody(source, owner, event) {
  const start = source.search(new RegExp(`\\.on\\("${event}"`));
  assert.notEqual(start, -1, `${owner} must register a "${event}" handler`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(open, i + 1);
    }
  }
  throw new Error(`unbalanced braces in the ${event} handler`);
}

describe("cage trace lifecycle wiring", () => {
  it("lands an in-flight capture when the renderer dies", () => {
    const body = handlerBody(WINDOW_LIFECYCLE, "window-lifecycle.js", "render-process-gone");
    assert.match(body, /cageTrace\.stopForCrash\(\)/);
  });

  it("lands an in-flight capture on quit", () => {
    // The regression: an armed capture active at quit is lost because nothing
    // calls the only writer before teardown.
    const body = handlerBody(MAIN, "main.js", "before-quit");
    assert.match(body, /windows\.diagnostics\.stopForQuit\(\)/);
    assert.match(
      WINDOW_LIFECYCLE,
      /stopForQuit:\s*\(\)\s*=>\s*cageTrace\.stopForQuit\(\)/,
      "the quit façade must land the trace owned by window-lifecycle",
    );
  });

  it("has no cage-trace stop entry point without a lifecycle caller", () => {
    // Guards the general shape rather than the two instances above: every stop
    // method the module exports must be reachable from a lifecycle owner, so adding a third
    // one and forgetting to wire it fails here instead of in production silence.
    const traceSrc = fs.readFileSync(path.join(__dirname, "..", "cage-trace.js"), "utf8");
    const exported = [...traceSrc.matchAll(/async (stopFor\w+)\s*\(/g)].map((m) => m[1]);
    assert.ok(exported.length >= 2, `expected the stopFor* family, got ${exported.join(", ")}`);
    for (const name of exported) {
      assert.match(
        WINDOW_LIFECYCLE,
        new RegExp(`cageTrace\\.${name}\\(\\)`),
        `${name} has no caller in window-lifecycle.js`,
      );
    }
  });
});
