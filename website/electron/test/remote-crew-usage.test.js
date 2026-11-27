"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { collectPaneProcesses, summarizeUsage, paneOrigins, MAX_ORIGINS } = require("../remote-crew-usage");

const DASH = "http://localhost:7777";

function dashboard(frames, ownPid = 100) {
  return {
    isDestroyed: () => false,
    mainFrame: { osProcessId: ownPid, origin: DASH, framesInSubtree: [{ origin: DASH, osProcessId: ownPid }, ...frames] },
  };
}

describe("paneOrigins", () => {
  it("keeps loopback pane origins and drops the dashboard's own and others", () => {
    const got = paneOrigins(
      ["http://localhost:7801", DASH, "https://example.com", "http://localhost:7801/path", 7, null],
      DASH,
    );
    assert.deepEqual([...got], ["http://localhost:7801"]);
  });

  it("reads at most MAX_ORIGINS entries", () => {
    const many = Array.from({ length: MAX_ORIGINS + 10 }, (_, i) => `http://localhost:${8000 + i}`);
    assert.equal(paneOrigins(many, DASH).size, MAX_ORIGINS);
  });

  it("answers nothing for a non-array", () => {
    assert.equal(paneOrigins("http://localhost:7801", DASH).size, 0);
  });
});

describe("collectPaneProcesses", () => {
  it("counts only frames on named origins, one per origin", () => {
    const got = collectPaneProcesses({
      dashboard: dashboard([
        { origin: "http://localhost:7801", osProcessId: 201 },
        { origin: "http://localhost:7801", osProcessId: 201 },
        { origin: "http://localhost:7802", osProcessId: 202 },
        { origin: "https://ads.example", osProcessId: 300 },
      ]),
      origins: ["http://localhost:7801"],
      backendUrl: DASH,
    });
    assert.equal(got.panes, 1);
    assert.equal(got.sharedPanes, 0);
    assert.deepEqual([...got.pids], [201]);
  });

  it("counts a pane on the dashboard's own process as shared, not measured", () => {
    const got = collectPaneProcesses({
      dashboard: dashboard([{ origin: "http://localhost:7801", osProcessId: 100 }]),
      origins: ["http://localhost:7801"],
      backendUrl: DASH,
    });
    assert.equal(got.panes, 1);
    assert.equal(got.sharedPanes, 1);
    assert.equal(got.pids.size, 0);
  });

  it("adds relay pane pids and skips invalid ones", () => {
    const got = collectPaneProcesses({
      dashboard: dashboard([]),
      origins: [],
      backendUrl: DASH,
      relayPids: [401, 0, -1, "x"],
    });
    assert.equal(got.panes, 1);
    assert.deepEqual([...got.pids], [401]);
  });

  it("survives a torn-down dashboard and frames that throw", () => {
    const gone = { isDestroyed: () => true, get mainFrame() { throw new Error("destroyed"); } };
    assert.deepEqual(
      collectPaneProcesses({ dashboard: gone, origins: ["http://localhost:7801"], backendUrl: DASH, relayPids: [5] }),
      { panes: 1, sharedPanes: 0, pids: new Set([5]) },
    );
    const throwing = { isDestroyed: () => false, get mainFrame() { throw new Error("mid-teardown"); } };
    assert.equal(collectPaneProcesses({ dashboard: throwing, origins: [], backendUrl: DASH }).panes, 0);
    const badFrame = { get origin() { throw new Error("frame gone"); } };
    const got = collectPaneProcesses({
      dashboard: dashboard([badFrame, { origin: "http://localhost:7801", osProcessId: 201 }]),
      origins: ["http://localhost:7801"],
      backendUrl: DASH,
    });
    assert.equal(got.panes, 1);
  });
});

describe("summarizeUsage", () => {
  it("sums memory (KB to MB) and CPU over the collected pids only", () => {
    const collected = { panes: 2, sharedPanes: 1, pids: new Set([201, 401]) };
    const metrics = [
      { pid: 100, memory: { workingSetSize: 999999 }, cpu: { percentCPUUsage: 50 } },
      { pid: 201, memory: { workingSetSize: 102400 }, cpu: { percentCPUUsage: 1.25 } },
      { pid: 401, memory: { workingSetSize: 51200 }, cpu: { percentCPUUsage: 2 } },
      { pid: 402 },
    ];
    assert.deepEqual(summarizeUsage(collected, metrics), { panes: 2, sharedPanes: 1, memoryMb: 150, cpuPercent: 3.3 });
  });

  it("answers zeros with no metrics", () => {
    assert.deepEqual(summarizeUsage({ panes: 0, sharedPanes: 0, pids: new Set() }, undefined), {
      panes: 0, sharedPanes: 0, memoryMb: 0, cpuPercent: 0,
    });
  });
});
