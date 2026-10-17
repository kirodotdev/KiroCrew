const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const {
  LOCAL_GATEWAY_KEY,
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
  classifyStartFailure,
} = require("../local-gateway");

/** Minimal electron-store stand-in: the two methods these helpers use. */
function fakeStore(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get: (key) => data[key],
    set: (key, value) => { data[key] = value; },
  };
}

test("isLocalGatewayEnabled: a store that has never held the key reads as enabled", () => {
  assert.equal(isLocalGatewayEnabled(fakeStore()), true);
});

test("isLocalGatewayEnabled: only an explicit false disables it", () => {
  assert.equal(isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: false })), false);
  assert.equal(isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: true })), true);
});

test("isLocalGatewayEnabled: a non-boolean stored value is not a request to stop", () => {
  // A hand-edited config carrying "false" or 0 is malformed, not an opt-out —
  // reading it as one would silently stop starting the gateway.
  for (const value of ["false", 0, null, "", "no"]) {
    assert.equal(
      isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: value })),
      true,
      `stored ${JSON.stringify(value)} should leave the gateway enabled`,
    );
  }
});

test("setLocalGatewayEnabled: writes a real boolean and returns what it wrote", () => {
  const store = fakeStore();
  assert.equal(setLocalGatewayEnabled(store, false), false);
  assert.equal(store.data[LOCAL_GATEWAY_KEY], false);
  assert.equal(isLocalGatewayEnabled(store), false);

  assert.equal(setLocalGatewayEnabled(store, true), true);
  assert.equal(store.data[LOCAL_GATEWAY_KEY], true);
  assert.equal(isLocalGatewayEnabled(store), true);
});

test("setLocalGatewayEnabled: coerces a truthy non-boolean rather than storing it raw", () => {
  const store = fakeStore();
  assert.equal(setLocalGatewayEnabled(store, "yes"), true);
  assert.strictEqual(store.data[LOCAL_GATEWAY_KEY], true);
});

// ── classifyStartFailure ──

test("classifyStartFailure: a disabled record is client-only", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, failure: { disabled: true, port: 5476 } }),
    "client-only",
  );
});

test("classifyStartFailure: client-only OUTRANKS a stale port-in-use log line", () => {
  // The launch log survives across launches, so a bound-port line from an
  // earlier run must not offer to force-stop a holder of a silent port.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { disabled: true, port: 5476 },
      isOwnPort: true,
      portInUseInLog: true,
    }),
    "client-only",
  );
});

test("classifyStartFailure: a refused incomplete bundle is 'installing', not a crash", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, failure: { incompleteBundle: true } }),
    "installing",
  );
});

test("classifyStartFailure: installing OUTRANKS a stale port-in-use log line", () => {
  // Nothing was spawned, so a bound-port line left by an earlier run must not
  // offer to force-stop a holder that this refusal says nothing about.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { incompleteBundle: true },
      isOwnPort: true,
      portInUseInLog: true,
    }),
    "installing",
  );
});

test("classifyStartFailure: client-only outranks an incomplete bundle", () => {
  // Both can hold at once on a client-only install that also has a partial
  // bundle; the user turned the local gateway off, so that is the real story.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { disabled: true, incompleteBundle: true },
    }),
    "client-only",
  );
});

test("classifyStartFailure: a launch refused its own port is client-only too", () => {
  // The setting is ON in these states and nothing was launched anyway, because
  // the port this launch targets is not one a new gateway may bind. The dialog
  // reads this same answer to decide whether to offer "Start Local Gateway", and
  // both messages name that button -- so a kind of "failed" here would print a
  // remedy the window does not render, and would put a crash's log pane under an
  // expected state.
  for (const reason of ["crew-configured", "foreign-holder"]) {
    assert.equal(
      classifyStartFailure({
        failedToStart: true,
        failure: { localStartBlocked: reason, port: 5476 },
      }),
      "client-only",
      reason,
    );
  }
});

test("classifyStartFailure: it outranks a stale port-in-use line here as well", () => {
  // Same reason the disabled case outranks it: nothing was launched, so an
  // "address already in use" line is left over from an earlier run and its
  // remedy -- force-stop a holder -- is aimed at a process this launch never
  // started.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { localStartBlocked: "crew-configured" },
      isOwnPort: true,
      portInUseInLog: true,
    }),
    "client-only",
  );
});

test("classifyStartFailure: a real port conflict still wins when nothing is disabled", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, isOwnPort: true, portInUseInLog: true }),
    "port-conflict",
  );
});

test("classifyStartFailure: a bound port on ANOTHER window's port is not our conflict", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, isOwnPort: false, portInUseInLog: true }),
    "failed",
  );
});

test("classifyStartFailure: a plain spawn failure and a timeout stay distinct", () => {
  assert.equal(classifyStartFailure({ failedToStart: true }), "failed");
  assert.equal(classifyStartFailure({ failedToStart: false }), "unreachable");
  assert.equal(classifyStartFailure(), "unreachable");
});

// #6138: the client-only dialog must not dress an expected state as a crash.
test("client-only: the failure dialog derives its log pane from that one bit", () => {
  // Source-level pin. The log pane is exactly the client-only condition, so a
  // second flag for it would be a duplicate spelling that can drift.
  const source = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  assert.match(source, /const showLog = !localGatewayOff;/);
  assert.doesNotMatch(source, /showLog:/);
});

test("client-only: the local-start offer routes through a re-exec on a crew's port", () => {
  // The spawn binds THIS port (`"--port", String(PORT)`), so on a port that
  // names a remote crew the escape hatch cannot start a gateway in place without
  // shadowing that crew. Port selection reads the setting once per process, so
  // the offer there means "restart and choose again". Cases the gate covers:
  // gateway on -> no offer; client-only on this launch's own port -> offer, start
  // in place; client-only on a crew's port -> offer while a re-exec is
  // possible; noRetry -> no offer.
  const source = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  assert.match(source, /const enableButton = offerLocalStart && !noRetry/);
  assert.match(source, /offerLocalStart: canOfferLocalStart\(localGatewayOff, remoteTarget\)/);
  // One named gate, so the two questions -- may we offer it, and what does it do
  // -- cannot drift into two spellings of the port condition.
  assert.match(
    source,
    /function canOfferLocalStart\([\s\S]*?return canRelaunchThisApp\(\);/,
  );
  // A failed attempt must NOT withhold the button: one lost handoff does not
  // establish that the next cannot finish, and the manual remedy the old copy
  // named re-execs the app exactly as the button does. The flag survives only to
  // word the acknowledgement, so it must not reappear in the gate.
  assert.doesNotMatch(
    source,
    /function canOfferLocalStart\([^)]*\)\s*\{[^}]*localStartRelaunchFailed/,
  );
  // A crew's port must not reach the in-place spawn: the action returns after
  // handing off to the re-exec, and only the own-port path arms runLocalGateway.
  assert.match(
    source,
    /if \(remoteTarget\) \{[\s\S]*?relaunchViaConfirmedSuccessor\([\s\S]*?\n {12}return;\n {10}\}\n {10}runLocalGateway = true;/,
  );
  assert.match(source, /remotePort: remoteConfig\?\.remotePort \|\| ""/);
  // The successor re-runs port selection and lands on a port THIS process never
  // served, so the handshake watches the port this process chose and pinned.
  // Polling this process's own port times out against a healthy successor and
  // then kills it.
  // The options stay on ONE line: splash-close.test.js slices this function's
  // body up to the first two-space-indented `}`, so a multi-line destructure
  // would end that slice at the parameter list instead of the function.
  assert.match(source, /async function relaunchViaConfirmedSuccessor\(\n {4}onFailed,\n {4}\{ expectPort = PORT, pinPort = false, restartingStatus = RESTARTING_STATUS \} = \{\},\n {2}\) \{/);
  assert.match(source, /const readyUrl = `http:\/\/localhost:\$\{expectPort\}\$\{READY_PATH\}`;/);
  assert.match(source, /await fetchGatewayReadiness\(readyUrl\)/);
  assert.match(source, /const successorPort = predictLocalPort\(\);/);
  assert.match(source, /\}, \{ expectPort: successorPort, pinPort: true, restartingStatus: RESTARTING_FOR_LOCAL_GATEWAY_STATUS \}\);/);
  // The splash is the only surface showing status during the handoff, and this
  // caller is not updating anything, so it must not borrow the update wording.
  assert.match(source, /const RESTARTING_FOR_LOCAL_GATEWAY_STATUS = "Restarting Kiro Crew to start a local gateway/);
  assert.doesNotMatch(source, /restartingStatus = RESTARTING_FOR_LOCAL_GATEWAY_STATUS/);
  // Pinning the port is what makes the watched port and the bound port one
  // value: the successor reads KIROCREW_PORT ahead of its own selection.
  assert.match(
    source,
    /if \(pinPort\) \{\n {6}spawnOptions\.env = \{ \.\.\.processObj\.env, KIROCREW_PORT: String\(expectPort\) \};/,
  );
  // A port that already has a responder cannot distinguish a successor from a
  // gateway some other install or terminal started, so confirming there would
  // exit this instance on a stranger's liveness. The test is whether ANYTHING
  // answers, not what it says: the readiness classifier folds a legacy 404 and a
  // refused connection into one "unknown", so it cannot answer this question.
  assert.match(source, /if \(await portHasAnyResponder\(readyUrl\)\) \{/);
  assert.match(source, /function portHasAnyResponder\(url\) \{[\s\S]*?req\.on\("error", \(\) => resolve\(false\)\);/);
  // Any HTTP answer is an occupied port, so the probe must not read statusCode.
  const probeStart = source.indexOf("function portHasAnyResponder(url) {");
  const probeEnd = source.indexOf("function fetchGatewayReadiness(");
  assert.ok(probeStart > 0 && probeEnd > probeStart);
  const probeBody = source.slice(probeStart, probeEnd);
  assert.match(probeBody, /resolve\(true\)/);
  assert.doesNotMatch(probeBody, /statusCode|classifyGatewayReadiness/);
  // The relaunch poll specifically must not be the bare call: that one reads this
  // process's BACKEND_URL, which is the abandoned crew port on the re-exec path.
  // Two unrelated call sites legitimately take no argument, so the probe reads
  // only this function's body. The positive assertion is the control: an empty or
  // mis-sliced region fails it rather than passing the absence check for free.
  const relaunchStart = source.indexOf("function relaunchViaConfirmedSuccessor(");
  const relaunchEnd = source.indexOf("function fetchHealthInfo(");
  assert.ok(relaunchStart > 0 && relaunchEnd > relaunchStart);
  const relaunchBody = source.slice(relaunchStart, relaunchEnd);
  assert.match(relaunchBody, /await fetchGatewayReadiness\(readyUrl\)/);
  assert.doesNotMatch(relaunchBody, /await fetchGatewayReadiness\(\);/);
  // Order is the whole point of the check: refusing after the spawn, the lock
  // release or the splash swap would already have torn this instance down. So
  // the occupancy read must sit ahead of the spawn and of every teardown step,
  // and the refusal must hand back to the caller instead of continuing.
  const occupantAt = relaunchBody.indexOf("if (await portHasAnyResponder(readyUrl)) {");
  const refusalAt = relaunchBody.indexOf('onFailed({ reason: "port-busy", port: expectPort });');
  const spawnAt = relaunchBody.indexOf("spawn(target, args, spawnOptions)");
  const releaseAt = relaunchBody.indexOf("app.releaseSingleInstanceLock()");
  const splashAt = relaunchBody.indexOf("livenessMonitor.stop()");
  assert.ok(occupantAt > 0, "the occupancy read must be inside this function");
  assert.ok(refusalAt > occupantAt, "the refusal must follow the occupancy read");
  for (const [name, at] of [["spawn", spawnAt], ["lock release", releaseAt], ["monitor stop", splashAt]]) {
    assert.ok(at > 0, `${name} must be inside this function`);
    assert.ok(occupantAt < at, `the occupancy read must precede the ${name}`);
  }
  // The ways the handoff ends must reach the caller as DIFFERENT reasons. One
  // spawned nothing and tore nothing down; one ran a successor that never served;
  // one ran a successor that DID serve, on a port whose holder could not be
  // established. Collapsing any pair tells the user something happened that did
  // not -- and collapsing the third into the second denies a restart they watched
  // while offering a button that repeats the same unreadable check.
  const busyReason = relaunchBody.indexOf('reason: "port-busy"');
  const defaultReason = relaunchBody.indexOf('failureReason = "successor-failed"');
  const unverifiableReason = relaunchBody.indexOf('failureReason: "successor-unverifiable"');
  assert.ok(busyReason > 0, "the occupied-port refusal must name its own reason");
  assert.ok(defaultReason > 0, "the served-nothing failure must name its own reason");
  assert.ok(unverifiableReason > 0, "the unreadable-probe outcome must name its own reason");
  // And the caller is told the reason the failure carries, not a literal: one
  // hardcoded string here is how the third outcome got reported as the second.
  assert.match(relaunchBody, /onFailed\(\{ reason: failureReason, port: expectPort \}\)/);
  assert.doesNotMatch(relaunchBody, /onFailed\(\{ reason: "successor-failed"/);
  // The record outlives each attempt, so whichever outcome is newest has to
  // retire the other, BOTH ways round -- the message reads the failed flag first,
  // so a leftover in either direction reports the wrong click.
  //
  // A restart that ran and lost retires an earlier busy port.
  assert.match(
    source,
    /gatewayStartFailure\.localStartFailed = true;[\s\S]{0,400}?gatewayStartFailure\.localStartPortBusy = 0;/,
  );
  // And a refusal on a busy port retires an earlier failed attempt. Without this
  // a second click that spawns nothing renders "the restarted app never served
  // one" and never names the port to free -- whose likeliest occupant is an
  // orphan from the attempt that set the flag in the first place.
  assert.match(
    source,
    /localStartPortBusy = busyPort;[\s\S]{0,700}?localStartRelaunchFailed = false;[\s\S]{0,200}?gatewayStartFailure\.localStartFailed = false;/,
  );
  // The reopened wait reuses this same record object rather than building a new
  // one, so the attempted fact has to be written here too or the message cannot
  // tell a failed attempt from an app that never offered the restart at all.
  assert.match(
    source,
    /localStartRelaunchFailed = true;[\s\S]*?gatewayStartFailure\.localStartFailed = true;/,
  );
  // And the record must NOT deny the button any more: the failure re-offers it,
  // so forcing canStartHere false here would make the message contradict the
  // window the user is looking at. The only writer of that field is the rebuild,
  // which asks the gate.
  assert.doesNotMatch(source, /gatewayStartFailure\.canStartHere = false/);
  // One predicate answers for both the button and the sentence, so the dialog
  // cannot render a button the message says is absent.
  assert.match(source, /canStartHere: canOfferLocalStart\(true, remoteHost\)/);
  // The record still has to say whether a restart already ran: the two states
  // differ only in that, and the message leads with the acknowledgement.
  assert.match(source, /localStartFailed: localStartRelaunchFailed,/);
  // Both local-start states end by asking for Start Local Gateway, so the accent
  // has to follow that sentence in BOTH. Gated on the busy port alone, the
  // failed state renders a paragraph naming one button while the orange one is a
  // different control, and the first click goes to the crew that is already
  // unreachable.
  assert.match(
    source,
    /gatewayStartFailure\?\.localStartPortBusy[\s\S]{0,120}?\|\|\s*gatewayStartFailure\?\.localStartFailed[\s\S]{0,200}?primaryAction: "enable-retry"/,
  );
  // Retry reaches the crew in every one of these states, so it names the crew in
  // every one of them. Labelling it only where the accent moves made one control
  // read as two: "Retry" in one opening of the dialog and "Retry <host>" in the
  // next, with nothing on screen to explain the difference. Still only when a
  // crew is actually named -- otherwise there is nothing to put in the label.
  assert.match(source, /retryLabel: `Retry \$\{remoteTarget\}`/);
  assert.match(source, /\.\.\.\(remoteTarget[\s\S]{0,40}?retryLabel/);
  // A state with no crew to name gets the port instead, so the slot never renders
  // a bare "Retry" whose target the surrounding sentence does not give.
  assert.match(source, /retryLabel: `Retry port \$\{PORT\}`/);
  // And each exactly once, outside the promotion block: a second copy inside it
  // would be the drift this pin exists to stop.
  assert.strictEqual((source.match(/retryLabel: `Retry \$\{remoteTarget\}`/g) || []).length, 1);
  assert.strictEqual((source.match(/retryLabel: `Retry port \$\{PORT\}`/g) || []).length, 1);
  assert.doesNotMatch(
    source,
    /primaryLabel: "Start Local Gateway",[\s\S]{0,80}?retryLabel/,
    "the label is not re-declared inside the promotion block",
  );
  // The title must name the crew's own port, not this end of the link.
  assert.match(source, /nothing answering at \$\{remoteTarget\}:\$\{remoteTargetPort\}/);
});

test("launch port: an unusable KIROCREW_PORT takes the normal decision, not the bare default", () => {
  // The default port can itself name a configured crew, so answering an
  // unparseable env value with that number bare binds the crew's port -- the
  // collision selectLaunchPort is there to avoid. Every launch with no usable
  // port of its own therefore reaches the same selection. The warning stays: the
  // value was given and ignored.
  const source = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  // The env value is resolved ABOVE the migration, because both readers need it:
  // an override is the port this launch binds, so it is also the port a legacy
  // crew must be keyed under.
  const envStart = source.indexOf("const ENV_PORT = (() => {");
  const start = source.indexOf("function resolvePort() {");
  const end = source.indexOf("const PORT = resolvePort();");
  assert.ok(envStart > 0, "the env override must be resolved once, above its readers");
  assert.ok(start > envStart && end > start, "resolvePort must follow it and precede its call site");
  const envBody = source.slice(envStart, start);
  const body = source.slice(start, end);
  // Positive assertions first: they are the control, so a mis-sliced region fails
  // here rather than passing the absence checks for free.
  assert.match(envBody, /console\.warn\('Invalid KIROCREW_PORT=/);
  // Only a usable value short-circuits selection; an unusable one reads as absent.
  // The check is isSelectablePort rather than a bare range, because the override
  // bypasses selection: a range check lets port 80 through, the shell's URL then
  // loses it, every per-port lookup misses, and the heartbeat sends the internal
  // secret to what is really a tunnelled crew.
  assert.match(envBody, /if \(isSelectablePort\(parsed\)\) return parsed;/);
  assert.doesNotMatch(envBody, /parsed >= 1 && parsed <= 65535/);
  assert.match(envBody, /return 0;/);
  assert.match(body, /if \(ENV_PORT\) return ENV_PORT;/);
  assert.match(body, /return selectLaunchPort\(\{/);
  assert.doesNotMatch(body, /return 5476/);
  // The migration has to precede selection. A legacy `remoteHost` carries no port
  // of its own, so until it is folded into `remoteHosts` selection sees no crew at
  // all and the first launch after an upgrade binds the crew's port -- the
  // shadowing this whole path removes. Both indexes are asserted positive as the
  // control, so a reader that locates neither fails rather than passing on two -1s.
  const migrateAt = source.indexOf("migrateRemoteHostConfig(store, legacyMigrationPort({");
  assert.ok(migrateAt > 0, "main must key the legacy host through legacyMigrationPort");
  assert.ok(migrateAt > envStart, "the override must be resolved before the migration reads it");
  assert.ok(start > 0, "resolvePort must be present");
  assert.ok(migrateAt < start, "the migration must run before port selection");
});
