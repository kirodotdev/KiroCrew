// Pins when the pet probe re-resolves its gateway credential. A minted link
// token is accepted for 300 s (LINK_WINDOW_SECS in token_auth_tokens.py), so
// gatewayToken() must mint a fresh one before that window closes rather than
// learn the expiry from a 403, which writes a `denied` security-event record
// and snaps the probe back to its 5 s base cadence. gatewayToken() and the
// cadence policy are sliced out of index.js and run in a VM with a fake clock
// and a fake mint, so no Electron main-process require and no socket is used.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { linkTokenIsStale } = require("../../link-token-age");

const source = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");
const LINK_WINDOW_MS = 300_000;

function sliceThrough(startMarker, fnMarker) {
  const start = source.indexOf(startMarker);
  const fnStart = source.indexOf(fnMarker, start);
  const end = source.indexOf("\n}", fnStart) + 2;
  assert.ok(start !== -1 && fnStart > start && end > fnStart,
    `${fnMarker} must remain sliceable from index.js`);
  return source.slice(start, end);
}

// `mint` decides what each fetchGatewayAuth() call answers; the default mints
// a numbered link token and records the fake time it was minted at.
function harness({ mint } = {}) {
  const clock = { now: 0 };
  const minted = new Map();
  let mints = 0;
  const defaultMint = () => {
    mints += 1;
    const value = `t${mints}`;
    minted.set(value, clock.now);
    return { value, viaCookie: false };
  };
  const sandbox = {
    // index.js requires this at its top, outside the sliced span.
    linkTokenIsStale,
    Date: { now: () => clock.now },
    fetchGatewayAuth: async () => (mint ? mint(clock, () => defaultMint()) : defaultMint()),
  };
  const code = `${sliceThrough("let cachedGatewayAuth", "async function gatewayToken(")}
${sliceThrough("const MOCHI_PET_RECONCILE_MS =", "function nextReconcileDelay(")}
({
  gatewayToken,
  nextReconcileDelay,
  dropRejected: () => { cachedGatewayAuth = { value: "" }; },
})`;
  const api = vm.runInNewContext(code, sandbox);
  return { api, clock, minted, mints: () => mints };
}

// A fake gateway: refuses a link token older than the 300 s window.
function gatewayRefuses(minted, auth, now) {
  if (auth.viaCookie) return false;
  return now - minted.get(auth.value) > LINK_WINDOW_MS;
}

test("a disabled pet probe never sends an expired link token over an hour", async () => {
  const { api, clock, minted, mints } = harness();
  let delay = 5000;
  let requests = 0;
  let denied = 0;
  while (clock.now < 3_600_000) {
    const auth = await api.gatewayToken();
    requests += 1;
    const refused = gatewayRefuses(minted, auth, clock.now);
    if (refused) {
      denied += 1;
      api.dropRejected();
    }
    delay = api.nextReconcileDelay(delay, refused ? "active" : "idle");
    clock.now += delay;
  }
  assert.equal(denied, 0, `the probe sent ${denied} expired link tokens`);
  assert.equal(delay, 300_000, "a disabled pet settles at the 300 s ceiling");
  assert.ok(requests < 30, `expected a backed-off request count, got ${requests}`);
  assert.ok(mints() <= requests, "never more than one mint per request");
});

test("a link token is reused inside the refresh window and re-minted before expiry", async () => {
  const { api, clock, mints } = harness();
  const first = await api.gatewayToken();
  clock.now = 239_999;
  assert.equal((await api.gatewayToken()).value, first.value, "still fresh: no mint");
  assert.equal(mints(), 1);
  clock.now = 240_000;
  const second = await api.gatewayToken();
  assert.notEqual(second.value, first.value, "re-minted before the 300 s window closes");
  assert.equal(mints(), 2);
});

test("a 5 s tick does not mint per tick", async () => {
  const { api, clock, mints } = harness();
  for (let t = 0; t < 200_000; t += 5000) {
    clock.now = t;
    await api.gatewayToken();
  }
  assert.equal(mints(), 1, "40 ticks inside the refresh window reuse one token");
});

test("a failed refresh mint fails closed and the next call mints again", async () => {
  let fail = false;
  const { api, clock, mints } = harness({
    mint: (_clock, next) => (fail ? null : next()),
  });
  const first = await api.gatewayToken();
  clock.now = 250_000;
  fail = true;
  assert.equal((await api.gatewayToken()).value, "", "a failed mint yields no credential");
  fail = false;
  clock.now = 255_000;
  const recovered = await api.gatewayToken();
  assert.ok(recovered.value && recovered.value !== first.value, "the next call re-mints");
  assert.equal(mints(), 2);
});

test("a thrown refresh mint never hands back the stale token", async () => {
  let fail = false;
  const { api, clock } = harness({
    mint: (_clock, next) => {
      if (fail) throw new Error("mint refused");
      return next();
    },
  });
  const first = await api.gatewayToken();
  clock.now = 250_000;
  fail = true;
  await assert.rejects(api.gatewayToken(), /mint refused/);
  clock.now = 310_000;
  await assert.rejects(api.gatewayToken(), /mint refused/,
    "still stale, so it mints again instead of returning the old token");
  fail = false;
  assert.notEqual((await api.gatewayToken()).value, first.value);
});

test("a wall-clock jump in either direction re-mints", async () => {
  const { api, clock, mints } = harness();
  clock.now = 1_000_000;
  await api.gatewayToken();
  clock.now = 1_000_000 - 3_600_000;
  await api.gatewayToken();
  assert.equal(mints(), 2, "a backward jump makes the age unknown, so it re-mints");
  clock.now += 7_200_000;
  await api.gatewayToken();
  assert.equal(mints(), 3, "a forward jump past the window re-mints");
});

test("a probe that stops mid-window resumes with a fresh token", async () => {
  const { api, clock, minted } = harness();
  clock.now = 0;
  await api.gatewayToken();
  clock.now = 120_000;
  await api.gatewayToken();
  // The loop is paused (suspend, a cancelled tick) for ten minutes.
  clock.now = 720_000;
  const auth = await api.gatewayToken();
  assert.equal(gatewayRefuses(minted, auth, clock.now), false,
    "the first request after the pause carries a token the gateway accepts");
});

test("a borrowed session cookie is not aged out", async () => {
  let borrows = 0;
  const { api, clock } = harness({
    mint: () => {
      borrows += 1;
      return { value: "session", viaCookie: true };
    },
  });
  await api.gatewayToken();
  clock.now = 3_600_000;
  await api.gatewayToken();
  assert.equal(borrows, 1, "a cookie is checked against session_exp, so it is kept");
});

test("the cached credential lives only in memory and the loop stops at quit", () => {
  // At quit the reconcile timer is cleared, so no tick (and so no mint) runs
  // after it, and the credential is never written anywhere that outlives the app.
  assert.match(source, /app\.on\("before-quit", \(\) => \{ if \(timer\) clearTimeout\(timer\); \}\)/);
  assert.doesNotMatch(source, /\.set\([^)]*cachedGatewayAuth/,
    "the credential must not be persisted to a store");
  assert.doesNotMatch(source, /glog\([^)]*(cachedGatewayAuth|auth\.value)/,
    "the credential must never be logged");
});
