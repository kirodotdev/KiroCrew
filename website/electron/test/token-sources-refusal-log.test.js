// A local-mint refusal that holds is logged once, not on every Crew Companion
// tick. The refusal itself is unchanged: every call still answers "" and no
// secret is read or sent.
const { test } = require("node:test");
const assert = require("node:assert");
const path = require("path");
const { createTokenSources } = require("../runtime/gateway/token-sources");

const PORT = 5476;
const BACKEND_URL = `http://127.0.0.1:${PORT}`;

function sources({ remoteHost, getGatewayProcess = () => null }) {
  const lines = [];
  const secretReads = [];
  const store = {
    get(key) {
      if (key === "remoteHosts" && remoteHost.value) {
        return { [String(PORT)]: { host: remoteHost.value } };
      }
      return undefined;
    },
  };
  const built = createTokenSources({
    store,
    port: PORT,
    backendUrl: BACKEND_URL,
    execFile(file, _args, _options, callback) {
      callback(new Error(`unexpected command: ${file}`));
    },
    fs: {
      constants: { X_OK: 1 },
      existsSync() { return true; },
      readFileSync(file) {
        secretReads.push(String(file));
        return "a-local-secret\n";
      },
    },
    path,
    http: {
      get() {
        throw new Error("no request may be made for a refused mint");
      },
    },
    isWindows: false,
    log: (line) => lines.push(line),
    sendStatus: () => {},
    snapshotGatewayPortPids: async () => null,
    getGatewayProcess,
    psPpid: async () => "",
    holdsHomeGatewayLock: async () => false,
  });
  return { ...built, lines, secretReads };
}

test("a standing refusal is logged once across repeated ticks and still refuses every time", async () => {
  const remoteHost = { value: "dev-desktop" };
  const s = sources({ remoteHost });
  for (let tick = 0; tick < 5; tick += 1) {
    assert.strictEqual(await s.mintLocalToken(), "");
  }
  assert.strictEqual(s.lines.length, 1);
  assert.match(s.lines[0], /a remote crew is configured there/);
  assert.deepStrictEqual(s.secretReads, []);
});

test("a changed refusal reason is logged again", async () => {
  const remoteHost = { value: "dev-desktop" };
  const s = sources({ remoteHost });
  assert.strictEqual(await s.mintLocalToken(), "");
  assert.strictEqual(await s.mintLocalToken(), "");
  remoteHost.value = "";
  assert.strictEqual(await s.mintLocalToken(), "");
  assert.strictEqual(await s.mintLocalToken(), "");
  remoteHost.value = "dev-desktop";
  assert.strictEqual(await s.mintLocalToken(), "");
  assert.strictEqual(s.lines.length, 3);
  assert.match(s.lines[0], /a remote crew is configured there/);
  assert.match(s.lines[1], /no live gateway of this process/);
  assert.match(s.lines[2], /a remote crew is configured there/);
  assert.deepStrictEqual(s.secretReads, []);
});
