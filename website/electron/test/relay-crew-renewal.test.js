"use strict";

const { test, beforeEach, afterEach, mock } = require("node:test");
const assert = require("node:assert/strict");

const { createRelayCrewRenewal, renewDelayMs, OUTCOME, STOP_REFUSALS } = require("../relay-crew-renewal");

const T0 = 1_700_000_000_000; // the device clock at the start of each test
const flush = () => new Promise((resolve) => setImmediate(resolve));

function grant(token, { lifetime = 300, renewInSeconds = null, serverDate = 5_000 } = {}) {
  return { accessToken: token, expiresAt: serverDate + lifetime, renewInSeconds, serverDate };
}

/** A renewal wired to a mocked mint, a TokenHolder and a RelayCrewWindow as interface D defines them. */
function harness({ random = () => 0 } = {}) {
  const answers = [];
  const logs = [];
  const outcomes = [];
  let token = null;
  const holder = { current: () => token, set: (t) => { token = t; }, clear: () => { token = null; } };
  const status = [];
  const closed = [];
  const win = {
    reloads: 0,
    reload() { win.reloads += 1; },
    onMainFrameStatus: (cb) => status.push(cb),
    onClosed: (cb) => closed.push(cb),
    fire: (code) => status.forEach((cb) => cb(code)),
    close: () => closed.forEach((cb) => cb()),
  };
  const h = {
    answers, logs, outcomes, win, holder,
    mints: 0,
    pending: null,
    renewal: null,
    token: () => token,
  };
  h.renewal = createRelayCrewRenewal({
    endpointId: "abcdefghij0123456789",
    holder,
    random,
    log: (m) => logs.push(m),
    onOutcome: (o) => outcomes.push(o),
    mint: async () => {
      h.mints += 1;
      const a = answers.shift();
      if (a === "pending") return new Promise((resolve) => { h.pending = resolve; });
      if (a instanceof Error) throw a;
      return a;
    },
  });
  return h;
}

async function started(h, first = grant("tok-1")) {
  h.answers.push(first);
  assert.deepEqual(await h.renewal.start(), { ok: true });
  h.renewal.attach(h.win);
}

async function tick(ms) {
  mock.timers.tick(ms);
  await flush();
}

function assertNoToken(h, ...tokens) {
  const text = JSON.stringify([h.logs, h.outcomes]);
  for (const t of tokens) assert.ok(!text.includes(t), `token ${t} leaked into logs or outcomes`);
}

beforeEach(() => mock.timers.enable({ apis: ["setTimeout", "Date"], now: T0 }));
afterEach(() => mock.timers.reset());

test("renewDelayMs spreads renewInSeconds by up to 5 s and clamps to 5 to 240 s", () => {
  assert.equal(renewDelayMs({ renewInSeconds: 120 }, () => 0), 120_000);
  assert.equal(renewDelayMs({ renewInSeconds: 120 }, () => 0.5), 117_500);
  assert.equal(renewDelayMs({ renewInSeconds: 120 }, () => 0.999), 115_005);
  assert.equal(renewDelayMs({ renewInSeconds: 2 }, () => 0), 5_000);
  assert.equal(renewDelayMs({ renewInSeconds: 1_000 }, () => 0), 240_000);
  assert.equal(renewDelayMs({ renewInSeconds: null, lifetimeSeconds: 100 }), 40_000);
  assert.equal(renewDelayMs({ renewInSeconds: null, lifetimeSeconds: 300 }), 240_000);
  assert.equal(renewDelayMs({ renewInSeconds: null, lifetimeSeconds: 30 }), 5_000);
  assert.equal(renewDelayMs({ renewInSeconds: null, lifetimeSeconds: NaN }), 5_000);
});

test("lifetime counts from the answer's Date, not the device clock", async () => {
  const h = harness();
  // The service clock (5,000 s) is far from the device clock (T0), so only expiresAt - serverDate works.
  await started(h, grant("tok-1", { lifetime: 100 }));
  h.answers.push(grant("tok-2"));
  await tick(39_999);
  assert.equal(h.mints, 1);
  await tick(1);
  assert.equal(h.mints, 2);
  assert.equal(h.token(), "tok-2");
});

test("renews at renewInSeconds minus the spread", async () => {
  const h = harness({ random: () => 0.5 });
  await started(h, grant("tok-1", { renewInSeconds: 60 }));
  h.answers.push(grant("tok-2"));
  await tick(57_499);
  assert.equal(h.mints, 1);
  await tick(1);
  assert.equal(h.mints, 2);
});

test("a main-frame 401 mints once and reloads, a second within 30 s stops", async () => {
  const h = harness();
  await started(h);
  h.answers.push(grant("tok-2"));
  h.win.fire(503);
  h.win.fire(401);
  await flush();
  assert.equal(h.mints, 2);
  assert.equal(h.token(), "tok-2");
  assert.equal(h.win.reloads, 1);
  await tick(29_000);
  h.win.fire(401);
  assert.equal(h.mints, 2);
  assert.equal(h.token(), null);
  assert.equal(h.renewal.isStopped(), true);
  assert.deepEqual(h.outcomes.at(-1), { code: OUTCOME.SIGN_IN_FAILED, stopped: true });
  assertNoToken(h, "tok-1", "tok-2");
});

test("a 401 more than 30 s after the first mints again", async () => {
  const h = harness();
  await started(h, grant("tok-1", { renewInSeconds: 200 }));
  h.answers.push(grant("tok-2", { renewInSeconds: 200 }), grant("tok-3", { renewInSeconds: 200 }));
  h.win.fire(401);
  await flush();
  await tick(31_000);
  h.win.fire(401);
  await flush();
  assert.equal(h.mints, 3);
  assert.equal(h.win.reloads, 2);
  assert.equal(h.renewal.isStopped(), false);
});

test("a 401 during a renewal reloads once that mint lands", async () => {
  const h = harness();
  await started(h, grant("tok-1", { renewInSeconds: 60 }));
  h.answers.push("pending");
  await tick(60_000);
  h.win.fire(401);
  assert.equal(h.mints, 2);
  h.pending(grant("tok-2"));
  await flush();
  assert.equal(h.token(), "tok-2");
  assert.equal(h.win.reloads, 1);
});

test("a failed renewal keeps the old token and retries on the backoff steps", async () => {
  const h = harness({ random: () => 1 });
  await started(h, grant("tok-1", { renewInSeconds: 60 }));
  h.answers.push({ code: "SERVICE_ERROR" }, new Error("socket hang up"), grant("tok-2"));
  await tick(55_000); // 60 s minus the full 5 s spread
  assert.equal(h.mints, 2);
  assert.equal(h.token(), "tok-1");
  await tick(1_999);
  assert.equal(h.mints, 2);
  await tick(1); // first step: 2 s
  assert.equal(h.mints, 3);
  assert.equal(h.token(), "tok-1");
  await tick(5_000); // second step: 5 s
  assert.equal(h.mints, 4);
  assert.equal(h.token(), "tok-2");
  assert.deepEqual(h.outcomes, [
    { code: OUTCOME.RENEW_FAILING, refusal: "SERVICE_ERROR", stopped: false },
    { code: OUTCOME.RENEWED, stopped: false },
  ]);
  assertNoToken(h, "tok-1", "tok-2");
});

test("throttled retries once after the advised delay, then backs off", async () => {
  const h = harness({ random: () => 1 });
  await started(h, grant("tok-1", { renewInSeconds: 60 }));
  h.answers.push({ code: "THROTTLED", retryAfter: 7 }, { code: "THROTTLED", retryAfter: 7 }, grant("tok-2"));
  await tick(55_000);
  assert.equal(h.mints, 2);
  await tick(7_000);
  assert.equal(h.mints, 3);
  await tick(2_000); // a second throttle in a row takes the first backoff step, not retryAfter
  assert.equal(h.mints, 4);
  assert.equal(h.token(), "tok-2");
});

test("closing the window stops renewal and clears the holder", async () => {
  const h = harness();
  await started(h);
  h.win.close();
  assert.equal(h.token(), null);
  assert.deepEqual(h.outcomes, [{ code: OUTCOME.CLOSED, stopped: true }]);
  await tick(600_000);
  h.win.fire(401);
  assert.equal(h.mints, 1);
});

test("a mint that lands after the window closed is dropped", async () => {
  const h = harness();
  await started(h, grant("tok-1", { renewInSeconds: 60 }));
  h.answers.push("pending");
  await tick(60_000);
  h.win.close();
  h.pending(grant("tok-2"));
  await flush();
  assert.equal(h.token(), null);
});

test("the stop refusals are exactly the ones no retry can fix", () => {
  assert.deepEqual([...STOP_REFUSALS].sort(), [
    "ACCESS_DENIED", "CREW_ACCESS_NOT_GRANTED", "CREW_OFFLINE", "INVALID_REQUEST",
    "NOT_SET_UP", "REMOTE_ACCESS_DISABLED", "SIGNED_OUT", "SIGN_IN_CHANGED",
  ]);
  for (const code of STOP_REFUSALS) assert.equal(OUTCOME[code], code);
});

for (const code of STOP_REFUSALS) {
  test(`the refusal ${code} stops renewal, clears the holder and schedules no retry`, async () => {
    const h = harness();
    await started(h, grant("tok-1", { renewInSeconds: 60 }));
    h.answers.push({ code });
    await tick(60_000);
    assert.equal(h.token(), null);
    assert.equal(h.renewal.isStopped(), true);
    assert.deepEqual(h.outcomes, [{ code, stopped: true }]);
    await tick(600_000);
    assert.equal(h.mints, 2);
  });
}

test("a refused first mint stops with the code and sets no token", async () => {
  const h = harness();
  h.answers.push({ code: "CREW_OFFLINE" });
  assert.deepEqual(await h.renewal.start(), { ok: false, code: "CREW_OFFLINE" });
  assert.equal(h.renewal.isStopped(), true);
  assert.equal(h.token(), null);
  await tick(600_000);
  assert.equal(h.mints, 1);
});

test("a hidden window renews once, then parks until shown", async () => {
  const h = harness();
  await started(h, grant("tok-1", { renewInSeconds: 60 }));
  h.renewal.setHidden(true);
  h.answers.push(grant("tok-2", { renewInSeconds: 60 }), grant("tok-3", { renewInSeconds: 60 }));
  await tick(60_000);
  assert.equal(h.mints, 2);
  await tick(600_000);
  assert.equal(h.mints, 2);
  h.renewal.setHidden(false);
  await flush();
  assert.equal(h.mints, 3);
  assert.equal(h.token(), "tok-3");
});

test("stop() is idempotent and reports once", async () => {
  const h = harness();
  await started(h);
  h.renewal.stop();
  h.renewal.stop();
  h.win.close();
  assert.deepEqual(h.outcomes, [{ code: OUTCOME.STOPPED, stopped: true }]);
  assert.equal(h.token(), null);
});
