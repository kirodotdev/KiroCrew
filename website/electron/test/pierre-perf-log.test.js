"use strict";
//
// Guards for the crash-time highlight-churn dump. The behaviours worth locking
// are the ones a future edit could silently break: that steady state writes
// nothing, that the buffer cannot grow without bound, and that the flush leads
// with the ratio a post-mortem is looking for.
//
const test = require("node:test");
const assert = require("node:assert");

const {
  createPierrePerfLog,
  normalizeWindow,
  DEFAULT_CAPACITY,
} = require("../pierre-perf-log");

const win = (over = {}) => ({
  calls: 1,
  keys: 1,
  maxKeysForOneSurface: 1,
  repeatKeyCalls: 0,
  chars: 100,
  maxLen: 100,
  heapMB: 50,
  ...over,
});

test("a malformed or empty report is not recorded", () => {
  const log = createPierrePerfLog();
  assert.equal(log.record(null), null);
  assert.equal(log.record(undefined), null);
  assert.equal(log.record("nope"), null);
  assert.equal(log.record({ calls: 0 }), null);
  assert.equal(log.record({ calls: NaN }), null);
  assert.equal(log.size(), 0);
});

test("non-finite fields are coerced rather than reaching a log line", () => {
  const w = normalizeWindow({
    calls: 2,
    keys: NaN,
    maxKeysForOneSurface: NaN,
    repeatKeyCalls: Infinity,
    chars: Infinity,
    maxLen: 10,
    heapMB: "x",
  });
  assert.equal(w.keys, 0);
  assert.equal(w.maxKeysForOneSurface, 0);
  assert.equal(w.repeatKeyCalls, 0);
  assert.equal(w.chars, 0);
  assert.equal(w.heapMB, -1);
});

test("nothing is flushed when no report was recorded", () => {
  // The property that keeps a normal install from writing anything: a crash with
  // no highlighting activity adds no lines at all.
  assert.deepStrictEqual(createPierrePerfLog().flush(), []);
});

test("the buffer is bounded to its capacity", () => {
  const log = createPierrePerfLog({ capacity: 3 });
  for (let i = 0; i < 10; i++) log.record(win({ chars: i }));
  assert.equal(log.size(), 3);
});

test("capacity defaults and rejects nonsense", () => {
  assert.equal(createPierrePerfLog({ capacity: 0 }).size(), 0);
  const log = createPierrePerfLog({ capacity: "junk" });
  for (let i = 0; i < DEFAULT_CAPACITY + 5; i++) log.record(win());
  assert.equal(log.size(), DEFAULT_CAPACITY);
});

test("the oldest windows are the ones dropped", () => {
  const log = createPierrePerfLog({ capacity: 2, now: () => "T" });
  log.record(win({ chars: 1, maxLen: 1 }));
  log.record(win({ chars: 2, maxLen: 1 }));
  log.record(win({ chars: 3, maxLen: 1 }));
  const lines = log.flush();
  const body = lines.slice(1).join("\n");
  assert.ok(!body.includes("chars=1"), "oldest window should have been evicted");
  assert.ok(body.includes("chars=2") && body.includes("chars=3"));
});

test("flush leads with a summary carrying the peak ratio", () => {
  const log = createPierrePerfLog({ now: () => "T" });
  // A single render (ratio 1.0) then a streamed block re-tokenized per chunk.
  log.record(win({ calls: 1, keys: 1, maxKeysForOneSurface: 1, chars: 100, maxLen: 100 }));
  log.record(
    win({ calls: 4, keys: 4, maxKeysForOneSurface: 4, chars: 1000, maxLen: 200, heapMB: 900 })
  );
  const lines = log.flush();
  assert.match(lines[0], /pre-crash summary over 2 window\(s\)/);
  assert.match(lines[0], /peakCharsPerMaxLen=5\.0/);
  assert.match(lines[0], /peakKeysForOneSurface=4/);
  assert.match(lines[0], /peakMainHeap=900MB/);
  assert.equal(lines.length, 3, "summary plus one line per window");
});

test("the summary distinguishes many-blocks-once from one-block-churned", () => {
  // Both windows carry IDENTICAL calls/keys/chars/maxLen, so peakCharsPerMaxLen
  // is the same for each: 5 distinct blocks rendered once vs 1 block re-tokenized
  // 5 times. peakKeysForOneSurface is the only field that separates them, which is
  // why the verdict is read off it and not off the ratio.
  const many = createPierrePerfLog({ now: () => "T" });
  many.record(win({ calls: 5, keys: 5, maxKeysForOneSurface: 1, chars: 500, maxLen: 100 }));
  const churn = createPierrePerfLog({ now: () => "T" });
  churn.record(win({ calls: 5, keys: 5, maxKeysForOneSurface: 5, chars: 500, maxLen: 100 }));

  const manyLine = many.flush()[0];
  const churnLine = churn.flush()[0];
  assert.match(manyLine, /peakCharsPerMaxLen=5\.0/);
  assert.match(churnLine, /peakCharsPerMaxLen=5\.0/);
  assert.match(manyLine, /peakKeysForOneSurface=1/);
  assert.match(churnLine, /peakKeysForOneSurface=5/);
});

test("a memoization miss is reported apart from key churn", () => {
  const log = createPierrePerfLog({ now: () => "T" });
  // Same content re-hashed 6 times: no new keys, so this is NOT churn.
  log.record(win({ calls: 7, keys: 1, maxKeysForOneSurface: 1, repeatKeyCalls: 6 }));
  const lines = log.flush();
  assert.match(lines[0], /peakRepeatKeyCalls=6/);
  assert.match(lines[0], /peakKeysForOneSurface=1/);
  assert.match(lines[1], /repeatKeyCalls=6/);
});

test("flush drains, so a second crash does not replay the first", () => {
  const log = createPierrePerfLog();
  log.record(win());
  assert.equal(log.flush().length, 2);
  assert.deepStrictEqual(log.flush(), []);
});

test("a window with no content length reports n/a instead of dividing by zero", () => {
  const log = createPierrePerfLog({ now: () => "T" });
  log.record(win({ maxLen: 0, chars: 0 }));
  assert.match(log.flush()[1], /charsPerMaxLen=n\/a/);
});

test("lastLine reflects only the most recent window", () => {
  const log = createPierrePerfLog({ now: () => "T" });
  assert.equal(log.lastLine(), null);
  log.record(win({ chars: 111, maxLen: 111 }));
  log.record(win({ chars: 222, maxLen: 111 }));
  assert.ok(log.lastLine().includes("chars=222"));
});
