const { test } = require("node:test");
const assert = require("node:assert");
const {
  createMetricsRecorder,
  profilingEnabled,
  normalizeMetric,
  buildSample,
  MIN_INTERVAL_MS,
} = require("../perf-metrics");

// A recorder wired entirely to fakes: Electron main is not available here, and
// the real timers/filesystem would make these tests slow and order-dependent.
function harness(overrides = {}) {
  const writes = [];
  const renames = [];
  let intervalFn = null;
  const logs = [];
  const rec = createMetricsRecorder({
    dir: "/tmp/logs",
    env: { KIROCREW_DEBUG: "1" },
    getAppMetrics: () => [
      { pid: 1, type: "Browser", cpu: { percentCPUUsage: 3.5 }, memory: { workingSetSize: 100 } },
      { pid: 2, type: "Tab", cpu: { percentCPUUsage: 1.5 }, memory: { workingSetSize: 50 } },
    ],
    writeFileSync: (p, data, opts) => writes.push({ p, data, opts }),
    renameSync: (from, to) => renames.push({ from, to }),
    mkdirSync: () => {},
    unlinkSync: () => {},
    setIntervalFn: (fn) => {
      intervalFn = fn;
      return { unref() {} };
    },
    clearIntervalFn: () => {
      intervalFn = null;
    },
    log: (m) => logs.push(m),
    now: () => new Date("2026-08-02T12:00:00.000Z"),
    ...overrides,
  });
  return { rec, writes, renames, logs, tick: () => intervalFn && intervalFn(), hasInterval: () => intervalFn !== null };
}

test("the gate is off by default and nothing is written", () => {
  const h = harness({ env: {} });
  assert.strictEqual(h.rec.enabled, false);
  assert.strictEqual(h.rec.start(), false);
  assert.strictEqual(h.rec.sampleOnce(), false);
  assert.strictEqual(h.writes.length, 0, "a non-debug install must write no artifact at all");
});

test("an explicit falsey value reads as off, not as merely-set", () => {
  for (const raw of ["0", "false", "no", "off", ""]) {
    assert.strictEqual(profilingEnabled({ KIROCREW_DEBUG: raw }), false, `${raw} should be off`);
  }
  for (const raw of ["1", "true", "YES", " on "]) {
    assert.strictEqual(profilingEnabled({ KIROCREW_DEBUG: raw }), true, `${raw} should be on`);
  }
});

test("start writes an artifact immediately rather than waiting for the first tick", () => {
  const h = harness();
  assert.strictEqual(h.rec.start(), true);
  assert.strictEqual(h.rec.sampleCount, 1);
  assert.strictEqual(h.renames.length, 1, "the artifact should be published on start");
});

test("the artifact is written via a temp file and renamed into place", () => {
  const h = harness();
  h.rec.sampleOnce();
  assert.match(h.writes[0].p, /\.tmp$/, "must write to a temp path, never the artifact directly");
  assert.strictEqual(h.renames[0].to, h.rec.artifactPath);
  assert.strictEqual(h.renames[0].from, h.writes[0].p);
});

test("the artifact is written 0600", () => {
  const h = harness();
  h.rec.sampleOnce();
  assert.strictEqual(h.writes[0].opts && h.writes[0].opts.mode, 0o600);
});

test("the artifact parses and carries normalized totals", () => {
  const h = harness();
  h.rec.sampleOnce();
  const parsed = JSON.parse(h.writes[0].data);
  assert.strictEqual(parsed.version, 1);
  assert.strictEqual(parsed.samples.length, 1);
  const s = parsed.samples[0];
  assert.strictEqual(s.at, "2026-08-02T12:00:00.000Z");
  assert.strictEqual(s.processes.length, 2);
  assert.strictEqual(s.totalCpuPercent, 5);
  assert.strictEqual(s.totalWorkingSetKb, 150);
});

test("the sample ring is bounded so the log directory cannot grow without limit", () => {
  const h = harness({ capacity: 3 });
  for (let i = 0; i < 10; i++) h.rec.sampleOnce();
  assert.strictEqual(h.rec.sampleCount, 3);
  const parsed = JSON.parse(h.writes[h.writes.length - 1].data);
  assert.strictEqual(parsed.samples.length, 3);
});

test("the ring keeps the NEWEST samples, not the oldest", () => {
  let n = 0;
  const h = harness({
    capacity: 2,
    now: () => new Date(Date.UTC(2026, 7, 2, 12, 0, n++)),
  });
  for (let i = 0; i < 4; i++) h.rec.sampleOnce();
  const parsed = JSON.parse(h.writes[h.writes.length - 1].data);
  assert.deepStrictEqual(
    parsed.samples.map((s) => s.at),
    ["2026-08-02T12:00:02.000Z", "2026-08-02T12:00:03.000Z"]
  );
});

test("a throwing getAppMetrics is contained and does not propagate", () => {
  const h = harness({
    getAppMetrics: () => {
      throw new Error("metrics unavailable");
    },
  });
  assert.strictEqual(h.rec.sampleOnce(), false);
  assert.match(h.logs.join("\n"), /sample failed/);
});

test("a failing write is contained, reported, and cleans up its temp file", () => {
  const unlinked = [];
  const h = harness({
    writeFileSync: () => {
      throw new Error("ENOSPC");
    },
    unlinkSync: (p) => unlinked.push(p),
  });
  assert.strictEqual(h.rec.sampleOnce(), false);
  assert.match(h.logs.join("\n"), /write failed/);
  assert.strictEqual(unlinked.length, 1, "the temp file must not be left behind");
});

test("the interval is floored so a bad value cannot busy-loop against the app", () => {
  const h = harness({ intervalMs: 1 });
  h.rec.sampleOnce();
  const parsed = JSON.parse(h.writes[0].data);
  assert.strictEqual(parsed.intervalMs, MIN_INTERVAL_MS);
});

test("start is idempotent so a second call does not double-sample", () => {
  const h = harness();
  h.rec.start();
  h.rec.start();
  assert.strictEqual(h.rec.sampleCount, 1);
});

test("stop clears the interval and flushes a final time", () => {
  const h = harness();
  h.rec.start();
  const before = h.renames.length;
  assert.strictEqual(h.rec.stop(), true);
  assert.strictEqual(h.hasInterval(), false);
  assert.strictEqual(h.renames.length, before + 1, "the last window should survive a clean quit");
});

test("the interval is unref'd so the recorder cannot hold the app open at quit", () => {
  let unrefCalled = false;
  const h = harness({
    setIntervalFn: () => ({
      unref() {
        unrefCalled = true;
      },
    }),
  });
  h.rec.start();
  assert.strictEqual(unrefCalled, true);
});

test("ticking the interval accumulates samples", () => {
  const h = harness();
  h.rec.start();
  h.tick();
  h.tick();
  assert.strictEqual(h.rec.sampleCount, 3);
});

test("normalizeMetric allowlists fields and drops non-finite numbers", () => {
  const m = normalizeMetric({
    pid: 7,
    type: "Tab",
    cpu: { percentCPUUsage: NaN, idleWakeupsPerSecond: 4 },
    memory: { workingSetSize: 10 },
    integrityLevel: "medium",
    creationTime: 123,
  });
  assert.strictEqual(m.cpuPercent, null, "NaN must not land in the artifact");
  assert.strictEqual(m.idleWakeupsPerSecond, 4);
  assert.ok(!("integrityLevel" in m), "unlisted platform extras must be dropped");
  assert.ok(!("creationTime" in m), "unlisted platform extras must be dropped");
  assert.ok(!("name" in m), "absent optional fields should be omitted, not null");
});

test("normalizeMetric rejects junk entries", () => {
  assert.strictEqual(normalizeMetric(null), null);
  assert.strictEqual(normalizeMetric("nope"), null);
});

test("buildSample tolerates a non-array from getAppMetrics", () => {
  const s = buildSample(undefined, () => new Date("2026-08-02T12:00:00.000Z"));
  assert.deepStrictEqual(s.processes, []);
  assert.strictEqual(s.totalCpuPercent, 0);
});
