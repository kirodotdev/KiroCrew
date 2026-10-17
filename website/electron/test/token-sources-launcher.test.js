// The local mint must accept a gateway that a launcher runs one level down,
// and only that.
//
// The desktop's own child can be a launcher (a package manager's shim, or an
// entry point that execs into a supervisor) that runs the real gateway as its
// child. The kernel then names that child as the port's listener, never the
// pid this process spawned, and an exact-pid rule refused every mint for the
// life of the shell. Being a direct child is not enough on its own: in the
// ordinary case our child IS the gateway, and its direct children are the
// agent processes and tunnels it starts, so the listener must also hold this
// home's gateway.lock.
const { test } = require("node:test");
const assert = require("node:assert");
const path = require("path");
const { EventEmitter } = require("events");
const { createTokenSources } = require("../runtime/gateway/token-sources");

const OWN_PID = 1234;
const PORT = 5476;
const BACKEND_URL = `http://127.0.0.1:${PORT}`;

// `ppids` maps a pid to the parent pid `ps -o ppid=` reports for it, handed
// back as the raw padded stdout; a pid with no entry yields "", the way ps does
// for a pid that no longer exists. `lockHolders` lists the pids the home's
// gateway.lock proves (named by the file and holding it open).
function sources({
  holders,
  ppids = {},
  lockHolders = [],
  child,
  isWindows = false,
  onPs = () => {},
}) {
  const secretRequests = [];
  const psCalls = [];
  const lockCalls = [];
  const gateway = child || { pid: OWN_PID, exitCode: null };
  const built = createTokenSources({
    store: { get: () => undefined },
    port: PORT,
    backendUrl: BACKEND_URL,
    execFile(file, _args, _options, callback) {
      callback(new Error(`unexpected command: ${file}`));
    },
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
    isWindows,
    log: () => {},
    sendStatus: () => {},
    snapshotGatewayPortPids: async () => holders,
    getGatewayProcess: () => gateway,
    async psPpid(pid) {
      psCalls.push(pid);
      onPs(pid);
      return pid in ppids ? `  ${ppids[pid]}\n` : "";
    },
    async holdsHomeGatewayLock(pid) {
      lockCalls.push(pid);
      return lockHolders.includes(pid);
    },
  });
  return { ...built, secretRequests, psCalls, lockCalls, gateway };
}

test("a direct child of our launcher child that holds the home's gateway.lock receives the secret", async () => {
  const { mintLocalToken, secretRequests, psCalls, lockCalls } = sources({
    holders: [OWN_PID + 1],
    ppids: { [OWN_PID + 1]: OWN_PID },
    lockHolders: [OWN_PID + 1],
  });
  await mintLocalToken();
  assert.ok(secretRequests.length > 0, "the launcher's gateway is ours");
  assert.deepStrictEqual(psCalls, [OWN_PID + 1]);
  assert.deepStrictEqual(lockCalls, [OWN_PID + 1]);
});

test("a direct child of our child that does not hold the lock is refused", async () => {
  // Our child is the gateway here, and the listener is a subprocess it started
  // (an agent process, a tunnel). One level down proves nothing without the lock.
  const { mintLocalToken, secretRequests, psCalls, lockCalls } = sources({
    holders: [OWN_PID + 1],
    ppids: { [OWN_PID + 1]: OWN_PID },
  });
  assert.strictEqual(await mintLocalToken(), "");
  assert.deepStrictEqual(secretRequests, [], "no secret leaves the machine");
  assert.deepStrictEqual(psCalls, [OWN_PID + 1]);
  assert.deepStrictEqual(lockCalls, [OWN_PID + 1], "the lock was asked and said no");
});

test("our own child as listener mints without asking ps or the lock", async () => {
  const { mintLocalToken, secretRequests, psCalls, lockCalls } = sources({ holders: [OWN_PID] });
  await mintLocalToken();
  assert.ok(secretRequests.length > 0);
  assert.deepStrictEqual(psCalls, [], "the exact-pid case needs no parent lookup");
  assert.deepStrictEqual(lockCalls, [], "nor a lock probe");
});

test("a grandchild of our child is not accepted as the listener, even holding the lock", async () => {
  // Only one level is accepted; the lock is asked only for a direct child.
  const grandchild = OWN_PID + 2;
  const { mintLocalToken, secretRequests, lockCalls } = sources({
    holders: [grandchild],
    ppids: { [grandchild]: OWN_PID + 1, [OWN_PID + 1]: OWN_PID },
    lockHolders: [grandchild],
  });
  assert.strictEqual(await mintLocalToken(), "");
  assert.deepStrictEqual(secretRequests, []);
  assert.deepStrictEqual(lockCalls, [], "the lock is never asked for a non-child");
});

test("a listener with another parent, or an unreadable parent, is refused", async () => {
  for (const [label, ppids] of [
    ["another parent", { 5000: 1 }],
    ["ps cannot read it", {}],
  ]) {
    const { mintLocalToken, secretRequests, lockCalls } = sources({
      holders: [5000],
      ppids,
      lockHolders: [5000],
    });
    assert.strictEqual(await mintLocalToken(), "", label);
    assert.deepStrictEqual(secretRequests, [], `${label}: no secret leaves the machine`);
    assert.deepStrictEqual(lockCalls, [], `${label}: the lock is never asked for a non-child`);
  }
});

test("Windows keeps the exact-pid rule", async () => {
  const { mintLocalToken, secretRequests, psCalls, lockCalls } = sources({
    holders: [OWN_PID + 1],
    ppids: { [OWN_PID + 1]: OWN_PID },
    lockHolders: [OWN_PID + 1],
    isWindows: true,
  });
  assert.strictEqual(await mintLocalToken(), "");
  assert.deepStrictEqual(secretRequests, []);
  assert.deepStrictEqual(psCalls, []);
  assert.deepStrictEqual(lockCalls, []);
});

test("a child that exits during the parent lookup gets nothing", async () => {
  // Once our child exits, its children re-parent and its pid may be reused, so
  // a parent match read while it was dying cannot authorise the send.
  const child = { pid: OWN_PID, exitCode: null };
  const { mintLocalToken, secretRequests } = sources({
    holders: [OWN_PID + 1],
    ppids: { [OWN_PID + 1]: OWN_PID },
    lockHolders: [OWN_PID + 1],
    child,
    onPs: () => { child.exitCode = 0; },
  });
  assert.strictEqual(await mintLocalToken(), "");
  assert.deepStrictEqual(secretRequests, []);
});
