// On Windows the local mint proves the listener with netstat, and refuses when
// it cannot. One failed or slow netstat run must not decide that: the probe is
// retried once, each attempt stays bounded, and a probe that never answers
// still refuses, with a log line that says how it failed.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { test } = require("node:test");
const assert = require("node:assert");

const { createPortHolders } = require("../runtime/gateway/port-holders");
const { createTokenSources } = require("../runtime/gateway/token-sources");

const OWN_PID = 1234;
const PORT = 5476;
const BACKEND_URL = `http://127.0.0.1:${PORT}`;
const LISTENING = `  TCP    127.0.0.1:${PORT}         0.0.0.0:0              LISTENING       ${OWN_PID}\r\n`;

function failed() {
  return Object.assign(new Error("Command failed: C:\\Windows\\System32\\netstat.exe -ano"), { code: 1 });
}

function timedOut() {
  return Object.assign(new Error("Command failed: C:\\Windows\\System32\\netstat.exe -ano"), {
    killed: true, signal: "SIGTERM", code: null,
  });
}

// netstat answers from a script: an Error entry fails that call, a string is
// its stdout, and the last entry repeats.
function windowsSources(script) {
  const netstatCalls = [];
  const lines = [];
  const secretRequests = [];
  const child = { pid: OWN_PID, exitCode: null };
  const execFile = (file, _args, options, callback) => {
    if (!String(file).toLowerCase().includes("netstat")) {
      callback(new Error(`unexpected command: ${file}`));
      return;
    }
    netstatCalls.push(options.timeout);
    const step = script[Math.min(netstatCalls.length - 1, script.length - 1)];
    if (step instanceof Error) callback(step, "");
    else callback(null, step, "");
  };
  const { mintPortPids } = createPortHolders({
    fs, os, path,
    execFile,
    processObj: { resourcesPath: "", env: {} },
    dirname: "/virtual/electron",
    isWindows: true,
    log: () => {},
    getSpawnedExecutablePaths: () => [],
    sleep: async () => {},
  });
  const built = createTokenSources({
    store: { get: () => undefined },
    port: PORT,
    backendUrl: BACKEND_URL,
    execFile,
    fs: {
      constants: { X_OK: 1 },
      existsSync() { return true; },
      readFileSync() { return "a-local-secret\n"; },
    },
    path,
    http: {
      get(url, options) {
        const request = new EventEmitter();
        request.destroy = () => {};
        if (options && options.headers && options.headers["X-Local-Secret"]) {
          secretRequests.push(String(url));
        }
        queueMicrotask(() => request.emit("error", new Error("connection refused")));
        return request;
      },
    },
    isWindows: true,
    log: (line) => lines.push(line),
    sendStatus: () => {},
    mintPortPids,
    getGatewayProcess: () => child,
    psPpid: async () => "",
    holdsHomeGatewayLock: async () => false,
  });
  return { ...built, netstatCalls, lines, secretRequests };
}

test("a first netstat run that fails or times out is retried, and our gateway is minted for", async () => {
  for (const [label, first] of [["failed", failed()], ["timed out", timedOut()]]) {
    const s = windowsSources([first, LISTENING]);
    await s.mintLocalToken();
    assert.ok(s.secretRequests.length > 0, `${label}: the secret goes to our own listener`);
    assert.deepStrictEqual(s.lines, [], `${label}: no refusal is logged`);
    assert.deepStrictEqual(s.netstatCalls, [5000, 5000], `${label}: two bounded attempts`);
    assert.strictEqual(await windowsSources([first, LISTENING]).ownsLocalListener(), true, label);
  }
});

test("a netstat that fails every attempt still refuses, names how, and logs a standing refusal once", async () => {
  const s = windowsSources([timedOut()]);
  assert.strictEqual(await s.mintLocalToken(), "");
  assert.strictEqual(await s.mintLocalToken(), "");
  assert.deepStrictEqual(s.secretRequests, [], "no secret leaves the machine");
  assert.deepStrictEqual(s.netstatCalls, [5000, 5000, 5000, 5000], "two bounded attempts per mint");
  assert.strictEqual(s.lines.length, 1);
  assert.match(s.lines[0], /its listener could not be read \(netstat failed 2 times on :5476; last: timed out after 5000ms\)/);
  assert.strictEqual(await windowsSources([failed()]).ownsLocalListener(), false);
});
