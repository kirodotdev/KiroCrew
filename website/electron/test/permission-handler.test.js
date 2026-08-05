const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  isAppOrigin,
  requestsVideo,
  isNoteworthyDenial,
  createPermissionRequestHandler,
  createPermissionCheckHandler,
} = require("../permission-handler");

/** webContents stub whose getURL() returns `url`. */
const wcAt = (url) => ({ getURL: () => url });
/** A destroyed webContents: getURL() throws. */
const wcDestroyed = () => ({ getURL: () => { throw new Error("Object has been destroyed"); } });

const APP = wcAt("http://localhost:5476/chat/x?token=abc");
const ORIGIN = "http://localhost:5476";
const allowAll = { isAppOrigin: () => true, onDeny: () => {} };
const quiet = { onDeny: () => {} };

/** Invoke the request handler and return what it passed to callback(). */
function grant(handler, wc, permission, details) {
  let granted;
  handler(wc, permission, (v) => { granted = v; }, details);
  return granted;
}

describe("isAppOrigin — two sources", () => {
  it("accepts the webContents URL on any port", () => {
    assert.equal(isAppOrigin(APP), true);
    assert.equal(isAppOrigin(wcAt("http://localhost:6777/")), true);
  });

  it("FALLS BACK to the origin string when webContents is null", () => {
    // THE BUG: Electron passes a null webContents for checks that don't come
    // from a live frame. The old code ran new URL("") -> threw -> denied, while
    // discarding the origin string provided for exactly this case.
    assert.equal(isAppOrigin(null, ORIGIN), true);
    assert.equal(isAppOrigin(undefined, "http://localhost:5476/"), true);
    assert.equal(isAppOrigin({}, ORIGIN), true);
  });

  it("falls back when webContents getURL() throws (destroyed)", () => {
    assert.equal(isAppOrigin(wcDestroyed(), ORIGIN), true);
    assert.equal(isAppOrigin(wcDestroyed(), undefined), false);
  });

  it("denies when BOTH sources are absent or foreign", () => {
    assert.equal(isAppOrigin(null, undefined), false);
    assert.equal(isAppOrigin(null, ""), false);
    assert.equal(isAppOrigin(wcAt("http://evil.example/"), "http://evil.example"), false);
    assert.equal(isAppOrigin(null, "http://evil.example"), false);
  });

  it("compares hostname, never a substring (no localhost.evil bypass)", () => {
    assert.equal(isAppOrigin(null, "http://localhost.evil.example/"), false);
    assert.equal(isAppOrigin(wcAt("http://notlocalhost/"), undefined), false);
    assert.equal(isAppOrigin(null, "http://evil.example/?x=http://localhost"), false);
  });

  it("tolerates unparseable values by trying the other source", () => {
    assert.equal(isAppOrigin(wcAt("not a url"), ORIGIN), true);
    assert.equal(isAppOrigin(wcAt("not a url"), "also not a url"), false);
    assert.equal(isAppOrigin(null, 42), false);
  });
});

describe("requestsVideo", () => {
  it("is true ONLY for an explicit video entry", () => {
    assert.equal(requestsVideo({ mediaTypes: ["video"] }), true);
    assert.equal(requestsVideo({ mediaTypes: ["audio", "video"] }), true);
  });

  it("treats absent / empty / non-array mediaTypes as NOT video", () => {
    for (const d of [{}, { mediaTypes: [] }, { mediaTypes: undefined }, { mediaTypes: "audio" }, undefined, null]) {
      assert.equal(requestsVideo(d), false, `${JSON.stringify(d)} must not read as video`);
    }
  });
});

describe("createPermissionCheckHandler", () => {
  it("GRANTS the real observed payload: live frame, NO mediaType at all", () => {
    // THE VERIFIED CAUSE. Captured from Electron 33.4.11 in the packaged app:
    //   CHECK media wcIsNull=false origin=http://localhost:5476/ mediaType=undefined
    // Electron issues a first `media` check whose details carry no mediaType —
    // only embeddingOrigin / isMainFrame / requestingUrl — then a second with
    // mediaType:"audio". The shipped `mediaType === "audio"` exact match denied
    // the FIRST one, so getUserMedia rejected with NotAllowedError before the
    // audio-specific check was ever reached.
    const h = createPermissionCheckHandler(quiet);
    const observed = {
      embeddingOrigin: "http://localhost:5476/",
      isMainFrame: true,
      requestingUrl: "http://localhost:5476/chat/x?token=redacted&sid=chat-70",
    };
    assert.equal(h(APP, "media", "http://localhost:5476/", observed), true);
    // …and the follow-up check that did carry mediaType.
    assert.equal(h(APP, "media", "http://localhost:5476/", { ...observed, mediaType: "audio" }), true);
  });

  it("grants mic when webContents is null but origin is ours", () => {
    // Not the cause observed here (wcIsNull was false), but Electron documents
    // a nullable webContents, and the old isAppOrigin threw on it.
    const h = createPermissionCheckHandler(quiet);
    assert.equal(h(null, "media", ORIGIN, { mediaType: "audio" }), true);
    assert.equal(h(null, "media", ORIGIN, { mediaType: "unknown" }), true);
    assert.equal(h(null, "media", ORIGIN, {}), true);
    assert.equal(h(null, "media", ORIGIN, undefined), true);
  });

  it("grants a non-'audio' mediaType such as 'unknown' (the other defect)", () => {
    const h = createPermissionCheckHandler(quiet);
    assert.equal(h(APP, "media", ORIGIN, { mediaType: "unknown" }), true);
  });

  it("refuses video, keeping the camera gated", () => {
    const h = createPermissionCheckHandler(quiet);
    assert.equal(h(APP, "media", ORIGIN, { mediaType: "video" }), false);
    assert.equal(h(null, "media", ORIGIN, { mediaType: "video" }), false);
  });

  it("denies non-media permissions and foreign origins", () => {
    const h = createPermissionCheckHandler(quiet);
    for (const p of ["geolocation", "notifications", "midi", "clipboard-read", "unknown"]) {
      assert.equal(h(APP, p, ORIGIN, {}), false, `${p} must be denied`);
    }
    assert.equal(h(wcAt("http://evil.example/"), "media", "http://evil.example", { mediaType: "audio" }), false);
    assert.equal(h(null, "media", undefined, { mediaType: "audio" }), false);
  });
});

describe("createPermissionRequestHandler", () => {
  it("grants audio-only and unspecified mediaTypes", () => {
    const h = createPermissionRequestHandler(allowAll);
    assert.equal(grant(h, APP, "media", { mediaTypes: ["audio"] }), true);
    assert.equal(grant(h, APP, "media", {}), true);
    assert.equal(grant(h, APP, "media", undefined), true);
  });

  it("denies video and every non-media permission", () => {
    const h = createPermissionRequestHandler(allowAll);
    assert.equal(grant(h, APP, "media", { mediaTypes: ["video"] }), false);
    assert.equal(grant(h, APP, "geolocation", { mediaTypes: ["audio"] }), false);
  });

  it("uses details.securityOrigin when webContents is null", () => {
    const h = createPermissionRequestHandler(quiet);
    assert.equal(grant(h, null, "media", { mediaTypes: ["audio"], securityOrigin: ORIGIN }), true);
    assert.equal(grant(h, null, "media", { mediaTypes: ["audio"] }), false);
  });

  it("invokes the callback exactly once, granted or not", () => {
    const h = createPermissionRequestHandler(allowAll);
    let calls = 0;
    h(APP, "geolocation", () => { calls += 1; }, {});
    h(APP, "media", () => { calls += 1; }, {});
    assert.equal(calls, 2);
  });
});

// macOS gates the mic separately from Electron, and its prompt is ONE-SHOT:
// once denied, the OS never asks again. So a bare deny is a permanent dead end
// — which is exactly what the user saw ("permission denied", no prompt, nothing
// to click). The request handler therefore consults TCC and routes a denied
// state to a recovery dialog.
describe("createPermissionRequestHandler — macOS TCC leg", () => {
  /** Collect the callback value from an async (promise-tailed) handler. */
  const grantAsync = async (h, wc, permission, details) => {
    let got;
    h(wc, permission, (v) => { got = v; }, details);
    await new Promise((r) => setImmediate(r));
    return got;
  };
  const audio = { mediaTypes: ["audio"] };

  it("grants without asking when TCC already granted", async () => {
    let asked = 0;
    const h = createPermissionRequestHandler({
      ...allowAll,
      getMicAccessStatus: () => "granted",
      askForMicAccess: async () => { asked += 1; return true; },
    });
    assert.equal(await grantAsync(h, APP, "media", audio), true);
    assert.equal(asked, 0, "must not re-prompt when already granted");
  });

  it("asks the OS in-context when not-determined, and honors the answer", async () => {
    for (const answer of [true, false]) {
      let asked = 0;
      const h = createPermissionRequestHandler({
        ...allowAll,
        getMicAccessStatus: () => "not-determined",
        askForMicAccess: async () => { asked += 1; return answer; },
      });
      assert.equal(await grantAsync(h, APP, "media", audio), answer);
      assert.equal(asked, 1);
    }
  });

  it("denies AND surfaces a recovery route when TCC is denied/restricted", async () => {
    // THE DEAD END: macOS will not re-prompt, so denying silently leaves the
    // mic broken forever. The blocked callback is the only way back.
    for (const status of ["denied", "restricted"]) {
      const blocked = [];
      let asked = 0;
      const h = createPermissionRequestHandler({
        ...allowAll,
        getMicAccessStatus: () => status,
        askForMicAccess: async () => { asked += 1; return true; },
        onMicBlocked: (r) => blocked.push(r),
      });
      assert.equal(await grantAsync(h, APP, "media", audio), false);
      assert.deepStrictEqual(blocked, [status]);
      assert.equal(asked, 0, "asking is pointless once denied — macOS won't prompt");
    }
  });

  it("never consults the OS for a request Electron already denied", async () => {
    let probed = 0;
    const h = createPermissionRequestHandler({
      ...allowAll,
      getMicAccessStatus: () => { probed += 1; return "granted"; },
    });
    assert.equal(await grantAsync(h, APP, "media", { mediaTypes: ["video"] }), false);
    assert.equal(await grantAsync(h, APP, "geolocation", audio), false);
    assert.equal(probed, 0);
  });

  it("fails OPEN when probing or asking throws (never blocks the mic itself)", async () => {
    const throwing = createPermissionRequestHandler({
      ...allowAll,
      getMicAccessStatus: () => { throw new Error("no such API"); },
    });
    assert.equal(await grantAsync(throwing, APP, "media", audio), true);

    const rejecting = createPermissionRequestHandler({
      ...allowAll,
      getMicAccessStatus: () => "not-determined",
      askForMicAccess: async () => { throw new Error("older macOS"); },
    });
    assert.equal(await grantAsync(rejecting, APP, "media", audio), true);
  });

  it("stays synchronous with no TCC deps (non-darwin path unchanged)", () => {
    const h = createPermissionRequestHandler(allowAll);
    // No await: the callback must have fired already.
    let got;
    h(APP, "media", (v) => { got = v; }, audio);
    assert.equal(got, true);
  });

  // REGRESSION: the sinks (onDeny / callback / onMicBlocked) must not be able to
  // change the answer. A first cut put them INSIDE the promise chain, upstream of
  // a trailing `.catch(() => callback(true))` — so a throwing sink was caught
  // downstream and answered with a SECOND callback(true). Measured effect: a user
  // who explicitly REFUSED the mic was reported as having GRANTED it. The
  // exactly-once test below could not see it because its stubs never throw.
  it("keeps a REFUSAL a refusal even when the deny breadcrumb throws", async () => {
    const vals = [];
    const h = createPermissionRequestHandler({
      isAppOrigin: () => true,
      onDeny: () => { throw new Error("logDeny blew up"); },
      getMicAccessStatus: () => "not-determined",
      askForMicAccess: async () => false, // the user said NO
    });
    h(APP, "media", (v) => vals.push(v), audio);
    await new Promise((r) => setImmediate(r));
    assert.deepStrictEqual(vals, [false], "a throwing sink must never invert a refusal");
  });

  it("still answers when the recovery dialog throws (no hung getUserMedia)", async () => {
    // onMicBlocked is real Electron UI (dialog.showMessageBox) and can throw.
    // If that escapes, the permission request never settles and the renderer's
    // getUserMedia promise hangs forever — the silent dead end this module exists
    // to prevent.
    const vals = [];
    const h = createPermissionRequestHandler({
      isAppOrigin: () => true,
      onDeny: () => {},
      getMicAccessStatus: () => "denied",
      onMicBlocked: () => { throw new Error("no window"); },
    });
    h(APP, "media", (v) => vals.push(v), audio);
    await new Promise((r) => setImmediate(r));
    assert.deepStrictEqual(vals, [false]);
  });

  it("invokes the callback exactly once on every TCC path", async () => {
    const paths = [
      { getMicAccessStatus: () => "granted" },
      { getMicAccessStatus: () => "denied" },
      { getMicAccessStatus: () => "not-determined", askForMicAccess: async () => true },
      { getMicAccessStatus: () => "not-determined", askForMicAccess: async () => false },
      { getMicAccessStatus: () => { throw new Error("x"); } },
    ];
    // Each path is exercised twice: once with inert sinks, and once with sinks
    // that THROW. Electron permits the callback exactly once and throws on a
    // second invoke, and the throwing variant is what caught the real
    // double-invoke — inert stubs alone cannot see it.
    for (const deps of paths) {
      for (const hostile of [false, true]) {
        const sinks = hostile
          ? {
              isAppOrigin: () => true,
              onDeny: () => { throw new Error("sink"); },
              onMicBlocked: () => { throw new Error("sink"); },
            }
          : { ...allowAll, onMicBlocked: () => {} };
        let calls = 0;
        const h = createPermissionRequestHandler({ ...sinks, ...deps });
        h(APP, "media", () => { calls += 1; }, audio);
        await new Promise((r) => setImmediate(r));
        assert.equal(
          calls,
          1,
          `callback count for ${JSON.stringify(Object.keys(deps))} (hostile=${hostile})`,
        );
      }
    }
  });
});

describe("check and request handlers agree", () => {
  it("never lets a check veto a grant the request handler would give", () => {
    // The observed contradiction: permissions.query() said "granted" while
    // getUserMedia was refused. Same inputs must yield the same verdict.
    const check = createPermissionCheckHandler(quiet);
    const req = createPermissionRequestHandler(quiet);
    const cases = [
      [APP, ORIGIN, "audio"], [APP, ORIGIN, "unknown"], [APP, ORIGIN, undefined],
      [null, ORIGIN, "audio"], [null, ORIGIN, "unknown"], [APP, ORIGIN, "video"],
      [null, undefined, "audio"],
    ];
    for (const [wc, origin, mediaType] of cases) {
      const asCheck = check(wc, "media", origin, { mediaType });
      const asReq = grant(req, wc, "media", {
        mediaTypes: mediaType ? [mediaType] : undefined,
        securityOrigin: origin,
      });
      assert.equal(asCheck, asReq, `disagreement for wc=${!!wc} origin=${origin} mediaType=${mediaType}`);
    }
  });
});

describe("denial logging", () => {
  it("emits exactly one breadcrumb per denial and none on grant", () => {
    const seen = [];
    const deps = { onDeny: (...a) => seen.push(a) };
    const check = createPermissionCheckHandler(deps);
    check(APP, "media", ORIGIN, { mediaType: "audio" });   // grant
    assert.equal(seen.length, 0);
    check(null, "media", undefined, { mediaType: "audio" }); // deny
    assert.equal(seen.length, 1);
    assert.equal(seen[0][0], "check");
  });

  it("does NOT log the by-design refusals Chromium re-checks every navigation", () => {
    // The console flood: every route triggers geolocation / web-app-installation
    // / background-sync / media(video) checks that are all denied by design. None
    // carries diagnostic value, so none is logged.
    const seen = [];
    const deps = { isAppOrigin: () => true, onDeny: (...a) => seen.push(a) };
    const check = createPermissionCheckHandler(deps);
    for (const p of ["geolocation", "web-app-installation", "background-sync", "notifications", "midi"]) {
      check(APP, p, ORIGIN, {});
    }
    check(APP, "media", ORIGIN, { mediaType: "video" }); // camera, by design
    const req = createPermissionRequestHandler(deps);
    grant(req, APP, "geolocation", {});
    grant(req, APP, "media", { mediaTypes: ["video"] });
    assert.deepEqual(seen, [], "by-design denials must not reach the console");
  });

  it("STILL logs a media audio/unspecified denial — the mic-regression signal", () => {
    // The one class worth seeing: a media request that should have been granted
    // but was refused (foreign/absent origin). Both handlers keep logging it.
    assert.equal(isNoteworthyDenial("media", { mediaType: "audio" }), true);
    assert.equal(isNoteworthyDenial("media", {}), true);
    assert.equal(isNoteworthyDenial("media", { mediaType: "video" }), false);
    assert.equal(isNoteworthyDenial("media", { mediaTypes: ["video"] }), false);
    assert.equal(isNoteworthyDenial("geolocation", {}), false);
  });

  it("survives a destroyed webContents while logging", () => {
    const h = createPermissionCheckHandler();
    // Real logDeny path with a throwing getURL() must not raise.
    assert.equal(h(wcDestroyed(), "media", undefined, { mediaType: "audio" }), false);
  });

  it("still returns a verdict when the breadcrumb itself throws", () => {
    // logDeny JSON.stringify()s an Electron-supplied `details`, which throws on a
    // circular structure or a throwing getter. This is a synchronous Chromium
    // callback, so an escaping throw propagates into the permission check —
    // logging must never be able to decide, or break, a permission.
    const h = createPermissionCheckHandler({
      onDeny: () => { throw new Error("stringify blew up"); },
    });
    assert.equal(h(null, "media", undefined, { mediaType: "audio" }), false);

    const circular = { mediaType: "audio" };
    circular.self = circular;
    const real = createPermissionCheckHandler();
    assert.equal(real(null, "geolocation", undefined, circular), false);
  });
});
