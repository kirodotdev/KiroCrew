"use strict";
// Decision table for the cross-app gateway ownership guard. The guard's
// contract: interpose ONLY when both sides are positively identified as
// different KiroCrew identity families; every ambiguous case preserves the
// historical reuse behavior.

const { test } = require("node:test");
const assert = require("node:assert");
const { identityFamily, decideGatewayAction, FAMILY_META, HEALTH_IDENTITY_PATH } = require("../instance-guard");

test("identityFamily maps channels to bundle-identity families", () => {
  assert.equal(identityFamily("0.1.0-nightly.20260722120000"), "nightly");
  assert.equal(identityFamily("0.1.0-insider.3"), "prod");
  assert.equal(identityFamily("0.1.0"), "prod"); // stable
  assert.equal(identityFamily(""), null);
  assert.equal(identityFamily(undefined), null);
});

test("same family reuses: prod shell, prod gateway", () => {
  const d = decideGatewayAction("0.1.0", { ok: true, app: "kirocrew", version: "0.1.0-insider.3" });
  assert.equal(d.action, "reuse"); // stable shell + insider gateway = same prod identity
});

test("same family reuses: nightly shell, nightly gateway (relaunch)", () => {
  const d = decideGatewayAction("0.1.0-nightly.20260722120000", {
    ok: true, app: "kirocrew", version: "0.1.0-nightly.20260721000000",
  });
  assert.equal(d.action, "reuse");
});

test("cross family prompts: nightly shell over prod gateway", () => {
  const d = decideGatewayAction("0.2.0-nightly.20260722120000", { ok: true, app: "kirocrew", version: "0.1.0" });
  assert.equal(d.action, "takeover-prompt");
  assert.equal(d.otherFamily, "prod");
  assert.equal(d.otherVersion, "0.1.0");
});

test("cross family prompts: prod shell over nightly gateway", () => {
  const d = decideGatewayAction("0.1.0", { ok: true, app: "kirocrew", version: "0.1.0-nightly.20260722120000" });
  assert.equal(d.action, "takeover-prompt");
  assert.equal(d.otherFamily, "nightly");
});

test("legacy gateway without identity fields reuses (historical behavior)", () => {
  assert.equal(decideGatewayAction("0.1.0-nightly.20260722120000", { ok: true }).action, "reuse");
});

test("unreachable/unparseable health reuses", () => {
  assert.equal(decideGatewayAction("0.1.0-nightly.20260722120000", null).action, "reuse");
});

test("non-kirocrew responder on the port reuses (never evict a stranger)", () => {
  const d = decideGatewayAction("0.1.0-nightly.20260722120000", { ok: true, app: "other", version: "9.9.9" });
  assert.equal(d.action, "reuse");
});

test("unclassifiable own version never evicts", () => {
  assert.equal(decideGatewayAction("", { ok: true, app: "kirocrew", version: "0.1.0" }).action, "reuse");
});

test("FAMILY_META carries the app names the takeover dialog and quit-by-name need", () => {
  // Both installs deliberately share one bundle identifier, so the app NAME
  // is the only valid AppleScript targeting handle.
  assert.equal(FAMILY_META.prod.appName, "KiroCrew");
  assert.equal(FAMILY_META.nightly.appName, "KiroCrew Nightly");
});

test("identity probe targets /api/health, never the /api/status liveness URL", () => {
  // Regression pin: the shell's liveness HEALTH_URL is /api/status, whose
  // payload has no `app` field. Probing it makes decideGatewayAction classify
  // every gateway as "unidentified" and silently disables the takeover path.
  assert.equal(HEALTH_IDENTITY_PATH, "/api/health");
  assert.notEqual(HEALTH_IDENTITY_PATH, "/api/status");
});
