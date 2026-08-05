/**
 * The two rules that keep an instance switch from churning windows.
 *
 * Both are the SAME class of bug the tri-state `mochiEnabledState` was created for
 * ("Tearing down on a failed probe is what made the pet appear to crash every few
 * seconds"), reappearing one layer down:
 *
 *  1. KEEP vs FALLBACK — a NON-answer must change nothing. Falling back to self on
 *     a timeout flips the resolved target, and a flipped target destroys and
 *     rebuilds every Mochi window — then does it again when the link recovers. One
 *     slow tick would cost the user their chat panel twice.
 *  2. IDENTITY, not origin — local ports are recycled, so two different instances
 *     can present the same `localhost:<port>`. Comparing origins alone reads that
 *     as "no change".
 *
 * main.js cannot be imported (it boots Electron), so the decisions are mirrored
 * here and a source guard asserts the real code still makes them.
 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

/** Mirror of the reconcile switch test. */
function switched(prev, next) {
  return prev.instanceId !== next.instanceId || prev.baseUrl !== next.baseUrl;
}

test("a recycled port with a DIFFERENT instance counts as switched", () => {
  // Instance a dies, releases 7778; instance b connects and is handed 7778.
  const prev = { instanceId: "a", baseUrl: "http://localhost:7778" };
  const next = { instanceId: "b", baseUrl: "http://localhost:7778" };
  assert.strictEqual(
    switched(prev, next),
    true,
    "same origin, different gateway — comparing origins alone would miss this",
  );
});

test("the same instance on the same origin is NOT switched", () => {
  const same = { instanceId: "a", baseUrl: "http://localhost:7778" };
  assert.strictEqual(switched(same, { ...same }), false);
});

test("the same instance that moved port IS switched", () => {
  // A reconnect can land on a different free port.
  assert.strictEqual(
    switched(
      { instanceId: "a", baseUrl: "http://localhost:7778" },
      { instanceId: "a", baseUrl: "http://localhost:7779" },
    ),
    true,
  );
});

test("remote -> self and self -> remote are both switched", () => {
  const self = { instanceId: "self", baseUrl: "http://localhost:5476" };
  const remote = { instanceId: "a", baseUrl: "http://localhost:7778" };
  assert.strictEqual(switched(self, remote), true);
  assert.strictEqual(switched(remote, self), true);
});

/**
 * Mirror of the FIRST decision in resolveMochiTarget: settings -> keep | choice.
 *
 * `mochiSettings()` returns null for every non-answer (5s timeout, non-200, lost
 * token, malformed JSON), and none of those mean "the user chose self". An OBJECT
 * without `petInstance` is a real answer and does mean self.
 */
function settingsOutcome(settings) {
  if (settings === null || settings === undefined) return "keep";
  return typeof settings.petInstance === "string" ? settings.petInstance || "self" : "self";
}

test("a non-answer about the settings keeps the current target", () => {
  for (const nonAnswer of [null, undefined]) {
    assert.strictEqual(
      settingsOutcome(nonAnswer),
      "keep",
      "a timed-out settings read flipped the target and tore down the panel",
    );
  }
});

test("settings that ANSWER without a chosen instance resolve to self", () => {
  // The distinction the null guard must not blur: read fine, nothing chosen.
  assert.strictEqual(settingsOutcome({}), "self");
  assert.strictEqual(settingsOutcome({ petInstance: "" }), "self");
  assert.strictEqual(settingsOutcome({ petInstance: "self" }), "self");
  assert.strictEqual(settingsOutcome({ petInstance: 42 }), "self");
});

test("a chosen instance id is carried through", () => {
  assert.strictEqual(settingsOutcome({ petInstance: "abc" }), "abc");
});

// ── source guards: the real code must still make these decisions ──────────

const MAIN = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");

test("resolveMochiTarget still has a keep outcome for non-answers", () => {
  const start = MAIN.indexOf("async function resolveMochiTarget(");
  assert.ok(start !== -1, "resolveMochiTarget must exist");
  const body = MAIN.slice(start, MAIN.indexOf("\n}", start));
  assert.ok(body.includes("keep: true"), "the keep outcome was removed");
  // Both non-answer sites must still return keep rather than self.
  assert.ok(
    body.includes("!listed.known"),
    "an unreadable instance list must not fall back to self",
  );
  assert.ok(
    body.includes("!conn.known"),
    "an unanswered connect must not fall back to self",
  );
  // The FIRST non-answer is the settings read itself: `mochiSettings()` returns
  // null for a timeout, a non-200, a lost token and malformed JSON. Collapsing
  // that to an empty choice fell through to `return self` — the same flip, with
  // the user's unsent draft in the panel it tore down.
  const nullGuard = body.indexOf("settings === null");
  assert.ok(nullGuard !== -1, "a null settings read must return keep, not self");
  assert.ok(
    nullGuard < body.indexOf("settings.petInstance"),
    "the null check must come BEFORE the choice is read, or it cannot prevent the fallback",
  );
});

test("the reconcile switch still compares the instance id", () => {
  assert.ok(
    /mochiPetInstanceId !== target\.instanceId/.test(MAIN),
    "the switch test stopped comparing identity — recycled ports would slip through",
  );
});

test("a keep outcome must not write the cached target", () => {
  // The writes have to sit INSIDE the `if (!target.keep)` block; otherwise keep
  // would still flip the variables the accelerator handlers read.
  const guard = MAIN.indexOf("if (!target.keep)");
  assert.ok(guard !== -1, "the keep guard was removed");
  const assign = MAIN.indexOf("mochiPetInstanceId = target.instanceId");
  assert.ok(assign > guard, "the target assignment escaped the keep guard");
});

test("the enabled cache is pruned and has a freshness check", () => {
  assert.ok(MAIN.includes("function pruneRemoteEnabledCache("), "cache eviction was removed");
  assert.ok(MAIN.includes("function hasFreshEnabled("), "the freshness skip was removed");
  assert.ok(
    /!hasFreshEnabled\(i\.id\)/.test(MAIN),
    "the probe stopped skipping fresh entries — polling would re-connect every cycle",
  );
});

test("hideAll is reset on an instance switch", () => {
  const guard = MAIN.indexOf("if (switched)");
  assert.ok(guard !== -1);
  const block = MAIN.slice(guard, MAIN.indexOf("\n  }", guard));
  assert.ok(
    block.includes("mochiWindowsHidden = false"),
    "rebuilt overlays come up visible, so the hideAll flag must be cleared",
  );
});

test("bindPanelIpc no longer clears the panel token", () => {
  const src = fs.readFileSync(path.join(__dirname, "..", "panelWindow.js"), "utf8");
  const start = src.indexOf("function bindPanelIpc(");
  const body = src.slice(start, src.indexOf("ipcBound = true;", start));
  assert.ok(
    !body.includes("setPanelTarget("),
    "bindPanelIpc must not route through setPanelTarget — its token default would clear a just-set token",
  );
});
