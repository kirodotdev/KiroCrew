"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const {
  createReachabilityWatch,
  UNREACHABLE_AFTER_MS,
  UNREACHABLE_MIN_FAILURES,
  OUTAGE_GAP_MS,
} = require("../relay-crew-reachability");

function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, advance: (ms) => { t += ms; } };
}

// The page's own pace while it is disconnected: a status probe every 3 s, plus
// a socket attempt on its 1 s, 2 s, 4 s, 8 s, then 10 s backoff.
function failFor(watch, c, ms, everyMs = 3000) {
  let tripped = false;
  for (let elapsed = 0; elapsed < ms; elapsed += everyMs) {
    c.advance(everyMs);
    if (watch.fail()) tripped = true;
  }
  return tripped;
}

test("the cap is about two minutes and needs several failed attempts", () => {
  assert.equal(UNREACHABLE_AFTER_MS, 120_000);
  assert.ok(UNREACHABLE_MIN_FAILURES >= 5);
  assert.ok(OUTAGE_GAP_MS > 10_000, "longer than the socket's 10 s backoff ceiling");
});

test("failures for the whole cap trip once, and only once", () => {
  const c = clock();
  const w = createReachabilityWatch({ now: c.now });
  assert.equal(failFor(w, c, UNREACHABLE_AFTER_MS - 3000), false, "still inside the cap");
  assert.equal(failFor(w, c, 6000), true, "past the cap");
  assert.equal(failFor(w, c, 30_000), false, "a tripped watch stays quiet until reset");
  w.reset();
  assert.equal(failFor(w, c, UNREACHABLE_AFTER_MS + 3000), true, "reset starts a fresh cycle");
});

test("a blip that recovers inside the cap never trips", () => {
  const c = clock();
  const w = createReachabilityWatch({ now: c.now });
  assert.equal(failFor(w, c, 90_000), false);
  w.ok();
  assert.equal(failFor(w, c, 90_000), false, "a success starts the clock again");
});

test("a long silence between failures starts a new outage, so a wake from sleep does not trip at once", () => {
  const c = clock();
  const w = createReachabilityWatch({ now: c.now });
  assert.equal(failFor(w, c, 60_000), false);
  c.advance(10 * 60_000); // the machine slept
  assert.equal(w.fail(), false, "the first failure after the gap is a fresh start");
  assert.equal(failFor(w, c, 60_000), false);
});

test("elapsed time alone is not enough: a few failures spread over the cap do not trip", () => {
  const c = clock();
  const w = createReachabilityWatch({ now: c.now });
  let tripped = false;
  for (let i = 0; i < UNREACHABLE_MIN_FAILURES - 1; i += 1) {
    if (w.fail()) tripped = true;
    c.advance(OUTAGE_GAP_MS - 1);
  }
  assert.ok(c.now() - 1_000_000 > UNREACHABLE_AFTER_MS);
  assert.equal(tripped, false);
  assert.equal(w.fail(), true, "the failure that reaches the minimum trips");
});

test("now() defaults to Date.now", () => {
  const w = createReachabilityWatch();
  assert.equal(w.fail(), false);
});
