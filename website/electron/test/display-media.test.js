const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  chooseDisplaySource,
  createDisplayMediaHandler,
} = require("../display-media");

// WHO may capture is capture-trust.js's decision, covered by its own suite. Here
// the predicate is INJECTED, so these tests pin what this handler does with each
// verdict without restating the trust rules.
const TRUSTED = { frame: "the app's own main frame" };
const UNTRUSTED = { frame: "a pane" };
const trustOnly = (allowed) => (request) => request === allowed;

describe("chooseDisplaySource", () => {
  it("returns null when there are no sources", () => {
    assert.equal(chooseDisplaySource([]), null);
    assert.equal(chooseDisplaySource(undefined), null);
  });

  it("prefers a whole-screen source over a window source", () => {
    const win = { id: "window:42:0", name: "Some App" };
    const screen = { id: "screen:1:0", name: "Entire Screen" };
    assert.equal(chooseDisplaySource([win, screen]), screen);
  });

  it("returns the first source when no screen sources are present", () => {
    const a = { id: "window:1:0", name: "A" };
    const b = { id: "window:2:0", name: "B" };
    assert.equal(chooseDisplaySource([a, b]), a);
  });
});

describe("createDisplayMediaHandler", () => {
  const screenSrc = { id: "screen:1:0", name: "Entire Screen" };

  /** A handler whose getSources() counts its own calls. */
  function countingHandler(extra = {}) {
    const calls = { getSources: 0 };
    const reasons = [];
    const handler = createDisplayMediaHandler({
      getSources: async () => {
        calls.getSources += 1;
        return [screenSrc];
      },
      getScreenAccessStatus: () => "granted",
      onPermissionNeeded: (r) => reasons.push(r),
      platform: "linux",
      isTrustedRequest: trustOnly(TRUSTED),
      ...extra,
    });
    return { handler, calls, reasons };
  }

  it("requires the trust dep: with none, nothing is granted", async () => {
    // A capability gate must fail CLOSED when nobody wired its decision. Without
    // this, a call site that forgets `isTrustedRequest` would hand a whole screen
    // to any requester on the session, which is the state this module started in.
    const calls = { getSources: 0 };
    const reasons = [];
    const handler = createDisplayMediaHandler({
      getSources: async () => {
        calls.getSources += 1;
        return [screenSrc];
      },
      onPermissionNeeded: (r) => reasons.push(r),
      platform: "linux",
    });
    let streams = "untouched";
    await handler(TRUSTED, (s) => {
      streams = s;
    });
    assert.deepEqual(streams, {});
    assert.deepEqual(reasons, ["untrusted-frame"]);
    assert.equal(calls.getSources, 0);
  });

  // A denial must be a REFUSAL, not a granted stream the caller happens to
  // ignore, so each asserts the source probe never ran (a count, not an exit
  // code) alongside the empty payload.
  it("denies a request the trust predicate refuses, without probing for sources", async () => {
    const { handler, calls, reasons } = countingHandler();
    let streams = "untouched";
    await handler(UNTRUSTED, (s) => {
      streams = s;
    });
    assert.deepEqual(streams, {});
    assert.deepEqual(reasons, ["untrusted-frame"]);
    assert.equal(calls.getSources, 0);
  });

  it("denies when the trust check itself throws", async () => {
    const { handler, calls, reasons } = countingHandler({
      isTrustedRequest: () => {
        throw new Error("frame destroyed");
      },
    });
    let streams = "untouched";
    await handler(TRUSTED, (s) => {
      streams = s;
    });
    assert.deepEqual(streams, {});
    assert.deepEqual(reasons, ["error"]);
    assert.equal(calls.getSources, 0);
  });

  it("passes the request through to the predicate rather than pre-judging it", async () => {
    const seen = [];
    const { handler } = countingHandler({
      isTrustedRequest: (request) => {
        seen.push(request);
        return true;
      },
    });
    await handler(UNTRUSTED, () => {});
    assert.deepEqual(seen, [UNTRUSTED]);
  });

  it("grants the chosen source via callback when sources are available", async () => {
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      getScreenAccessStatus: () => "granted",
      isTrustedRequest: trustOnly(TRUSTED),
      platform: "darwin",
    });
    await handler(TRUSTED, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });

  it("denies and notifies on macOS when screen access is denied (without calling getSources)", async () => {
    let calledGetSources = false;
    let reason;
    let streams = "untouched";
    const handler = createDisplayMediaHandler({
      getSources: async () => {
        calledGetSources = true;
        return [screenSrc];
      },
      getScreenAccessStatus: () => "denied",
      onPermissionNeeded: (r) => {
        reason = r;
      },
      isTrustedRequest: trustOnly(TRUSTED),
      platform: "darwin",
    });
    await handler(TRUSTED, (s) => {
      streams = s;
    });
    assert.equal(calledGetSources, false);
    assert.equal(reason, "denied");
    assert.deepEqual(streams, {});
  });

  it("denies and notifies when no capture sources are returned", async () => {
    let reason;
    let streams = "untouched";
    const handler = createDisplayMediaHandler({
      getSources: async () => [],
      getScreenAccessStatus: () => "granted",
      onPermissionNeeded: (r) => {
        reason = r;
      },
      isTrustedRequest: trustOnly(TRUSTED),
      platform: "darwin",
    });
    await handler(TRUSTED, (s) => {
      streams = s;
    });
    assert.equal(reason, "no-sources");
    assert.deepEqual(streams, {});
  });

  it("denies gracefully (no throw) when getSources rejects", async () => {
    let streams = "untouched";
    const handler = createDisplayMediaHandler({
      getSources: async () => {
        throw new Error("desktopCapturer failed");
      },
      getScreenAccessStatus: () => "granted",
      isTrustedRequest: trustOnly(TRUSTED),
      platform: "darwin",
    });
    await handler(TRUSTED, (s) => {
      streams = s;
    });
    assert.deepEqual(streams, {});
  });

  it("ignores screen-access status on non-darwin platforms and proceeds", async () => {
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      // even if this said 'denied', linux must not short-circuit
      getScreenAccessStatus: () => "denied",
      isTrustedRequest: trustOnly(TRUSTED),
      platform: "linux",
    });
    await handler(TRUSTED, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });
});
