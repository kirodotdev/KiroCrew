const { test } = require("node:test");
const assert = require("node:assert");
const {
  createPermissionRequestHandler,
  createPermissionCheckHandler,
} = require("../permission-handler");

// The embedded browser view (browser-view.js) shares session.defaultSession with
// the dashboard, and `isAppOrigin` treats ANY localhost origin as the app. These
// tests pin the rule that closes the resulting hole: an untrusted view is denied
// by IDENTITY, before the origin heuristic can grant it anything.

const LOCALHOST_WC = { getURL: () => "http://localhost:5173/" };
const silent = () => {};

function requestVerdict(handler, wc, permission, details) {
  let granted = null;
  handler(wc, permission, (g) => { granted = g; }, details);
  return granted;
}

test("untrusted view is denied the microphone even on a localhost origin", () => {
  const handler = createPermissionRequestHandler({
    isUntrusted: (wc) => wc === LOCALHOST_WC,
    onDeny: silent,
  });
  assert.strictEqual(
    requestVerdict(handler, LOCALHOST_WC, "media", { mediaTypes: ["audio"] }),
    false,
  );
});

test("the dashboard still gets the microphone (the mic fix is not regressed)", () => {
  const handler = createPermissionRequestHandler({
    isUntrusted: () => false,
    onDeny: silent,
  });
  assert.strictEqual(
    requestVerdict(handler, LOCALHOST_WC, "media", { mediaTypes: ["audio"] }),
    true,
  );
  // Unspecified mediaTypes must still pass — that was the original bug.
  assert.strictEqual(requestVerdict(handler, LOCALHOST_WC, "media", {}), true);
});

test("untrusted view is denied every other permission type too", () => {
  const handler = createPermissionRequestHandler({
    isUntrusted: () => true,
    onDeny: silent,
  });
  for (const p of ["geolocation", "notifications", "midi", "clipboard-read", "media"]) {
    assert.strictEqual(requestVerdict(handler, LOCALHOST_WC, p, {}), false, `${p} denied`);
  }
});

test("check handler mirrors the request handler for untrusted views", () => {
  const untrusted = createPermissionCheckHandler({
    isUntrusted: () => true,
    onDeny: silent,
  });
  const trusted = createPermissionCheckHandler({
    isUntrusted: () => false,
    onDeny: silent,
  });
  assert.strictEqual(
    untrusted(LOCALHOST_WC, "media", "http://localhost:5173/", { mediaType: "audio" }),
    false,
  );
  assert.strictEqual(
    trusted(LOCALHOST_WC, "media", "http://localhost:5173/", { mediaType: "audio" }),
    true,
  );
});

test("camera stays denied for the dashboard as well", () => {
  const handler = createPermissionRequestHandler({ isUntrusted: () => false, onDeny: silent });
  assert.strictEqual(
    requestVerdict(handler, LOCALHOST_WC, "media", { mediaTypes: ["audio", "video"] }),
    false,
  );
});

test("omitting isUntrusted preserves the pre-existing behaviour", () => {
  // Default dep is () => false, so callers that never pass it are unaffected.
  const handler = createPermissionRequestHandler({ onDeny: silent });
  assert.strictEqual(
    requestVerdict(handler, LOCALHOST_WC, "media", { mediaTypes: ["audio"] }),
    true,
  );
});
