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

// A local KiroCrew LISTEN owner is what makes an eviction legitimate. Passing
// it explicitly in the cross-family cases keeps those tests about FAMILY logic.
const LOCAL = { localOwner: "kirocrew" };

test("cross family prompts: nightly shell over prod gateway", () => {
  const d = decideGatewayAction("0.2.0-nightly.20260722120000", { ok: true, app: "kirocrew", version: "0.1.0" }, LOCAL);
  assert.equal(d.action, "takeover-prompt");
  assert.equal(d.otherFamily, "prod");
  assert.equal(d.otherVersion, "0.1.0");
});

test("cross family prompts: prod shell over nightly gateway", () => {
  const d = decideGatewayAction("0.1.0", { ok: true, app: "kirocrew", version: "0.1.0-nightly.20260722120000" }, LOCAL);
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

// ── Locality: a cross-family payload alone must never authorise an eviction ──
// An `ssh -L 5476:localhost:5476 host` forward makes a REMOTE gateway answer
// /api/health on localhost with a payload identical to a local install's. Every
// case below is cross-family (the family logic says "evict") and must still
// reuse, because the port is not held by a local KiroCrew process.
const CROSS_FAMILY_HEALTH = { ok: true, app: "kirocrew", version: "0.1.0" };
const NIGHTLY_SHELL = "0.2.0-nightly.20260722120000";

test("tunnelled remote gateway is reused, never evicted (ssh owns the socket)", () => {
  const d = decideGatewayAction(NIGHTLY_SHELL, CROSS_FAMILY_HEALTH, { localOwner: "foreign" });
  assert.equal(d.action, "reuse");
  assert.match(d.reason, /non-local-holder:foreign/);
});

test("no visible local listener is reused, never evicted", () => {
  const d = decideGatewayAction(NIGHTLY_SHELL, CROSS_FAMILY_HEALTH, { localOwner: "none" });
  assert.equal(d.action, "reuse");
  assert.match(d.reason, /non-local-holder:none/);
});

test("a failed owner probe fails SAFE: unknown never evicts", () => {
  // "couldn't look" must never be mistaken for "safe to kill".
  const d = decideGatewayAction(NIGHTLY_SHELL, CROSS_FAMILY_HEALTH, { localOwner: "unknown" });
  assert.equal(d.action, "reuse");
  assert.match(d.reason, /non-local-holder:unknown/);
});

test("omitting the locality input defaults to no eviction", () => {
  // A caller that forgets the third argument must not inherit the old
  // evict-on-payload-alone behavior.
  assert.equal(decideGatewayAction(NIGHTLY_SHELL, CROSS_FAMILY_HEALTH).action, "reuse");
});

test("a genuine local rival install still prompts (cross-app mutex preserved)", () => {
  // Regression pin for the case the takeover was built for (#193): two installs
  // on ONE machine sharing ~/.kiro/crew and :5476. The guard must keep working.
  const d = decideGatewayAction(NIGHTLY_SHELL, CROSS_FAMILY_HEALTH, { localOwner: "kirocrew" });
  assert.equal(d.action, "takeover-prompt");
  assert.equal(d.otherFamily, "prod");
});

test("locality is checked only after family — same-family still short-circuits", () => {
  // A same-family gateway reuses for the family reason regardless of owner, so
  // the reason string stays useful for diagnosing reuse causes.
  const d = decideGatewayAction("0.1.0", { ok: true, app: "kirocrew", version: "0.1.0-insider.3" }, { localOwner: "foreign" });
  assert.equal(d.action, "reuse");
  assert.equal(d.reason, "same-family");
});

test("FAMILY_META separates display names from quit-by-name targets", () => {
  // Both installs deliberately share one bundle identifier, so the app NAME
  // is the only valid AppleScript targeting handle.
  assert.equal(FAMILY_META.prod.appName, "KiroCrew");
  assert.equal(FAMILY_META.nightly.appName, "KiroCrew Nightly");
  assert.equal(FAMILY_META.prod.displayName, "Kiro Crew");
  assert.equal(FAMILY_META.nightly.displayName, "Kiro Crew Nightly");
});

test("identity probe targets /api/health, never the /api/status liveness URL", () => {
  // Regression pin: the shell's liveness HEALTH_URL is /api/status, whose
  // payload has no `app` field. Probing it makes decideGatewayAction classify
  // every gateway as "unidentified" and silently disables the takeover path.
  assert.equal(HEALTH_IDENTITY_PATH, "/api/health");
  assert.notEqual(HEALTH_IDENTITY_PATH, "/api/status");
});
