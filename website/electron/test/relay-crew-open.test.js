"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { createRelayCrewOpener } = require("../relay-crew-open");
const { createRelayCrewRenewal } = require("../relay-crew-renewal");
const { createTokenHolder } = require("../relay-crew-window");

const ID = "a1b2c3d4e5f6g7h8i9j0k1";
const OTHER = "z9y8x7w6v5u4t3s2r1q0p9";
const TOKEN = "relay-access-token-SENSITIVE-abcdef";
const CREW_URL = `https://${ID}.relay.example.test`;
const GRANT = { accessToken: TOKEN, expiresAt: 1300, renewInSeconds: 240, serverDate: 1000 };

// A stand-in for D's RelayCrewWindow (interface-D), including the
// onVisibilityChange hook D's held fix adds.
function fakeWindowModule() {
  const opened = [];
  const openWindow = (opts) => {
    const w = {
      opts, focused: 0, closed: false, cbs: { status: [], closed: [], visibility: [] },
      endpointId: opts.endpointId,
      reload() {},
      focus() { w.focused += 1; },
      close() { if (!w.closed) { w.closed = true; w.cbs.closed.forEach((cb) => cb()); } },
      isClosed: () => w.closed,
      onMainFrameStatus: (cb) => w.cbs.status.push(cb),
      onClosed: (cb) => w.cbs.closed.push(cb),
      onVisibilityChange: (cb) => w.cbs.visibility.push(cb),
    };
    opened.push(w);
    return w;
  };
  return { opened, openWindow };
}

function setup({ answers, createRenewal = createRelayCrewRenewal, openWindow } = {}) {
  const requests = [];
  const logs = [];
  const queue = [...(answers || [{ ok: true, grant: GRANT, crewUrl: CREW_URL, name: "Team Crew" }])];
  const win = fakeWindowModule();
  const opener = createRelayCrewOpener({
    requestToken: async (id) => {
      requests.push(id);
      return queue.length > 1 ? queue.shift() : queue[0];
    },
    openWindow: openWindow || win.openWindow,
    createRenewal,
    createTokenHolder,
    log: (line) => logs.push(line),
  });
  return { opener, requests, logs, win };
}

function assertNoSecrets(values) {
  for (const v of values) {
    const s = JSON.stringify(v);
    assert.equal(s.includes(TOKEN), false, s);
    assert.equal(s.includes(CREW_URL), false, s);
    assert.equal(s.includes("Team Crew"), false, s);
  }
}

test("mints before the window opens, titles it with the Crew's name, and answers only ok", async () => {
  const s = setup();
  const r = await s.opener.open(ID);
  assert.deepEqual(r, { ok: true });
  assert.deepEqual(s.requests, [ID]);
  assert.equal(s.win.opened.length, 1);
  const { opts } = s.win.opened[0];
  assert.equal(opts.endpointId, ID);
  assert.equal(opts.crewUrl, CREW_URL);
  assert.equal(opts.title, "Team Crew");
  assert.equal(opts.getToken(), TOKEN, "the window reads E's holder");
  assertNoSecrets([r, ...s.logs]);
  assert.deepEqual(s.logs.filter((l) => l.includes("open ->")), [`relay crew ${ID}: open -> opened`]);
  s.win.opened[0].close();
  assert.equal(opts.getToken(), null, "closing the window clears the token");
});

test("a refused first mint opens no window and answers the raw client code", async () => {
  for (const [code, want] of [
    ["CREW_OFFLINE", "CREW_OFFLINE"],
    ["RELAY_VIEWER_OFF", "RELAY_VIEWER_OFF"],
    ["UNAVAILABLE", "UNAVAILABLE"],
    ["THROTTLED", "THROTTLED"],
    ["BAD_ANSWER", "BAD_ANSWER"],
  ]) {
    const s = setup({ answers: [{ ok: false, code, retryAfter: null }] });
    assert.deepEqual(await s.opener.open(ID), { ok: false, error: want }, code);
    assert.equal(s.win.opened.length, 0, code);
  }
});

test("an endpoint ID that fails the grammar is refused with no mint", async () => {
  const s = setup();
  for (const bad of ["", "ABC", `${ID}.x`, 7, null]) {
    assert.deepEqual(await s.opener.open(bad), { ok: false, error: "INVALID_ENDPOINT" });
  }
  assert.deepEqual(s.requests, []);
});

test("an open Crew is focused, not opened twice, and a double click mints once", async () => {
  const s = setup();
  const [a, b] = await Promise.all([s.opener.open(ID), s.opener.open(ID)]);
  assert.deepEqual([a, b], [{ ok: true }, { ok: true }]);
  assert.deepEqual(s.requests, [ID]);
  assert.deepEqual(await s.opener.open(ID), { ok: true });
  assert.equal(s.win.opened.length, 1);
  assert.equal(s.win.opened[0].focused, 1);
  assert.deepEqual(s.requests, [ID], "focusing mints nothing");
  assert.ok(s.logs.includes(`relay crew ${ID}: open -> focused`));

  await s.opener.open(OTHER);
  assert.equal(s.win.opened.length, 2, "another Crew gets its own window");

  s.win.opened[0].close();
  assert.deepEqual(await s.opener.open(ID), { ok: true });
  assert.equal(s.win.opened.length, 3, "a closed Crew opens again");
  for (const w of s.win.opened) w.close();
});

test("the window's visibility drives E's setHidden", async () => {
  const hidden = [];
  let attached = null;
  const createRenewal = ({ holder }) => ({
    start: async () => { holder.set(TOKEN); return { ok: true }; },
    attach: (w) => { attached = w; },
    setHidden: (h) => hidden.push(h),
    stop() {},
  });
  const s = setup({ createRenewal });
  await s.opener.open(ID);
  const w = s.win.opened[0];
  assert.equal(attached, w);
  assert.equal(w.cbs.visibility.length, 1);
  w.cbs.visibility[0](true);
  w.cbs.visibility[0](false);
  assert.deepEqual(hidden, [true, false]);
});

test("renewal reads RELAY_VIEWER_OFF as a stop and UNAVAILABLE as a retry", async () => {
  const mints = [];
  const createRenewal = ({ mint }) => ({
    start: async () => {
      mints.push(await mint());
      mints.push(await mint());
      mints.push(await mint());
      return { ok: true };
    },
    attach() {}, setHidden() {}, stop() {},
  });
  const s = setup({
    createRenewal,
    answers: [
      { ok: false, code: "RELAY_VIEWER_OFF", retryAfter: null },
      { ok: false, code: "UNAVAILABLE", retryAfter: null },
      { ok: false, code: "THROTTLED", retryAfter: 3 },
    ],
  });
  await s.opener.open(ID);
  assert.deepEqual(mints, [
    { code: "NOT_SET_UP", retryAfter: null },
    { code: "UNREACHABLE", retryAfter: null },
    { code: "THROTTLED", retryAfter: 3 },
  ]);
});

test("a window that can't open stops renewal and answers UNAVAILABLE", async () => {
  let stopped = 0;
  const createRenewal = ({ holder }) => ({
    start: async () => { holder.set(TOKEN); return { ok: true }; },
    attach() {}, setHidden() {},
    stop: () => { stopped += 1; holder.clear(); },
  });
  const s = setup({
    createRenewal,
    openWindow: () => { throw new Error("relay crew: invalid endpoint or crew URL"); },
  });
  const r = await s.opener.open(ID);
  assert.deepEqual(r, { ok: false, error: "UNAVAILABLE" });
  assert.equal(stopped, 1);
  assertNoSecrets([r, ...s.logs]);
});

test("the preload bridge carries only the endpoint ID and returns what main answers", () => {
  const src = require("node:fs").readFileSync(require("node:path").join(__dirname, "..", "preload.js"), "utf8");
  const block = src.match(/exposeInMainWorld\("relayCrewAPI", \{([\s\S]*?)\n\}\);/);
  assert.ok(block, "preload.js exposes relayCrewAPI");
  assert.equal(
    block[1].trim(),
    'open: (endpointId) => ipcRenderer.invoke("relay-crew:open", String(endpointId || "")),',
  );
});

test("opening a Crew whose renewal stopped mints again and reloads, and a refusal answers its code", async () => {
  const made = [];
  const createRenewal = ({ mint, holder }) => {
    const r = {
      stopped: false,
      attached: null,
      async start() {
        const g = await mint();
        if (g && g.accessToken) { holder.set(g.accessToken); return { ok: true }; }
        r.stopped = true;
        return { ok: false, code: g.code };
      },
      attach(w) { r.attached = w; },
      setHidden() {},
      stop() { r.stopped = true; holder.clear(); },
      isStopped: () => r.stopped,
    };
    made.push(r);
    return r;
  };
  const s = setup({
    createRenewal,
    answers: [
      { ok: true, grant: GRANT, crewUrl: CREW_URL, name: "Team Crew" },
      { ok: true, grant: { ...GRANT, accessToken: `${TOKEN}-2` }, crewUrl: CREW_URL, name: "Team Crew" },
      { ok: false, code: "CREW_OFFLINE", retryAfter: null },
    ],
  });
  assert.deepEqual(await s.opener.open(ID), { ok: true });
  const w = s.win.opened[0];
  let reloads = 0;
  w.reload = () => { reloads += 1; };

  made[0].stop(); // renewal stopped while the window stays open
  assert.equal(w.opts.getToken(), null);
  assert.deepEqual(await s.opener.open(ID), { ok: true });
  assert.equal(s.requests.length, 2, "a stopped Crew mints again instead of focusing only");
  assert.equal(made.length, 2);
  assert.equal(made[1].attached, w, "the new renewal drives the same window");
  assert.equal(w.cbs.visibility.length, 2);
  assert.equal(reloads, 1);
  assert.equal(w.focused, 1);
  assert.equal(w.opts.getToken(), `${TOKEN}-2`, "the window reads the same holder");
  assert.equal(s.win.opened.length, 1, "no second window");

  made[1].stop();
  assert.deepEqual(await s.opener.open(ID), { ok: false, error: "CREW_OFFLINE" });
  assert.equal(reloads, 1, "a refused mint leaves the window as it is");
  assert.equal(w.closed, false);
  assertNoSecrets([...s.logs]);
});

test("a restart whose window closes mid-mint stops the new renewal, and a second click mints once", async () => {
  const made = [];
  const createRenewal = ({ mint, holder }) => {
    const r = {
      stopped: false,
      attached: null,
      async start() {
        const g = await mint();
        if (g && g.accessToken) { holder.set(g.accessToken); return { ok: true }; }
        r.stopped = true;
        return { ok: false, code: g.code };
      },
      attach(w) { r.attached = w; },
      setHidden() {},
      stop() { r.stopped = true; holder.clear(); },
      isStopped: () => r.stopped,
    };
    made.push(r);
    return r;
  };
  let release;
  const late = new Promise((resolve) => { release = resolve; });
  const s = setup({
    createRenewal,
    answers: [{ ok: true, grant: GRANT, crewUrl: CREW_URL, name: "Team Crew" }, late],
  });
  assert.deepEqual(await s.opener.open(ID), { ok: true });
  const w = s.win.opened[0];
  made[0].stop();

  const first = s.opener.open(ID);
  const second = s.opener.open(ID);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.requests.length, 2, "two clicks during a restart mint once");
  w.close(); // closed while the restart's mint is in flight
  release({ ok: true, grant: { ...GRANT, accessToken: `${TOKEN}-2` }, crewUrl: CREW_URL, name: "Team Crew" });
  assert.deepEqual(await first, { ok: false, error: "UNAVAILABLE" });
  assert.deepEqual(await second, { ok: false, error: "UNAVAILABLE" });
  assert.equal(made.length, 2);
  assert.equal(made[1].stopped, true, "the late renewal is stopped");
  assert.equal(made[1].attached, null, "and never attached to the closed window");
  assert.equal(w.opts.getToken(), null);
});
