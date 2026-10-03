const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  chooseAudioGrant,
  chooseDisplaySource,
  createDisplayMediaHandler,
  describeAudioTier,
  supportsSystemPicker,
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

  // The audio grant sits AFTER the identity gate: every request below is a
  // trusted one, so what is being tested is the audio decision, not admission.
  const trustAll = () => true;

  it("grants a loopback audio device on Windows when audio was requested", async () => {
    // The meeting-capture win: the handler auto-selects the source, so on Windows
    // the other participants' audio arrives with no picker at all.
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      platform: "win32",
      isTrustedRequest: trustAll,
    });
    await handler({ audioRequested: true }, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc, audio: "loopback" });
  });

  it("does NOT attach audio to a video-only request", async () => {
    // This handler is shared with the chat input's screen-snip tool, which asks for
    // video only. Attaching a loopback device there would start capturing the
    // user's system audio to take a screenshot.
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      platform: "win32",
      isTrustedRequest: trustAll,
    });
    await handler({ audioRequested: false }, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });

  it("treats a request with no audioRequested field as video-only", async () => {
    // Every existing caller (and every existing test) passes a bare request.
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      platform: "win32",
      isTrustedRequest: trustAll,
    });
    await handler({}, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });

  it("does not offer loopback audio where Electron cannot supply it", async () => {
    // electron.d.ts (43.2.0) states a loopback device is currently Windows-only.
    for (const platform of ["darwin", "linux"]) {
      let granted;
      const handler = createDisplayMediaHandler({
        getSources: async () => [screenSrc],
        getScreenAccessStatus: () => "granted",
        platform,
        isTrustedRequest: trustAll,
      });
      await handler({ audioRequested: true }, (streams) => {
        granted = streams;
      });
      assert.deepEqual(granted, { video: screenSrc }, platform);
    }
  });

  it("an untrusted requester gets no audio either, even on Windows", async () => {
    // Asking for audio must not be a way around the identity gate: a pane or a
    // browsed page that requests audio is refused before any grant is composed.
    const { handler, calls } = countingHandler({ platform: "win32" });
    let granted;
    await handler({ audioRequested: true }, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, {});
    assert.equal(calls.getSources, 0);
  });
});

describe("chooseAudioGrant", () => {
  it("requires BOTH an audio request and a supporting platform", () => {
    assert.equal(chooseAudioGrant({ audioRequested: true, platform: "win32" }), "loopback");
    assert.equal(chooseAudioGrant({ audioRequested: false, platform: "win32" }), undefined);
    assert.equal(chooseAudioGrant({ audioRequested: true, platform: "darwin" }), undefined);
    assert.equal(chooseAudioGrant({ audioRequested: true, platform: "linux" }), undefined);
  });

  it("tolerates a missing or empty options object", () => {
    assert.equal(chooseAudioGrant(), undefined);
    assert.equal(chooseAudioGrant({}), undefined);
  });
});

describe("describeAudioTier", () => {
  it("detects native picker availability from the Darwin kernel release", () => {
    assert.equal(supportsSystemPicker({ platform: "darwin", release: "24.0.0" }), true);
    assert.equal(supportsSystemPicker({ platform: "darwin", release: "23.6.0" }), false);
    assert.equal(supportsSystemPicker({ platform: "win32", release: "24.0.0" }), false);
    assert.equal(supportsSystemPicker({ platform: "linux", release: "24.0.0" }), false);
    assert.equal(supportsSystemPicker({ platform: "darwin", release: "unknown" }), false);
  });

  it("reports loopback where the handler grants a device itself", () => {
    assert.equal(describeAudioTier({
      platform: "win32",
      release: "10.0.0",
      useSystemPicker: false,
    }), "loopback");
  });

  it("credits the native picker only on macOS 15 and newer", () => {
    assert.equal(describeAudioTier({
      platform: "darwin",
      release: "24.1.0",
      useSystemPicker: true,
    }), "system-picker");
    assert.equal(describeAudioTier({
      platform: "darwin",
      release: "23.6.0",
      useSystemPicker: true,
    }), "video-only");
    assert.equal(describeAudioTier({
      platform: "darwin",
      release: "24.1.0",
      useSystemPicker: false,
    }), "video-only");
  });

  it("reports video-only where the picker flag is inert", () => {
    assert.equal(describeAudioTier({
      platform: "linux",
      release: "6.8.0",
      useSystemPicker: true,
    }), "video-only");
    assert.equal(describeAudioTier({ platform: "freebsd" }), "video-only");
    assert.equal(describeAudioTier(), "video-only");
  });
});
