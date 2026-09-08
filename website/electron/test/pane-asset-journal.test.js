"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  INFLIGHT_TRACK_MAX,
  PREFIX,
  attachPaneAssetJournal,
  classifyUrl,
  createPaneAssetTracker,
} = require("../pane-asset-journal");

/** A manual clock + timer queue so stall detection runs without real waiting. */
function fakeTime() {
  let t = 0;
  const timers = new Map();
  let seq = 0;
  return {
    now: () => t,
    setTimer(fn, ms) {
      const id = ++seq;
      timers.set(id, { at: t + ms, fn });
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    advance(ms) {
      t += ms;
      for (const [id, { at, fn }] of [...timers.entries()]) {
        if (at <= t) {
          timers.delete(id);
          fn();
        }
      }
    },
    pending: () => timers.size,
  };
}

function tracker(overrides = {}) {
  const lines = [];
  const time = fakeTime();
  const tr = createPaneAssetTracker({
    log: (l) => lines.push(l),
    now: time.now,
    setTimer: time.setTimer,
    clearTimer: time.clearTimer,
    stallMs: 1000,
    ...overrides,
  });
  return { tr, lines, time };
}

const PANE = "http://localhost:7778";

describe("classifyUrl", () => {
  it("accepts loopback hashed assets and returns origin + path only", () => {
    assert.deepEqual(classifyUrl("http://localhost:7778/assets/main-abc.js?x=1"), {
      origin: "http://localhost:7778",
      path: "/assets/main-abc.js",
    });
    assert.deepEqual(classifyUrl("http://127.0.0.1:7779/assets/a.css").origin, "http://127.0.0.1:7779");
    assert.equal(classifyUrl("http://kirocrew.localhost:7780/assets/a.js").origin, "http://kirocrew.localhost:7780");
  });

  it("rejects non-asset paths, non-loopback hosts and non-http schemes", () => {
    assert.equal(classifyUrl("http://localhost:7778/api/status"), null);
    assert.equal(classifyUrl("http://localhost:7778/?token=abc"), null);
    assert.equal(classifyUrl("http://example.com/assets/a.js"), null);
    assert.equal(classifyUrl("chrome-error://chromewebdata/assets/a.js"), null);
    assert.equal(classifyUrl("not a url"), null);
  });
});

describe("createPaneAssetTracker", () => {
  it("is silent while a graph loads and journals one settle line when it completes", () => {
    const { tr, lines } = tracker();
    for (let i = 0; i < 5; i++) tr.onStart(i, `${PANE}/assets/c${i}.js`);
    assert.equal(lines.length, 0);
    for (let i = 0; i < 4; i++) tr.onCompleted(i, `${PANE}/assets/c${i}.js`);
    assert.equal(lines.length, 0, "not settled until the last request finishes");
    tr.onCompleted(4, `${PANE}/assets/c4.js`);
    assert.equal(lines.length, 1);
    assert.match(lines[0], /^\[pane-assets\] origin=http:\/\/localhost:7778 started=5 done=5 failed=0 inflight=0 settled stalled=0$/);
  });

  it("journals a STALLED line naming the request that sat in flight past stallMs", () => {
    const { tr, lines, time } = tracker();
    tr.onStart(1, `${PANE}/assets/main-abc.js`);
    tr.onStart(2, `${PANE}/assets/App-def.js`);
    tr.onCompleted(1, `${PANE}/assets/main-abc.js`);
    time.advance(999);
    assert.equal(lines.length, 0);
    time.advance(1);
    assert.equal(lines.length, 1);
    assert.equal(
      lines[0],
      `${PREFIX} origin=${PANE} started=2 done=1 failed=0 inflight=1 STALLED=/assets/App-def.js after=1000ms`,
    );
    // The stall line is emitted once per request, never re-armed.
    time.advance(5000);
    assert.equal(lines.length, 1);
    // A late completion still settles the origin and reports the stall count.
    tr.onCompleted(2, `${PANE}/assets/App-def.js`);
    assert.equal(lines.length, 2);
    assert.match(lines[1], /settled stalled=1$/);
  });

  it("clears the stall timer when a request completes in time", () => {
    const { tr, lines, time } = tracker();
    tr.onStart(1, `${PANE}/assets/a.js`);
    tr.onCompleted(1, `${PANE}/assets/a.js`);
    assert.equal(time.pending(), 0);
    time.advance(10_000);
    assert.equal(lines.filter((l) => l.includes("STALLED")).length, 0);
  });

  it("counts network errors as failed, not done", () => {
    const { tr, lines } = tracker();
    tr.onStart(1, `${PANE}/assets/a.js`);
    tr.onError(1, `${PANE}/assets/a.js`);
    assert.equal(lines.length, 1);
    assert.match(lines[0], /started=1 done=0 failed=1 inflight=0 settled/);
  });

  it("keeps origins independent and never journals the dashboard's own origin", () => {
    const { tr, lines } = tracker({ skipOrigin: "http://localhost:5476" });
    tr.onStart(1, "http://localhost:5476/assets/main.js");
    tr.onStart(2, `${PANE}/assets/a.js`);
    tr.onStart(3, "http://localhost:7779/assets/a.js");
    tr.onCompleted(2, `${PANE}/assets/a.js`);
    assert.equal(lines.length, 1);
    assert.match(lines[0], /origin=http:\/\/localhost:7778 .* settled/);
    assert.equal(tr.snapshot("http://localhost:5476"), null);
    assert.deepEqual(tr.snapshot("http://localhost:7779"), { started: 1, done: 0, failed: 0, stalled: 0, inflight: 1 });
  });

  it("ignores completions for requests it never saw start", () => {
    const { tr, lines } = tracker();
    tr.onCompleted(99, `${PANE}/assets/a.js`);
    tr.onStart(1, `${PANE}/assets/b.js`);
    tr.onCompleted(99, `${PANE}/assets/a.js`);
    assert.equal(lines.length, 0);
    assert.deepEqual(tr.snapshot(PANE), { started: 1, done: 0, failed: 0, stalled: 0, inflight: 1 });
  });

  it("strips the query string from the journaled path", () => {
    const { tr, lines, time } = tracker();
    tr.onStart(1, `${PANE}/assets/a.js?token=secret`);
    time.advance(1000);
    assert.equal(lines.length, 1);
    assert.doesNotMatch(lines[0], /secret|token/);
    assert.match(lines[0], /STALLED=\/assets\/a\.js /);
  });

  it("stops timing (but keeps counting) past the in-flight tracking ceiling", () => {
    const { tr, lines, time } = tracker();
    for (let i = 0; i < INFLIGHT_TRACK_MAX + 10; i++) tr.onStart(i, `${PANE}/assets/c${i}.js`);
    assert.equal(time.pending(), INFLIGHT_TRACK_MAX);
    assert.equal(tr.snapshot(PANE).inflight, INFLIGHT_TRACK_MAX + 10);
    for (let i = 0; i < INFLIGHT_TRACK_MAX + 10; i++) tr.onCompleted(i, `${PANE}/assets/c${i}.js`);
    assert.equal(lines.length, 1);
    assert.match(lines[0], new RegExp(`started=${INFLIGHT_TRACK_MAX + 10} done=${INFLIGHT_TRACK_MAX + 10} failed=0 inflight=0 settled`));
  });
});

describe("attachPaneAssetJournal", () => {
  function fakeSession() {
    const listeners = {};
    return {
      listeners,
      webRequest: {
        onSendHeaders(filter, fn) { listeners.before = { filter, fn }; },
        onCompleted(filter, fn) { listeners.completed = { filter, fn }; },
        onErrorOccurred(filter, fn) { listeners.error = { filter, fn }; },
      },
    };
  }

  it("tolerates a missing session or log", () => {
    assert.equal(attachPaneAssetJournal(null, () => {}, "http://localhost:5476"), false);
    assert.equal(attachPaneAssetJournal({}, () => {}, "http://localhost:5476"), false);
    assert.equal(attachPaneAssetJournal(fakeSession(), null, "http://localhost:5476"), false);
  });

  it("registers loopback-asset filtered non-blocking listeners", () => {
    const s = fakeSession();
    const lines = [];
    assert.equal(attachPaneAssetJournal(s, (l) => lines.push(l), "http://localhost:5476/?token=abc"), true);
    for (const key of ["before", "completed", "error"]) {
      assert.ok(s.listeners[key], `${key} listener registered`);
      assert.ok(s.listeners[key].filter.urls.every((u) => u.includes("/assets/*")), "filtered to hashed assets");
    }
    // Non-blocking: the fake exposes only `onSendHeaders` (no `onBeforeRequest`),
    // so attaching at all proves the journal never takes the blocking slot.
    s.listeners.before.fn({ id: 1, url: `${PANE}/assets/a.js` });
    s.listeners.completed.fn({ id: 1, url: `${PANE}/assets/a.js` });
    assert.equal(lines.length, 1);
    assert.match(lines[0], /origin=http:\/\/localhost:7778 started=1 done=1/);
  });

  it("does not journal the dashboard's own bundle", () => {
    const s = fakeSession();
    const lines = [];
    attachPaneAssetJournal(s, (l) => lines.push(l), "http://localhost:5476/?token=abc");
    s.listeners.before.fn({ id: 1, url: "http://localhost:5476/assets/main.js" });
    s.listeners.completed.fn({ id: 1, url: "http://localhost:5476/assets/main.js" });
    assert.equal(lines.length, 0);
  });
});

describe("aggregate log budget", () => {
  it("a pane that settles forever writes at most logLinesMax lines, the last one naming the cap", () => {
    const { tr, lines } = tracker({ logLinesMax: 5 });
    // Each single fetch that completes is one settle -> one line. Loop well past the cap.
    for (let i = 0; i < 50; i++) {
      tr.onStart(i, `${PANE}/assets/c${i}.js`);
      tr.onCompleted(i, `${PANE}/assets/c${i}.js`);
    }
    assert.equal(lines.length, 5);
    assert.match(lines[4], /budget-exhausted lines=5/);
    assert.equal(lines.filter((l) => /settled/.test(l)).length, 4);
    // Counters still count; only the journal is quiet.
    assert.equal(tr.snapshot(PANE).done, 50);
  });

  it("stall lines share the same budget", () => {
    const { tr, lines, time } = tracker({ logLinesMax: 3 });
    for (let i = 0; i < 10; i++) tr.onStart(i, `${PANE}/assets/c${i}.js`);
    time.advance(1000);
    assert.equal(lines.length, 3);
    assert.match(lines[2], /budget-exhausted/);
  });
});
