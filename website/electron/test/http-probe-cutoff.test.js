"use strict";

// A gateway probe that reads a JSON body must settle when the response is cut
// off mid-body (the gateway stopped or restarted while answering). Node then
// emits `error` ("aborted") and `close` on the response, never `end`, and no
// `error` or `timeout` on the request, so a probe that waits only for `end`
// never settles and whatever awaits it stalls for good.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { EventEmitter } = require("node:events");

const { createSuccessorHandoff } = require("../runtime/gateway/successor-handoff");

/** An http stand-in whose response sends part of a body, then is cut off. */
function cutOffHttp() {
  const get = (_url, _opts, cb) => {
    const req = new EventEmitter();
    req.destroy = () => {};
    setImmediate(() => {
      const res = new EventEmitter();
      res.statusCode = 200;
      res.resume = () => {};
      cb(res);
      res.emit("data", "{\"ok\":");
      res.emit("error", new Error("aborted"));
      res.emit("close");
    });
    return req;
  };
  return { get };
}

/** Resolve to the probe's value, or to `pending` once the fake has finished. */
async function outcome(promise) {
  let result = "pending";
  promise.then((value) => { result = { value }; });
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
  return result;
}

test("fetchHealthInfo settles null when the body is cut off", async () => {
  const handoff = createSuccessorHandoff({ http: cutOffHttp(), backendUrl: "http://localhost:5476" });
  assert.deepEqual(await outcome(handoff.fetchHealthInfo()), { value: null });
});

test("fetchGatewayReadiness settles unknown when the body is cut off", async () => {
  const handoff = createSuccessorHandoff({ http: cutOffHttp(), backendUrl: "http://localhost:5476" });
  assert.deepEqual(await outcome(handoff.fetchGatewayReadiness()), { value: "unknown" });
});

test("the companion's enabled probe settles unknown when the body is cut off", async () => {
  const index = path.join(__dirname, "..", "crew-companion", "index.js");
  const originalLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "node:http" || request === "http") return cutOffHttp();
    return originalLoad(request, parent, isMain);
  };
  let companion;
  try {
    delete require.cache[require.resolve(index)];
    companion = require(index);
  } finally {
    Module._load = originalLoad;
    delete require.cache[require.resolve(index)];
  }
  assert.deepEqual(await outcome(companion.probeEnabled({ value: "tok" })), { value: "unknown" });
});

// Every shell module that reads a gateway response body to `end` also settles
// on the response's `error`, so a new probe cannot reintroduce the stall.
test("every response read to end also settles on the response's error", () => {
  const files = [
    "crew-companion/index.js",
    "local-token.js",
    "mochi/index.js",
    "runtime/gateway/successor-handoff.js",
  ];
  for (const file of files) {
    const source = fs.readFileSync(path.join(__dirname, "..", file), "utf8");
    const ends = source.match(/\bres\.on\("end"/g) || [];
    const errors = source.match(/\bres\.on\("error"/g) || [];
    assert.ok(ends.length > 0, `${file} still reads a response to end`);
    assert.equal(errors.length, ends.length, `${file}: ${ends.length} end listener(s), ${errors.length} error listener(s)`);
  }
});
