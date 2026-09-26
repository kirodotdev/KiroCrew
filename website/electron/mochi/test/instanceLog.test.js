const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");

function instanceLogHarness() {
  const start = source.search(/^(?:let lastMochiInstanceLog|const MOCHI_INSTANCE_LOG_REPEAT_MS)/m);
  const end = source.indexOf("/**", start);
  assert.ok(start !== -1 && end > start, "instance logger must remain in index.js");
  const messages = [];
  let now = 0;
  const mochiInstanceLog = vm.runInNewContext(
    `${source.slice(start, end)}\nmochiInstanceLog`,
    { glog: (message) => messages.push(message), Date: { now: () => now } },
  );
  return { mochiInstanceLog, messages, advance: (ms) => { now += ms; } };
}

test("alternating unknown and resolved instance outcomes do not flood the log", () => {
  const { mochiInstanceLog, messages, advance } = instanceLogHarness();
  for (let i = 0; i < 12; i += 1) {
    mochiInstanceLog('petInstance "remote" did not answer — leaving Mochi where it is');
    mochiInstanceLog('petInstance "remote" resolved to port 7778');
    advance(5_000);
  }
  assert.deepEqual(messages, [
    'mochi instance: petInstance "remote" did not answer — leaving Mochi where it is',
    'mochi instance: petInstance "remote" resolved to port 7778',
  ]);
});

test("a confirmed instance target change logs immediately", () => {
  const { mochiInstanceLog, messages } = instanceLogHarness();
  mochiInstanceLog('petInstance "remote" resolved to port 7778');
  mochiInstanceLog('petInstance "remote" resolved to port 7779');
  mochiInstanceLog('petInstance "remote" resolved to port 7778');
  assert.equal(messages.length, 3);
});

test("a continuing instance resolution failure logs again after one minute", () => {
  const { mochiInstanceLog, messages, advance } = instanceLogHarness();
  mochiInstanceLog('petInstance "remote" did not answer — leaving Mochi where it is');
  mochiInstanceLog('petInstance "remote" resolved to port 7778');
  advance(60_000);
  mochiInstanceLog('petInstance "remote" did not answer — leaving Mochi where it is');
  assert.equal(messages.length, 3);
});
