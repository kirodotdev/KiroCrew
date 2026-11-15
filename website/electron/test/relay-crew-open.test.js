"use strict";

const { test, afterEach } = require("node:test");
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
      opts, focused: 0, closed: false, cbs: { status: [], closed: [], visibility: [], unreachable: [], action: [] },
      endpointId: opts.endpointId,
      notices: [],
      reload() {},
      showNotice(text) { w.notices.push(text); },
      onUnreachable: (cb) => w.cbs.unreachable.push(cb),
      onNoticeAction: (cb) => w.cbs.action.push(cb),
      focus() { w.focused += 1; },
      close() { if (!w.closed) { w.closed = true; w.cbs.closed.forEach((cb) => cb()); } },
      isClosed: () => w.closed,
      onMainFrameStatus: (cb) => w.cbs.status.push(cb),
      onClosed: (cb) => w.cbs.closed.push(cb),
      onVisibilityChange: (cb) => w.cbs.visibility.push(cb),
    };
    opened.push(w);
    liveWindows.push(w);
    return w;
  };
  return { opened, openWindow };
}

// Every fake window a test opened, closed after it so no renewal timer outlives it.
const liveWindows = [];
afterEach(() => { for (const w of liveWindows.splice(0)) w.close(); });

function setup({ answers, createRenewal = createRelayCrewRenewal, openWindow } = {}) {
  const requests = [];
  const markers = [];
  const logs = [];
  const queue = [...(answers || [{ ok: true, grant: GRANT, crewUrl: CREW_URL, name: "Team Crew" }])];
  const win = fakeWindowModule();
  const opener = createRelayCrewOpener({
    requestToken: async (id, signInMarker) => {
      requests.push(id);
      markers.push(signInMarker === undefined ? null : signInMarker);
      return queue.length > 1 ? queue.shift() : queue[0];
    },
    openWindow: openWindow || win.openWindow,
    createRenewal,
    createTokenHolder,
    log: (line) => logs.push(line),
  });
  return { opener, requests, markers, logs, win };
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

// Real renewals, recorded so a test can read isStopped(). A main-frame 401 makes
// a renewal mint again at once, so no timer has to fire.
function recordedRenewals() {
  const made = [];
  const createRenewal = (opts) => { const r = createRelayCrewRenewal(opts); made.push(r); return r; };
  return { made, createRenewal };
}
const okFor = (token) => ({ ok: true, grant: { ...GRANT, accessToken: token }, crewUrl: CREW_URL, name: "Team Crew" });
const refusal = (code) => ({ ok: false, code, retryAfter: null });
const settle = () => new Promise((resolve) => setImmediate(resolve));
// No window is still registered for these Crews: opening each mints again
// (the last queued answer) instead of focusing a window.
async function assertReopensFresh(s, ids) {
  for (const id of ids) {
    const before = s.requests.length;
    const focused = s.win.opened.map((w) => w.focused);
    await s.opener.open(id);
    assert.equal(s.requests.length, before + 1, `${id} mints fresh`);
    assert.deepEqual(s.win.opened.map((w) => w.focused).slice(0, focused.length), focused, `${id} focuses nothing`);
  }
}

test("a SIGNED_OUT renewal stops every renewal, empties every holder and closes every window", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), okFor(`${TOKEN}-b`), refusal("SIGNED_OUT")] });
  assert.deepEqual(await s.opener.open(ID), { ok: true });
  assert.deepEqual(await s.opener.open(OTHER), { ok: true });
  const [a, b] = s.win.opened;
  assert.equal(b.opts.getToken(), `${TOKEN}-b`);

  a.cbs.status[0](401); // A mints again and the gateway answers SIGNED_OUT
  await settle();
  assert.equal(made.length, 2);
  assert.ok(made.every((r) => r.isStopped()), "every renewal stopped");
  assert.equal(a.opts.getToken(), null);
  assert.equal(b.opts.getToken(), null);
  assert.ok(a.closed && b.closed, "every window closed");
  await assertReopensFresh(s, [ID, OTHER]);
  assert.ok(s.logs.includes(`relay crew ${ID}: open -> closed (SIGNED_OUT)`));
  assert.ok(s.logs.includes(`relay crew ${OTHER}: open -> closed (SIGNED_OUT)`));
  assertNoSecrets([...s.logs]);
  assert.equal(s.logs.some((l) => l.includes("relay-access-token")), false);
});

test("CREW_OFFLINE and RENEW_FAILING close nothing", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({
    createRenewal,
    answers: [okFor(TOKEN), okFor(`${TOKEN}-b`), refusal("CREW_OFFLINE"), refusal("UNREACHABLE")],
  });
  await s.opener.open(ID);
  await s.opener.open(OTHER);
  const [a, b] = s.win.opened;
  a.cbs.status[0](401); // CREW_OFFLINE: A's renewal stops, its window stays
  await settle();
  b.cbs.status[0](401); // UNREACHABLE: B reports RENEW_FAILING and backs off
  await settle();
  assert.equal(made[0].isStopped(), true);
  assert.equal(made[1].isStopped(), false);
  assert.equal(a.closed || b.closed, false, "no window closed");
  assert.equal(b.opts.getToken(), `${TOKEN}-b`, "B keeps its token while it retries");
  assert.equal(s.logs.some((l) => l.includes("closed (")), false);
  assert.deepEqual(await s.opener.open(OTHER), { ok: true }, "B is still registered");
  assert.equal(b.focused, 1, "and is focused, not opened again");
  for (const w of s.win.opened) w.close();
  assert.ok(made.every((r) => r.isStopped()));
});

test("a SIGNED_OUT while a restart mint is in flight leaves no renewal running and no window registered", async () => {
  const { made, createRenewal } = recordedRenewals();
  let release;
  const late = new Promise((resolve) => { release = resolve; });
  const s = setup({
    createRenewal,
    answers: [okFor(TOKEN), okFor(`${TOKEN}-b`), late, refusal("SIGNED_OUT")],
  });
  await s.opener.open(ID);
  await s.opener.open(OTHER);
  const [a, b] = s.win.opened;
  made[0].stop(); // A's renewal stopped; its window stays open
  const restarting = s.opener.open(ID);
  await settle();
  assert.equal(s.requests.length, 3, "the restart mint is in flight");

  b.cbs.status[0](401); // B's renewal sees SIGNED_OUT
  await settle();
  assert.ok(a.closed && b.closed);
  release(okFor(`${TOKEN}-2`)); // the restart's mint lands after the sign-out
  assert.deepEqual(await restarting, { ok: false, error: "UNAVAILABLE" });
  assert.equal(made.length, 3);
  assert.ok(made.every((r) => r.isStopped()), "no renewal running");
  assert.equal(a.opts.getToken(), null);
  assert.equal(s.win.opened.length, 2, "no new window");
  await assertReopensFresh(s, [ID, OTHER]);
});

test("a first mint refused SIGNED_OUT on a new open closes the other open windows", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), refusal("SIGNED_OUT")] });
  await s.opener.open(ID);
  const [a] = s.win.opened;
  assert.deepEqual(await s.opener.open(OTHER), { ok: false, error: "SIGNED_OUT" });
  assert.equal(a.closed, true);
  assert.equal(a.opts.getToken(), null);
  assert.equal(s.win.opened.length, 1, "no window for the refused Crew");
  assert.ok(made.every((r) => r.isStopped()), "no renewal running");
  await assertReopensFresh(s, [ID]);
});

test("a first mint refused SIGNED_OUT on a restart closes every window, the restarting one too", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), okFor(`${TOKEN}-b`), refusal("SIGNED_OUT")] });
  await s.opener.open(ID);
  await s.opener.open(OTHER);
  const [a, b] = s.win.opened;
  made[0].stop(); // A's renewal stopped; its window stays open
  assert.deepEqual(await s.opener.open(ID), { ok: false, error: "SIGNED_OUT" });
  assert.ok(a.closed && b.closed);
  assert.equal(b.opts.getToken(), null);
  assert.equal(made.length, 3);
  assert.ok(made.every((r) => r.isStopped()), "no renewal running");
  await assertReopensFresh(s, [ID, OTHER]);
});

test("a new open whose mint lands after everything closed opens nothing and stops its renewal", async () => {
  const { made, createRenewal } = recordedRenewals();
  let release;
  const late = new Promise((resolve) => { release = resolve; });
  const s = setup({ createRenewal, answers: [okFor(TOKEN), late, refusal("SIGNED_OUT")] });
  await s.opener.open(ID);
  const [a] = s.win.opened;
  const opening = s.opener.open(OTHER);
  await settle();
  a.cbs.status[0](401); // A's renewal sees SIGNED_OUT while OTHER's mint is in flight
  await settle();
  assert.equal(a.closed, true);
  release(okFor(`${TOKEN}-b`)); // OTHER's mint read the sign-in before the sign-out
  assert.deepEqual(await opening, { ok: false, error: "UNAVAILABLE" });
  assert.equal(s.win.opened.length, 1, "no window opened");
  assert.equal(made.length, 2);
  assert.ok(made.every((r) => r.isStopped()), "no renewal running");
  await assertReopensFresh(s, [ID, OTHER]);
});

const MARK_A = "aaaaaaaaaaaaaaaa";
const MARK_B = "bbbbbbbbbbbbbbbb";
const okAs = (token, signInMarker) => ({ ...okFor(token), signInMarker });
const changedLogs = (s) => s.logs.filter((l) => l.includes("closed (SIGN_IN_CHANGED)"));
const noMarkerLogged = (s) => s.logs.every((l) => !l.includes(MARK_A) && !l.includes(MARK_B));

test("a first open sends no marker and each window's renewal sends its own", async () => {
  const { createRenewal } = recordedRenewals();
  const s = setup({
    createRenewal,
    answers: [okAs(TOKEN, MARK_A), okAs(`${TOKEN}-b`, MARK_B), okAs(`${TOKEN}-2`, MARK_A), okAs(`${TOKEN}-b2`, MARK_B)],
  });
  await s.opener.open(ID);
  await s.opener.open(OTHER);
  const [a, b] = s.win.opened;
  a.cbs.status[0](401);
  await settle();
  b.cbs.status[0](401);
  await settle();
  assert.deepEqual(s.markers, [null, null, MARK_A, MARK_B]);
  for (const w of s.win.opened) w.close();
});

test("SIGN_IN_CHANGED on one window's renewal closes only that window", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({
    createRenewal,
    answers: [okAs(TOKEN, MARK_A), okAs(`${TOKEN}-b`, MARK_A), refusal("SIGN_IN_CHANGED")],
  });
  await s.opener.open(ID);
  await s.opener.open(OTHER);
  const [a, b] = s.win.opened;
  a.cbs.status[0](401); // A's renewal: the vault now holds another account
  await settle();
  assert.equal(a.closed, true);
  assert.equal(a.opts.getToken(), null);
  assert.equal(made[0].isStopped(), true);
  assert.equal(b.closed, false, "the other window stays open");
  assert.equal(b.opts.getToken(), `${TOKEN}-b`, "and keeps its token");
  assert.equal(made[1].isStopped(), false);
  assert.deepEqual(changedLogs(s), [`relay crew ${ID}: open -> closed (SIGN_IN_CHANGED)`]);
  assert.ok(noMarkerLogged(s));
  assertNoSecrets([...s.logs]);
  await assertReopensFresh(s, [ID]);
  b.close();
});

test("a restart after a switch sends the window's marker, closes the window and answers SIGN_IN_CHANGED", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okAs(TOKEN, MARK_A), refusal("SIGN_IN_CHANGED"), okAs(`${TOKEN}-b`, MARK_B)] });
  await s.opener.open(ID);
  const [a] = s.win.opened;
  made[0].stop(); // the renewal stopped while the window stays open
  assert.deepEqual(await s.opener.open(ID), { ok: false, error: "SIGN_IN_CHANGED" });
  assert.equal(s.markers[1], MARK_A, "the restart sent the window's marker");
  assert.equal(a.closed, true);
  assert.equal(changedLogs(s).length, 1);
  assert.deepEqual(await s.opener.open(ID), { ok: true }, "the next click opens as the new account");
  assert.equal(s.markers[2], null, "a fresh open sends no marker");
  assert.equal(s.win.opened.length, 2);
  s.win.opened[1].close();
  assert.ok(made.every((r) => r.isStopped()));
});

test("a late answer for a renewal stopped by a hand close changes nothing", async () => {
  const { createRenewal } = recordedRenewals();
  let release;
  const late = new Promise((resolve) => { release = resolve; });
  const s = setup({
    createRenewal,
    answers: [okAs(TOKEN, MARK_A), late, okAs(`${TOKEN}-b`, MARK_B), okAs(`${TOKEN}-b2`, MARK_B)],
  });
  await s.opener.open(ID);
  const [a] = s.win.opened;
  a.cbs.status[0](401); // A's renewal mint goes out and is held
  await settle();
  a.close();
  assert.deepEqual(await s.opener.open(OTHER), { ok: true });
  const b = s.win.opened[1];
  release(okAs(`${TOKEN}-2`, MARK_A));
  await settle();
  assert.equal(b.closed, false);
  assert.equal(a.opts.getToken(), null, "the stale grant is never set");
  b.cbs.status[0](401);
  await settle();
  assert.equal(b.closed, false);
  assert.equal(s.markers[3], MARK_B, "B renews with its own marker");
  assert.equal(changedLogs(s).length, 0);
  b.close();
});

// --- The window's notices: an offline host, lost access, Check again ---

const UNAVAILABLE_TEXT = {
  title: "Team Crew is currently unavailable", body: "", check: "Check again", close: "Close window",
};
const NO_ACCESS_TEXT = {
  title: "You no longer have access to Team Crew.",
  body: "To get access back, ask your admin.",
  check: "",
  close: "Close window",
};

test("a CREW_OFFLINE renewal stop shows the unavailable notice in that window and closes nothing", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), okFor(`${TOKEN}-b`), refusal("CREW_OFFLINE")] });
  await s.opener.open(ID);
  await s.opener.open(OTHER);
  const [a, b] = s.win.opened;
  a.cbs.status[0](401); // A mints again and the gateway answers CREW_OFFLINE
  await settle();
  assert.equal(made[0].isStopped(), true);
  assert.deepEqual(a.notices, [UNAVAILABLE_TEXT]);
  assert.deepEqual(b.notices, [], "only the window whose renewal stopped");
  assert.equal(a.closed || b.closed, false);
  assert.ok(s.logs.includes(`relay crew ${ID}: open -> notice (unavailable)`));
  assertNoSecrets([...s.logs]);
});

test("a CREW_ACCESS_NOT_GRANTED stop shows the lost-access notice with no Check again", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), refusal("CREW_ACCESS_NOT_GRANTED")] });
  await s.opener.open(ID);
  const w = s.win.opened[0];
  w.cbs.status[0](401);
  await settle();
  assert.equal(made[0].isStopped(), true, "retrying stops");
  assert.deepEqual(w.notices, [NO_ACCESS_TEXT]);
  assert.equal(w.closed, false);
  assert.ok(s.logs.includes(`relay crew ${ID}: open -> notice (no_access)`));
  assertNoSecrets([...s.logs]);
});

test("a Crew with no name gets a neutral one in the notice", async () => {
  const { createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [{ ...okFor(TOKEN), name: "" }, refusal("CREW_OFFLINE")] });
  await s.opener.open(ID);
  s.win.opened[0].cbs.status[0](401);
  await settle();
  assert.equal(s.win.opened[0].notices[0].title, "This Crew is currently unavailable");
});

test("other stops and a failing renewal show no notice", async () => {
  for (const code of ["REMOTE_ACCESS_DISABLED", "ACCESS_DENIED", "UNREACHABLE"]) {
    const { createRenewal } = recordedRenewals();
    const s = setup({ createRenewal, answers: [okFor(TOKEN), refusal(code)] });
    await s.opener.open(ID);
    s.win.opened[0].cbs.status[0](401);
    await settle();
    assert.deepEqual(s.win.opened[0].notices, [], code);
    s.win.opened[0].close();
  }
});

test("the window reporting the host unreachable stops renewal and shows the unavailable notice", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal });
  await s.opener.open(ID);
  const w = s.win.opened[0];
  w.cbs.unreachable.forEach((cb) => cb());
  assert.equal(made[0].isStopped(), true);
  assert.equal(w.opts.getToken(), null);
  assert.deepEqual(w.notices, [UNAVAILABLE_TEXT]);
  w.close();
});

test("Check again mints a new token and reloads when the host is back", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), okFor(`${TOKEN}-2`)] });
  await s.opener.open(ID);
  const w = s.win.opened[0];
  let reloads = 0;
  w.reload = () => { reloads += 1; };
  w.cbs.unreachable.forEach((cb) => cb());
  w.cbs.action.forEach((cb) => cb("check"));
  await settle();
  await settle();
  assert.equal(s.requests.length, 2, "one fresh mint");
  assert.equal(reloads, 1, "one reload, which removes the notice");
  assert.equal(made[1].isStopped(), false);
  assert.equal(w.opts.getToken(), `${TOKEN}-2`);
  assert.equal(w.notices.length, 1, "no second notice");
  w.close();
});

test("Check again while the host is still gone shows the notice again and does not retry on its own", async () => {
  const { createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), refusal("CREW_OFFLINE")] });
  await s.opener.open(ID);
  const w = s.win.opened[0];
  let reloads = 0;
  w.reload = () => { reloads += 1; };
  w.cbs.unreachable.forEach((cb) => cb());
  w.cbs.action.forEach((cb) => cb("check"));
  await settle();
  await settle();
  assert.equal(reloads, 0);
  assert.deepEqual(w.notices, [UNAVAILABLE_TEXT, UNAVAILABLE_TEXT]);
  assert.equal(s.requests.length, 2, "one attempt, then nothing");

  // A refusal of another kind, such as a local gateway that can't be reached,
  // keeps the notice that is up.
  const s2 = setup({ createRenewal: recordedRenewals().createRenewal, answers: [okFor(TOKEN), refusal("UNAVAILABLE")] });
  await s2.opener.open(ID);
  const w2 = s2.win.opened[0];
  w2.cbs.unreachable.forEach((cb) => cb());
  w2.cbs.action.forEach((cb) => cb("check"));
  await settle();
  await settle();
  assert.deepEqual(w2.notices, [UNAVAILABLE_TEXT, UNAVAILABLE_TEXT]);
});

test("Check again that finds access gone switches to the lost-access notice", async () => {
  const { createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), refusal("CREW_ACCESS_NOT_GRANTED")] });
  await s.opener.open(ID);
  const w = s.win.opened[0];
  w.cbs.unreachable.forEach((cb) => cb());
  w.cbs.action.forEach((cb) => cb("check"));
  await settle();
  await settle();
  assert.deepEqual(w.notices, [UNAVAILABLE_TEXT, NO_ACCESS_TEXT]);
});

test("Close window closes only that window and stops its renewal", async () => {
  const { made, createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), okFor(`${TOKEN}-b`)] });
  await s.opener.open(ID);
  await s.opener.open(OTHER);
  const [a, b] = s.win.opened;
  a.cbs.unreachable.forEach((cb) => cb());
  a.cbs.action.forEach((cb) => cb("close"));
  assert.equal(a.closed, true);
  assert.equal(b.closed, false);
  assert.equal(made[0].isStopped(), true);
  assert.equal(made[1].isStopped(), false);
  b.close();
});

test("clicking the Crew in the local dashboard while its notice is up runs the same one attempt", async () => {
  const { createRenewal } = recordedRenewals();
  const s = setup({ createRenewal, answers: [okFor(TOKEN), refusal("CREW_OFFLINE")] });
  await s.opener.open(ID);
  const w = s.win.opened[0];
  w.cbs.unreachable.forEach((cb) => cb());
  assert.deepEqual(await s.opener.open(ID), { ok: false, error: "CREW_OFFLINE" });
  assert.deepEqual(w.notices, [UNAVAILABLE_TEXT, UNAVAILABLE_TEXT]);
});
