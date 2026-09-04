const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  chooseDisplaySource,
  createDisplayMediaHandler,
} = require("../display-media");

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

  it("grants the chosen source via callback when sources are available", async () => {
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      getScreenAccessStatus: () => "granted",
      platform: "darwin",
    });
    await handler({}, (streams) => {
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
      platform: "darwin",
    });
    await handler({}, (s) => {
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
      platform: "darwin",
    });
    await handler({}, (s) => {
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
      platform: "darwin",
    });
    await handler({}, (s) => {
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
      platform: "linux",
    });
    await handler({}, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });
});
