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
 * Mirror of the FIRST decision in resolveMochiTarget: the stored pointer -> target.
 *
 * The pointer now comes from the SHELL's own store (machineStore), not from the
 * gateway's Mochi settings — so there is no "could not read it" case left at this
 * step, which is exactly what lets a remote pet outlive a local disable. `self` and
 * anything blank still mean this computer.
 */
function pointerOutcome(choice) {
  return typeof choice === "string" && choice.trim() ? choice : "self";
}

test("a blank or absent pointer resolves to self", () => {
  for (const blank of [null, undefined, "", "   ", 42]) {
    assert.strictEqual(pointerOutcome(blank), "self", `${JSON.stringify(blank)}`);
  }
});

test("a chosen instance id is carried through", () => {
  assert.strictEqual(pointerOutcome("abc"), "abc");
  assert.strictEqual(pointerOutcome("self"), "self");
});

// ── source guards: the real code must still make these decisions ──────────

const MAIN = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");

test("resolveMochiTarget still has a keep outcome for non-answers", () => {
  const start = MAIN.indexOf("async function resolveMochiTarget(");
  assert.ok(start !== -1, "resolveMochiTarget must exist");
  const body = MAIN.slice(start, MAIN.indexOf("\n}", start));
  assert.ok(body.includes("keep: true"), "the keep outcome was removed");
  // Both remaining non-answer sites must still return keep rather than self.
  assert.ok(
    body.includes("!listed.known"),
    "an unreadable instance list must not fall back to self",
  );
  assert.ok(
    body.includes("!conn.known"),
    "an unanswered connect must not fall back to self",
  );
});

test("the pointer comes from the SHELL's store, never from a gateway read", () => {
  // The move that fixes three defects at once (one-way door, pointer dying with
  // the app, no restart survival). Asserted on the shipped source because all
  // three failures were architectural: a future edit that reads `petInstance`
  // back off `mochiSettings()` restores every one of them while the behavioural
  // tests above keep passing.
  assert.ok(
    /resolveMochiTarget\(petInstanceOf\(machineStore\)\)/.test(MAIN),
    "reconcile must resolve from the shell store's pointer",
  );
  assert.ok(
    !/settings\.petInstance/.test(MAIN),
    "index.js must not read petInstance out of the gateway's Mochi settings again",
  );
  // `mochiSettings()` survives for ONE purpose only — the one-shot migration.
  const settingsCalls = MAIN.match(/await mochiSettings\(\)/g) || [];
  assert.strictEqual(
    settingsCalls.length,
    1,
    "the only remaining gateway settings read should be the one-shot migration",
  );
  assert.ok(
    /migrateMachinePrefs\(machineStore, await mochiSettings\(\)\)/.test(MAIN),
    "the surviving settings read must be the migration one",
  );
});

test("teardown is decided AFTER the resolve, not before it", () => {
  // The ordering IS the bug: deciding on the host's disabled flag first and
  // resolving second is what let a local disable remove a pet that a remote was
  // still serving. Nothing about the resolve needs the host's Mochi to be on —
  // core's /api/instances and the remote's own /api/apps are both outside that
  // gate — so there is no reason left to decide first.
  const resolveAt = MAIN.indexOf("const target = await resolveMochiTarget(");
  const teardownAt = MAIN.indexOf('state === "disabled" && hostDisabledMeansTeardown(');
  assert.ok(resolveAt !== -1, "the resolve call must exist");
  assert.ok(teardownAt !== -1, "the teardown must be gated on hostDisabledMeansTeardown");
  assert.ok(
    resolveAt < teardownAt,
    "resolving after the teardown decision cannot inform it — that is the original defect",
  );
});

test("a REFUSED accelerator is not persisted over the working one", () => {
  // Registration is the only availability test, so a rebind must bind before it
  // stores. Persisting first left the store holding a key the OS rejected: the
  // action then had no working accelerator at all, and closing Settings kept it
  // that way. Keeping the previous value means the next drift check rebinds
  // something that works.
  const applyAt = MAIN.indexOf('ipcMain.handle("mochi-shortcuts:apply"');
  assert.ok(applyAt !== -1, "the shortcuts apply handler must exist");
  const body = MAIN.slice(applyAt, applyAt + 2500);
  const bindAt = body.indexOf("applyMochiShortcuts({ ...prev, ...desired })");
  const persistAt = body.indexOf("setShortcutsIn(machineStore, keep,");
  assert.ok(bindAt !== -1, "the rebind must be attempted over the previous values");
  assert.ok(persistAt !== -1, "only the accepted values may be written back");
  assert.ok(bindAt < persistAt, "binding must come first — it is the availability test");
  assert.ok(
    /keep = mergeAcceptedShortcuts\(/.test(body),
    "the write-back set must come from mergeAcceptedShortcuts, which tolerates an unset store",
  );
  assert.ok(
    !/prev\[action\]/.test(body),
    "indexing `prev` inline throws when the store has no accelerators yet",
  );
  assert.ok(
    !/setShortcutsIn\(machineStore, desired,/.test(body),
    "writing the raw request back would store a refused key",
  );
});

test("a switch reports where the pet LANDED, not that reconcile returned", () => {
  // Almost every way a switch fails is a silent, non-throwing return: reconcile
  // bails out when the host's enabled-state probe is unreadable, and
  // resolveMochiTarget falls back to self when the chosen instance is
  // listed-but-down, unlisted, unusable, or has Mochi off. A hardcoded ok:true
  // therefore closed Settings over a pet that never moved — the success-path
  // twin of the failure message that used to claim nothing was saved.
  const setAt = MAIN.indexOf('ipcMain.handle("mochi-instances:set"');
  assert.ok(setAt !== -1, "the set handler must exist");
  // Generous window: the handler carries a long rationale comment, and slicing
  // too tightly would pass by simply not reaching the return statement.
  const body = MAIN.slice(setAt, setAt + 4000);
  assert.ok(
    /return \{ ok: mochiPetInstanceId === saved, petInstance: saved \}/.test(body),
    "the handler must compare the SHOWN instance against the saved pointer",
  );
  assert.ok(
    !/return \{ ok: true, petInstance: saved \}/.test(body),
    "an unconditional ok:true cannot distinguish a switch from a silent fallback",
  );
});

test("the accelerators are bound from the shell store too", () => {
  // Same argument as the pointer, and leaving this one behind would reproduce the
  // bug in miniature: custom keys would silently revert to defaults the moment the
  // host's Mochi was switched off.
  assert.ok(
    /applyMochiShortcuts\(shortcutsOf\(machineStore\)\)/.test(MAIN),
    "reconcile must bind the shell store's accelerators",
  );
  assert.ok(
    !/mochiShortcutsOf\(/.test(MAIN),
    "the gateway-backed shortcuts reader should be gone, not merely unused",
  );
});

test("the instance list keeps its four states across the IPC boundary", () => {
  // `disabled` (multi-instance off) and `inactive` (needs restart) carry the ONLY
  // guidance this pane gives. An earlier version returned {known, instances} and
  // let the renderer rebuild the view, which erased both — so every desktop user
  // with the feature off saw just "This computer" and no way forward. Asserted on
  // the shipped source because the loss was silent: the pane still rendered, just
  // without the two states that tell the user what to do.
  const start = MAIN.indexOf("function fetchInstances(");
  assert.ok(start !== -1, "fetchInstances must exist");
  const body = MAIN.slice(start, MAIN.indexOf("\n}", start));
  for (const state of ["disabled", "inactive", "ready", "error"]) {
    assert.ok(body.includes(`"${state}"`), `fetchInstances stopped reporting "${state}"`);
  }
  // 403 is the "feature is off" answer, and `active:false` is "needs restart" —
  // both must map to their own state rather than to an empty ready list.
  assert.ok(/statusCode === 403[\s\S]{0,160}"disabled"/.test(body), "403 must map to disabled");
  assert.ok(/active === false[\s\S]{0,80}"inactive"/.test(body), "active:false must map to inactive");
  // The handler must pass the state through rather than re-deriving it.
  assert.ok(
    /state: listed\.state/.test(MAIN),
    "the list IPC must forward fetchInstances' state, not recompute it",
  );
});

test("both IPC write paths record the write as user intent", () => {
  // The migration guard is only as good as the intent it can see: an IPC write
  // that forgot `byUser` would look imported, and a delayed migration would
  // happily revert it.
  assert.ok(
    /setPetInstanceIn\(machineStore, instanceId, \{ byUser: true \}\)/.test(MAIN),
    "the set-instance IPC must mark the write as user intent",
  );
  assert.ok(
    // `keep`, not `desired`: the handler writes back only the accelerators the
    // OS accepted (see the refused-accelerator test above). What matters here is
    // that whatever it does write is still marked as user intent.
    /setShortcutsIn\(machineStore, keep, \{ byUser: true \}\)/.test(MAIN),
    "the shortcuts-apply IPC must mark the write as user intent",
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
