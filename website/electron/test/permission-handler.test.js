const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  isAppOrigin,
  requestsVideo,
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

  it("survives a destroyed webContents while logging", () => {
    const h = createPermissionCheckHandler();
    // Real logDeny path with a throwing getURL() must not raise.
    assert.equal(h(wcDestroyed(), "media", undefined, { mediaType: "audio" }), false);
  });
});
