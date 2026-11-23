"use strict";

// `crashNoticeSummary` adds this launch's native-log-off cause, and only a code
// from the closed set: the log path and any free text stay in main.
//
// A file of its own, so crash-collector.test.js stays within its line target.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { crashNoticeSummary } = require("../crash-collector");

describe("crashNoticeSummary native-log-off cause", () => {
  it("adds the native-log-off cause only when the file sink is off this launch", () => {
    assert.deepEqual(crashNoticeSummary({ newCrashes: [{}] }, { fileLogOffCause: "disk_full" }), {
      newCount: 1,
      lastCrashAt: null,
      nativeLogOff: "disk_full",
    });
    assert.deepEqual(crashNoticeSummary({ newCrashes: [{}] }, { fileLogOffCause: null }), {
      newCount: 1,
      lastCrashAt: null,
    });
    assert.deepEqual(crashNoticeSummary({ newCrashes: [{}] }, null), { newCount: 1, lastCrashAt: null });
  });

  it("carries nothing else from the native-logging result", () => {
    const status = { fileLogOffCause: "in_use", logPath: "/Users/someone/Library/Logs/chromium.log", blocked: true };
    assert.deepEqual(Object.keys(crashNoticeSummary(null, status)).sort(), ["lastCrashAt", "nativeLogOff", "newCount"]);
    assert.equal(JSON.stringify(crashNoticeSummary(null, status)).includes("/Users"), false);
  });

  it("drops anything that is not one of the closed-set cause codes", () => {
    const sentence = "chromium.log could not be rotated (rotate failed (ENOSPC))";
    const none = { newCount: 0, lastCrashAt: null };
    assert.deepEqual(crashNoticeSummary(null, { fileLogOffCause: sentence }), none);
    assert.deepEqual(crashNoticeSummary(null, { fileLogOffCause: { path: "/x" } }), none);
    assert.deepEqual(crashNoticeSummary(null, { fileLogOffReason: "disk_full" }), none);
  });
});
