const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");

function probeHarness() {
  const start = source.search(/^(?:let lastMochiProbe|const MOCHI_PROBE_LOG_REPEAT_MS)/m);
  const end = source.indexOf("// Cached because", start);
  assert.ok(start !== -1 && end > start, "probe logger must remain in index.js");
  const messages = [];
  let now = 0;
  const probeLog = vm.runInNewContext(
    `${source.slice(start, end)}\nprobeLog`,
    { console: { log: (...parts) => messages.push(parts.join(" ")) }, Date: { now: () => now } },
  );
  return { probeLog, messages, advance: (ms) => { now += ms; } };
}

test("alternating 403 and disabled outcomes do not flood the pet log", () => {
  const { probeLog, messages } = probeHarness();
  for (let i = 0; i < 12; i += 1) {
    probeLog("/api/apps returned HTTP 403");
    probeLog("mochi installed but disabled");
  }
  assert.deepEqual(messages, [
    "Mochi pet probe: /api/apps returned HTTP 403",
    "Mochi pet probe: mochi installed but disabled",
  ]);
});

test("a real enabled-state change is still logged", () => {
  const { probeLog, messages } = probeHarness();
  probeLog("mochi installed but disabled");
  probeLog("mochi enabled — opening pet");
  probeLog("mochi installed but disabled");
  assert.equal(messages.length, 3);
});

test("a continuing error is logged again after the repeat interval", () => {
  const { probeLog, messages, advance } = probeHarness();
  probeLog("/api/apps returned HTTP 403");
  probeLog("mochi installed but disabled");
  advance(60_000);
  probeLog("/api/apps returned HTTP 403");
  assert.equal(messages.length, 3);
});
