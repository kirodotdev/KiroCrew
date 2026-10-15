// Externally-managed installs: the EXTERNALLY-MANAGED marker gate, the
// marker-driven managed lane (check, install, apply on restart), the marker
// reader and its integrity rules, and the constructed command environment.
// The shared initAutoUpdate fixture lives in auto-update-test-helpers.js.

const { test } = require("node:test");
const assert = require("node:assert");
const { initAutoUpdate, readExternallyManaged, canRewriteMarker } = require("../auto-update");

const { makeDeps } = require("./auto-update-test-helpers");

// ---------------------------------------------------------------------------
// Externally-managed marker (PEP 668 precedent). An operator/distro packager
// that owns the install's update lifecycle disables the updater outright: the
// feed is never contacted, the channel switcher loses its lane, and the About
// panel gets the marker's metadata to display instead.
// ---------------------------------------------------------------------------

test("externally-managed BARE marker returns disabled:'externally-managed' and never arms the updater", () => {
  const { deps, calls } = makeDeps({
    externallyManaged: { managedBy: "internal-registry", updateCommand: "", checkCommand: "" },
  });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "externally-managed");
  assert.strictEqual(calls.setFeedURL.length, 0, "the feed must never be contacted");
  assert.strictEqual(calls.checkForUpdates, 0);
  assert.strictEqual(deps.autoUpdater.autoDownload, undefined, "policy flags must not be applied");
  // The whole disabled surface must stay callable (ipcMain invokes every key).
  assert.strictEqual(typeof u.check, "function");
  assert.strictEqual(typeof u.download, "function");
  assert.strictEqual(typeof u.install, "function");
  assert.strictEqual(typeof u.getInfo, "function");
});

test("externally-managed getInfo carries the marker metadata and kills the switcher", () => {
  const { deps } = makeDeps({
    appVersion: "1.0.0", // bare semver stamps as 'stable' -> switchable on a normal install
    externallyManaged: { managedBy: "internal-registry", updateCommand: "pkgtool update kirocrew" },
  });
  const info = initAutoUpdate(deps).getInfo();
  assert.strictEqual(info.managedBy, "internal-registry");
  assert.strictEqual(info.updateCommand, "pkgtool update kirocrew");
  assert.strictEqual(info.channelSwitchable, false,
    "a managed install has no lane the marker's owner reads");
});

test("a self-updating install reports empty managed metadata", () => {
  const { deps } = makeDeps({ appVersion: "1.0.0" });
  const info = initAutoUpdate(deps).getInfo();
  assert.strictEqual(info.managedBy, "");
  assert.strictEqual(info.updateCommand, "");
  assert.strictEqual(info.channelSwitchable, true);
});

test("externally-managed wins over the dev gate (intentional operator override)", () => {
  const { deps } = makeDeps({
    isPackaged: false,
    externallyManaged: { managedBy: "", updateCommand: "" },
  });
  assert.strictEqual(initAutoUpdate(deps).disabled, "externally-managed");
});

// ---------------------------------------------------------------------------
// MANAGED AUTO-UPDATE (marker-driven). A marker that ALSO carries an
// updateCommand no longer disables the updater: it shells the marker's own
// commands to check and apply, never arming electron-updater or the feed. The
// commands come from the keystone-protected marker, so shelling them is trusted
// (like the Python security_policy update pins).
//
// child_process is required inside auto-update.js, so these tests stub
// child_process.spawn on the real module for the duration of the test.
// ---------------------------------------------------------------------------

const cpModule = require("node:child_process");

// Install a fake spawn that records the command and drives a scripted
// {code, out}. Returns a restore fn + the recorded command list.
function stubSpawn(script) {
  const commands = [];
  // Spawn OPTIONS per call, so a test can assert the hardened environment the
  // marker's command runs in and not only which command ran.
  const optsList = [];
  const orig = cpModule.spawn;
  const { EventEmitter } = require("node:events");
  cpModule.spawn = (command, opts) => {
    commands.push(command);
    optsList.push(opts);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const spec = (typeof script === "function" ? script(command) : script) || {};
    const { code = 0, out = "", err = "", error = false, signal = null } = spec;
    // Emit asynchronously so listeners attached after spawn() still catch it.
    setImmediate(() => {
      // `error: true` models a spawn failure (ENOENT / no shell); a `signal`
      // with a null code models a timeout kill. Both must read as "could not
      // run", distinct from a normal non-zero exit.
      if (error) { child.emit("error", new Error("spawn failed")); return; }
      if (out) child.stdout.emit("data", Buffer.from(out));
      if (err) child.stderr.emit("data", Buffer.from(err));
      child.emit("close", error ? null : code, signal);
    });
    return child;
  };
  return { commands, optsList, restore: () => { cpModule.spawn = orig; } };
}

test("managed check() with updateCommand+checkCommand emits found with the printed version", async (t) => {
  const { deps, states } = makeDeps({
    osPlatform: "win32",
    externallyManaged: {
      managedBy: "internal-registry",
      updateCommand: "pkgtool update kirocrew",
      checkCommand: "pkgtool check kirocrew",
    },
  });
  // Sibling contract: exit 0 and stdout IS the version (the packager authors
  // checkCommand to print the target version alone).
  const { commands, restore } = stubSpawn({ code: 0, out: "0.5.0.5\n" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined, "a marker with an updateCommand is NOT disabled");
  await u.check();
  assert.deepStrictEqual(commands, ["pkgtool check kirocrew"], "check must shell the checkCommand");
  const found = states.find((s) => s.state === "found");
  assert.ok(found, "an available update must surface a 'found' state");
  assert.strictEqual(found.version, "0.5.0.5", "trimmed stdout is the version");
  assert.strictEqual(
    found.installHandoff,
    "automatic-relaunch",
    "managed Windows updates run the marker command and must not promise an NSIS window",
  );
});

test("managed check(): non-zero exit -> not-available (ran, nothing new)", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ code: 1, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(states.some((s) => s.state === "not-available"), "a non-zero check is up-to-date");
  assert.ok(!states.some((s) => s.state === "found"));
});

test("managed check(): exit 0 but no version printed -> check error (not 'latest')", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ code: 0, out: "   \n" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const err = states.find((s) => s.state === "error");
  assert.ok(err && err.phase === "check", "an exit-0 empty check is a broken command, not 'latest'");
  assert.ok(!states.some((s) => s.state === "not-available"));
  assert.ok(!states.some((s) => s.state === "found"));
});

test("managed check(): command that cannot run (spawn error) -> check error (not 'latest')", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ error: true });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const err = states.find((s) => s.state === "error");
  assert.ok(err && err.phase === "check", "a check that could not run is an error, not 'up to date'");
  assert.ok(!states.some((s) => s.state === "not-available"));
});

test("managed check(): a discovered update that later clears disarms the quit-apply", async (t) => {
  let phase = "found";
  const relaunches = [];
  const { deps, appOnce, appRemoved } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => true;
  deps.app.relaunch = () => relaunches.push(true);
  const { restore } = stubSpawn(() =>
    phase === "found" ? { code: 0, out: "0.5.0.5" } : { code: 1, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check(); // discovers -> arms before-quit
  const quit = appOnce.find((r) => r.ev === "before-quit");
  assert.ok(quit, "first check arms the quit-apply");
  phase = "clear";
  await u.check(); // external manager applied/withdrew it -> nothing new
  assert.ok(appRemoved.some((r) => r.ev === "before-quit"), "the stale quit-apply must be removed");
  // The captured handler now runs, but disarm cleared foundVersion: a normal
  // quit must NOT relaunch into an update that is no longer pending.
  let prevented = false;
  quit.fn({ preventDefault: () => { prevented = true; } });
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.strictEqual(relaunches.length, 0, "a cleared update must not relaunch on quit");
});

test("managed check(): no checkCommand -> check error (cannot discover, not 'latest')", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "" },
  });
  const { commands, restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  assert.deepStrictEqual(commands, [], "no checkCommand -> nothing is shelled");
  const err = states.find((s) => s.state === "error");
  assert.ok(err && err.phase === "check", "no way to check must surface an error, not a green 'latest'");
  assert.ok(!states.some((s) => s.state === "not-available"));
});

test("managed install() runs updateCommand then relaunch+exit", async (t) => {
  const relaunches = [];
  const exits = [];
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "pkgtool update kirocrew", checkCommand: "check" },
  });
  deps.app.relaunch = () => relaunches.push(true);
  deps.app.exit = (c) => exits.push(c);
  const { commands, restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.install();
  assert.ok(commands.includes("pkgtool update kirocrew"), "install must shell the updateCommand");
  assert.ok(states.some((s) => s.state === "installing"));
  assert.strictEqual(relaunches.length, 1, "a successful install relaunches");
  assert.deepStrictEqual(exits, [0], "a successful install exits(0)");
});

test("managed install() failure emits an install-phase error and calls onInstallFailed", async (t) => {
  const failed = [];
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.onInstallFailed = () => failed.push(true);
  deps.app.relaunch = () => { throw new Error("must not relaunch on failure"); };
  const { restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 7, out: "boom" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.install();
  assert.strictEqual(failed.length, 1, "onInstallFailed must fire on a non-zero apply");
  const err = states.find((s) => s.state === "error");
  assert.ok(err && err.phase === "install", "a failed apply emits an install-phase error");
});

test("managed auto-on-restart: pref true + found arms before-quit that runs updateCommand", async (t) => {
  const relaunches = [];
  const exits = [];
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "pkgtool update kirocrew", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => true;
  deps.app.relaunch = () => relaunches.push(true);
  deps.app.exit = (c) => exits.push(c);
  const { commands, restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const quit = appOnce.find((r) => r.ev === "before-quit");
  assert.ok(quit, "pref true + found must arm a before-quit handler");
  const event = { preventDefault: () => {} };
  quit.fn(event);
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.ok(commands.includes("pkgtool update kirocrew"), "before-quit must run the updateCommand");
  assert.strictEqual(relaunches.length, 1);
  assert.deepStrictEqual(exits, [0]);
});

test("managed auto-on-restart: pref false does NOT arm before-quit", async (t) => {
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => false;
  const { restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(!appOnce.some((r) => r.ev === "before-quit"),
    "pref off -> nothing automatic; manual Install still works");
});

test("managed auto-on-restart: pref flipped OFF between check and quit is honored at quit", async (t) => {
  let pref = true;
  const relaunches = [];
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => pref;
  deps.app.relaunch = () => relaunches.push(true);
  const { commands, restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const quit = appOnce.find((r) => r.ev === "before-quit");
  assert.ok(quit, "armed while pref was true");
  pref = false; // user toggled off before quitting
  let prevented = false;
  quit.fn({ preventDefault: () => { prevented = true; } });
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.strictEqual(prevented, false, "pref read fresh at quit -> quit proceeds normally");
  assert.ok(!commands.includes("apply"), "the updateCommand must NOT run when pref is off at quit");
  assert.strictEqual(relaunches.length, 0);
});

test("managed auto-on-restart: a FAILED apply on quit exits without relaunching", async (t) => {
  const relaunches = [];
  const exits = [];
  const failed = [];
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => true;
  deps.onInstallFailed = () => failed.push(true);
  deps.app.relaunch = () => relaunches.push(true);
  deps.app.exit = (c) => exits.push(c);
  const { restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 7, out: "boom" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const quit = appOnce.find((r) => r.ev === "before-quit");
  assert.ok(quit, "pref true + found arms the quit-apply");
  quit.fn({ preventDefault: () => {} });
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.strictEqual(relaunches.length, 0, "a failed apply must NOT relaunch into an uninstalled version");
  assert.deepStrictEqual(exits, [0], "the quit is still honored (exit 0)");
  assert.strictEqual(failed.length, 1, "onInstallFailed fires on a failed quit-apply");
});

test("managed check(): the version comes from stdout only, ignoring stderr warnings", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ code: 0, out: "0.5.0.5\n", err: "WARNING: config deprecated\n" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const found = states.find((s) => s.state === "found");
  assert.ok(found, "an exit-0 check with a version on stdout is 'found'");
  assert.strictEqual(found.version, "0.5.0.5", "a stderr warning must not leak into the version");
});

test("managed download() lights the Install action (downloaded) without applying", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { commands, restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.download(); // no prior check -> discovers first
  const dl = states.find((s) => s.state === "downloaded");
  assert.ok(dl && dl.version === "0.5.0.5", "download surfaces 'downloaded' with the found version");
  assert.ok(!commands.includes("apply"), "download must NOT run the apply command");
});

// Regression: overlapping checks (launch timer, poll, Check button) each used
// to emit their own verdict, so the one finishing second demoted "downloaded"
// back to "found" and closed the ready dialog under the Install click.
test("managed check(): overlapping calls share ONE run and emit ONE verdict", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { commands, restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await Promise.all([u.check(), u.check()]);
  assert.deepStrictEqual(commands, ["check"], "a second caller must join the run in flight");
  assert.strictEqual(states.filter((s) => s.state === "checking").length, 1);
  assert.strictEqual(states.filter((s) => s.state === "found").length, 1);
});

test("managed check(): a check finishing after Download keeps the ready state", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.download();
  await u.check(); // e.g. the poll, or Check clicked again while the dialog is up
  const last = states[states.length - 1];
  assert.strictEqual(last.state, "downloaded", "the same version must not demote the ready dialog");
  assert.strictEqual(last.version, "0.5.0.5");
});

test("managed check(): a later check after Download never leaves the ready state", async (t) => {
  // The ready dialog renders only while the state is "downloaded", so even a
  // transient "checking" from a background check (launch timer, poll) closes
  // it and can swallow the Install click.
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.download();
  const readyAt = states.length;
  await u.check();
  assert.deepStrictEqual(states.slice(readyAt).map((s) => s.state).filter((s) => s !== "downloaded"), []);
});

test("managed check(): a check that fails after Download keeps the ready state", async (t) => {
  // A check error says nothing about the readied version, and emitting it
  // would replace "downloaded" and close the dialog the same way.
  let checkWorks = true;
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn(() => (checkWorks ? { code: 0, out: "0.5.0.5" } : { error: true }));
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.download();
  const readyAt = states.length;
  checkWorks = false;
  await u.check();
  assert.deepStrictEqual(states.slice(readyAt).map((s) => s.state), ["downloaded"], "a failed check must not replace the ready state");
  checkWorks = true;
  await u.check(); // and the lane still checks afterwards
  assert.strictEqual(states[states.length - 1].state, "downloaded");
});

test("managed check(): the Check button over a ready update brings the card back when the command fails", async () => {
  // The About panel's Check clears the renderer's update state before it
  // calls in, so a failed check that stays silent leaves no ready card and no
  // Install button. Each way a check can fail must re-surface "downloaded".
  for (const failure of [{ error: true }, { code: null, signal: "SIGTERM" }, { code: 0, out: "  " }]) {
    let checkWorks = true;
    const { deps, states } = makeDeps({
      externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
    });
    const { restore } = stubSpawn(() => (checkWorks ? { code: 0, out: "0.5.0.5" } : failure));
    try {
      const u = initAutoUpdate(deps);
      await u.check();
      await u.download();
      const clearedAt = states.length; // the Check button nulls the renderer's cache here
      checkWorks = false;
      await u.check();
      const after = states.slice(clearedAt);
      assert.deepStrictEqual(after.map((s) => s.state), ["downloaded"], `${JSON.stringify(failure)}: the ready state must come back`);
      assert.strictEqual(after[0].version, "0.5.0.5");
    } finally {
      restore();
    }
  }
});

test("managed check(): a check finishing during an install never replaces installing", async (t) => {
  // The launch timer or the poll can be mid-check when Install is clicked.
  let releaseStop;
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.stopGateway = () => new Promise((resolve) => { releaseStop = resolve; });
  deps.app.relaunch = () => {};
  deps.app.exit = () => {};
  let version = "0.5.0.5";
  const { restore } = stubSpawn((cmd) => (cmd === "check" ? { code: 0, out: version } : { code: 0 }));
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.download();
  version = "0.5.0.6";
  const background = u.check(); // already running when Install is clicked
  const install = u.install();
  await background;
  assert.strictEqual(states[states.length - 1].state, "installing", "the check's verdict must be dropped");
  releaseStop();
  await install;
  assert.ok(!states.some((s) => s.state === "found" && s.version === "0.5.0.6"));
});

test("managed check(): Check during an install is skipped and re-surfaces installing", async (t) => {
  let releaseStop;
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.stopGateway = () => new Promise((resolve) => { releaseStop = resolve; });
  deps.app.relaunch = () => {};
  deps.app.exit = () => {};
  const { commands, restore } = stubSpawn((cmd) => (cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0 }));
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const install = u.install();
  await u.check();
  assert.deepStrictEqual(commands, ["check"], "no check command runs mid-install");
  const last = states[states.length - 1];
  assert.strictEqual(last.state, "installing");
  assert.strictEqual(last.version, "0.5.0.5");
  releaseStop();
  await install;
});

test("managed check(): a failed install hands the state back to checks", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.app.relaunch = () => { throw new Error("must not relaunch on failure"); };
  const { restore } = stubSpawn((cmd) => (cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 7 }));
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.install();
  await u.check();
  assert.strictEqual(states[states.length - 1].state, "found", "checks resume after a failed install");
});

test("managed check(): a NEWER version after Download supersedes the ready state", async (t) => {
  let version = "0.5.0.5";
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn(() => ({ code: 0, out: version }));
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.download();
  version = "0.5.0.6";
  await u.check();
  const last = states[states.length - 1];
  assert.strictEqual(last.state, "found", "a different version is offered anew");
  assert.strictEqual(last.version, "0.5.0.6");
});

test("managed updater auto-checks on launch + arms a poll (no user action needed)", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const realST = global.setTimeout;
  const realSI = global.setInterval;
  let launchCb = null;
  let pollArmed = false;
  // Capture the scheduling done DURING synchronous init; the managed updater
  // returns before the feed path, so these are its only timers. Restore the
  // real timers immediately after init, before running the captured callback.
  global.setTimeout = (fn) => { launchCb = fn; return { unref() {} }; };
  global.setInterval = () => { pollArmed = true; return { unref() {} }; };
  const { commands, restore } = stubSpawn({ code: 0, out: "0.5.0.6" });
  t.after(() => { global.setTimeout = realST; global.setInterval = realSI; restore(); });
  initAutoUpdate(deps);
  global.setTimeout = realST;
  global.setInterval = realSI;
  assert.strictEqual(typeof launchCb, "function", "a launch check must be scheduled automatically");
  assert.ok(pollArmed, "a background polling interval must be armed");
  launchCb();
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.deepStrictEqual(commands, ["check"], "the scheduled launch check shells checkCommand");
  const found = states.find((s) => s.state === "found");
  assert.ok(found && found.version === "0.5.0.6", "the background check discovers the update on its own");
});

test("readExternallyManaged: absent marker -> null", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  assert.strictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), null);
});

test("readExternallyManaged: JSON marker carries metadata", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(dir, "EXTERNALLY-MANAGED"),
    JSON.stringify({ managedBy: "internal-registry", updateCommand: "pkgtool update kirocrew" }),
  );
  // probeMarkerRewritable: this test is about PARSING a trusted marker; the
  // integrity gate itself is covered by the writability tests below.
  assert.deepStrictEqual(readExternallyManaged({
    env: {},
    resourcesPath: dir,
    probeMarkerRewritable: () => false,
  }), {
    managedBy: "internal-registry",
    updateCommand: "pkgtool update kirocrew",
    checkCommand: "",
  });
});

test("readExternallyManaged: bare/unparsable marker still means managed", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, "EXTERNALLY-MANAGED"), "not json {");
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), {
    managedBy: "",
    updateCommand: "",
    checkCommand: "",
  });
});

test("readExternallyManaged: degenerate markers (oversized, symlink, directory) mean managed, no metadata", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  // Oversized: presence still wins, the body is never read into memory.
  const big = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(big, { recursive: true, force: true }));
  fs.writeFileSync(path.join(big, "EXTERNALLY-MANAGED"), "x".repeat(9000));
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: big }), {
    managedBy: "",
    updateCommand: "",
    checkCommand: "",
  });
  // Symlink (even dangling): lstat'ed, never followed — a link into a FIFO or
  // device must not be able to stall this startup-path read.
  const sym = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(sym, { recursive: true, force: true }));
  try {
    fs.symlinkSync(path.join(sym, "nowhere"), path.join(sym, "EXTERNALLY-MANAGED"));
    assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: sym }), {
      managedBy: "",
      updateCommand: "",
      checkCommand: "",
    });
  } catch (err) {
    // Ordinary Windows accounts may lack SeCreateSymbolicLinkPrivilege. Keep
    // the oversized and directory cases live, and omit only the setup this
    // host cannot perform; capable Windows hosts still exercise the assertion.
    if (process.platform !== "win32" || !["EPERM", "EACCES"].includes(err?.code)) {
      throw err;
    }
    t.diagnostic("symlink assertion omitted: host cannot create symlinks");
  }
  // Directory named like the marker: present = managed, nothing to parse.
  const dirCase = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dirCase, { recursive: true, force: true }));
  fs.mkdirSync(path.join(dirCase, "EXTERNALLY-MANAGED"));
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dirCase }), {
    managedBy: "",
    updateCommand: "",
    checkCommand: "",
  });
});

test("readExternallyManaged: metadata fields are length-capped", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(dir, "EXTERNALLY-MANAGED"),
    JSON.stringify({ managedBy: "m".repeat(500), updateCommand: "c".repeat(2000), checkCommand: "k".repeat(2000) }),
  );
  const got = readExternallyManaged({
    env: {},
    resourcesPath: dir,
    probeMarkerRewritable: () => false,
  });
  assert.strictEqual(got.managedBy.length, 128);
  assert.strictEqual(got.updateCommand.length, 512);
  assert.strictEqual(got.checkCommand.length, 512);
});

test("readExternallyManaged: env override points at a marker file", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const marker = path.join(dir, "custom-marker.json");
  fs.writeFileSync(marker, JSON.stringify({ managedBy: "harness", updateCommand: "" }));
  const got = readExternallyManaged({
    env: { KIROCREW_EXTERNALLY_MANAGED: marker },
    resourcesPath: "/nonexistent",
    probeMarkerRewritable: () => false,
  });
  assert.deepStrictEqual(got, { managedBy: "harness", updateCommand: "", checkCommand: "" });
});

// ---------------------------------------------------------------------------
// Marker INTEGRITY. The marker's updateCommand/checkCommand are shelled, and the
// background launch check fires them with no user action, so the marker is an
// execution trust root: one file write under a user-writable <resourcesPath>
// (Homebrew, `pip --user`, ~/Applications) would otherwise be arbitrary code
// execution in the desktop app. These exercise the REAL probe -- no injected
// seam -- because a test that only greps for the call would execute none of it.
// ---------------------------------------------------------------------------

// Root can chmod and rewrite anything, so the probe correctly answers
// "rewritable" for every path and a trusted-marker case has nothing to assert.
// Windows has no POSIX owner to read and is declared fail-closed.
const canTestOwnership = process.platform !== "win32"
  && typeof process.geteuid === "function" && process.geteuid() !== 0;

// A real path this account does NOT own and cannot chmod, used as the trusted
// marker case. Resolved from the filesystem rather than hard-coded so the test
// asserts only when its own precondition genuinely holds.
function foreignOwnedPath() {
  if (!canTestOwnership) return "";
  const fs = require("node:fs");
  const path = require("node:path");
  const euid = process.geteuid();
  for (const candidate of ["/usr/bin/env", "/bin/sh", "/usr/lib/os-release"]) {
    try {
      const st = fs.lstatSync(candidate);
      const dir = fs.lstatSync(path.dirname(candidate));
      if (st.uid !== euid && dir.uid !== euid
        && (st.mode & 0o022) === 0 && (dir.mode & 0o022) === 0) return candidate;
    } catch {
      // try the next candidate
    }
  }
  return "";
}

test("readExternallyManaged: a marker in a USER-WRITABLE dir yields NO metadata", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(dir, "EXTERNALLY-MANAGED"),
    JSON.stringify({
      managedBy: "attacker",
      updateCommand: "/bin/sh -c 'touch /tmp/pwned'",
      checkCommand: "/bin/sh -c 'touch /tmp/pwned'",
    }),
  );
  // Still MANAGED (the updater stays off) but the commands are refused: this is
  // the historical bare-marker shape.
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), {
    managedBy: "",
    updateCommand: "",
    checkCommand: "",
  });
});

test("readExternallyManaged: chmod 0400 on an OWNED marker does not buy trust", (t) => {
  // The bypass the mode-bit version of this gate had: the owner can always
  // chmod +w back, so read-only-right-now is not provenance. An agent shell
  // plants the marker, makes it 0400 in a 0500 dir, and must still be refused.
  if (!canTestOwnership) {
    t.skip("needs a non-root POSIX host: root can rewrite anything");
    return;
  }
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => {
    fs.chmodSync(dir, 0o700);
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const marker = path.join(dir, "EXTERNALLY-MANAGED");
  fs.writeFileSync(marker, JSON.stringify({
    managedBy: "attacker",
    updateCommand: "/bin/sh -c 'touch /tmp/pwned'",
    checkCommand: "/bin/sh -c 'touch /tmp/pwned'",
  }));
  fs.chmodSync(marker, 0o400); // "not writable" -- but still ours
  fs.chmodSync(dir, 0o500);    // ditto
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), {
    managedBy: "",
    updateCommand: "",
    checkCommand: "",
  });
});

// --- Baked marker: shipped inside the app's own code ------------------------
//
// A marker that lives in app.asar next to main.js has the application's own
// provenance: nothing can rewrite it without also being able to rewrite the
// code that reads it. So it is trusted WITHOUT the ownership probe, on every
// platform, and it outranks a loose marker dropped beside the app afterwards.
// These tests pin all three properties, plus the degenerate shapes.

function bakedFixture(t, body) {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "kc-baked-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const codeDir = path.join(root, "app-code");
  const resources = path.join(root, "resources");
  fs.mkdirSync(codeDir);
  fs.mkdirSync(resources);
  const baked = path.join(codeDir, "EXTERNALLY-MANAGED");
  if (body !== undefined) fs.writeFileSync(baked, body);
  return { root, codeDir, resources, baked };
}

test("baked marker: trusted as code -- the ownership probe is never consulted", (t) => {
  // The user-owned, writable file that canRewriteMarker would refuse: exactly
  // the shape a Toolbox / Homebrew / ~/Applications install has for EVERY file
  // in the app, main.js included. Baked, it is honored anyway.
  const { baked, resources } = bakedFixture(t, JSON.stringify({
    managedBy: "Builder Toolbox",
    updateCommand: "/opt/toolbox/bin/toolbox update kirocrew",
    checkCommand: "/opt/toolbox/bin/kirocrew-update-check",
  }));
  let probed = 0;
  assert.deepStrictEqual(readExternallyManaged({
    env: {},
    resourcesPath: resources,
    bakedMarkerPath: baked, isPackaged: true,
    probeMarkerRewritable: () => { probed += 1; return true; },
  }), {
    managedBy: "Builder Toolbox",
    updateCommand: "/opt/toolbox/bin/toolbox update kirocrew",
    checkCommand: "/opt/toolbox/bin/kirocrew-update-check",
  });
  assert.strictEqual(probed, 0, "a baked marker is not subject to the loose-marker provenance probe");
});

test("baked marker: read on a PACKAGED build only -- a dev checkout's source dir is not an archive", (t) => {
  // In a dev checkout `__dirname` is writable source, not app.asar, so a file
  // there has none of the provenance the baked trust rests on -- and the managed
  // lane arms its launch timer before the dev-disable gate, so a planted file
  // would be shelled. Unpackaged: the baked path is not read at all, and the
  // loose marker keeps its own gated contract.
  const fs = require("node:fs");
  const path = require("node:path");
  const { baked, resources } = bakedFixture(t, JSON.stringify({
    managedBy: "planted", updateCommand: "/usr/bin/planted-update",
  }));
  assert.strictEqual(readExternallyManaged({
    env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: false,
  }), null, "unpackaged: no baked read, no loose marker -> not managed");
  fs.writeFileSync(path.join(resources, "EXTERNALLY-MANAGED"), JSON.stringify({ managedBy: "loose" }));
  assert.deepStrictEqual(readExternallyManaged({
    env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: false,
    probeMarkerRewritable: () => false,
  }), { managedBy: "loose", updateCommand: "", checkCommand: "" }, "unpackaged: the loose path is what is read");
});

test("baked marker: honored with the REAL probe on a user-owned tree (the Windows/Toolbox shape)", (t) => {
  // No injected probe: canRewriteMarker itself runs, and on this host the
  // fixture is ours (or we are root, or on Windows) -- every arm of that probe
  // answers "rewritable". The baked path must not ask it. This is the property
  // that brings the commands back on Windows, where the probe is fail-closed by
  // declaration.
  const { baked, resources } = bakedFixture(t, JSON.stringify({
    managedBy: "pkgtool", updateCommand: "/usr/bin/pkgtool update",
  }));
  assert.strictEqual(canRewriteMarker(baked), true, "precondition: the probe WOULD refuse this file");
  assert.deepStrictEqual(readExternallyManaged({
    env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: true,
  }), { managedBy: "pkgtool", updateCommand: "/usr/bin/pkgtool update", checkCommand: "" });
});

test("baked marker: outranks a loose marker when both exist", (t) => {
  // A build-time declaration by the edition beats a file dropped later --
  // including a trusted-looking loose one. The loose body must not leak into
  // any field.
  const fs = require("node:fs");
  const path = require("node:path");
  const { baked, resources } = bakedFixture(t, JSON.stringify({
    managedBy: "edition", updateCommand: "/usr/bin/edition-update",
  }));
  fs.writeFileSync(path.join(resources, "EXTERNALLY-MANAGED"), JSON.stringify({
    managedBy: "loose", updateCommand: "/usr/bin/loose-update", checkCommand: "/usr/bin/loose-check",
  }));
  assert.deepStrictEqual(readExternallyManaged({
    env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: true,
    probeMarkerRewritable: () => false, // even a loose marker that WOULD pass
  }), { managedBy: "edition", updateCommand: "/usr/bin/edition-update", checkCommand: "" });
});

test("baked marker: absent -> the loose marker keeps its gated behavior", (t) => {
  // The default build ships no baked marker, so the pre-existing contract must
  // be untouched: a loose marker is read, and its metadata still depends on the
  // provenance probe.
  const fs = require("node:fs");
  const path = require("node:path");
  const { baked, resources } = bakedFixture(t /* no body: file absent */);
  assert.strictEqual(readExternallyManaged({
    env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: true,
  }), null, "neither marker present: not managed");
  fs.writeFileSync(path.join(resources, "EXTERNALLY-MANAGED"), JSON.stringify({
    managedBy: "loose", updateCommand: "/usr/bin/loose-update",
  }));
  assert.deepStrictEqual(readExternallyManaged({
    env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: true, probeMarkerRewritable: () => true,
  }), { managedBy: "", updateCommand: "", checkCommand: "" }, "rewritable loose marker: bare");
  assert.deepStrictEqual(readExternallyManaged({
    env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: true, probeMarkerRewritable: () => false,
  }), { managedBy: "loose", updateCommand: "/usr/bin/loose-update", checkCommand: "" }, "trusted loose marker: metadata");
});

test("baked marker: degenerate bodies still mean managed, with nothing to run", (t) => {
  // Same fail-safe as the loose shape: a baked marker that is empty, unparsable,
  // over-cap or not a regular file leaves the updater OFF and yields no command.
  // A build that mis-writes its marker must not fall back to self-updating.
  const fs = require("node:fs");
  const bare = { managedBy: "", updateCommand: "", checkCommand: "" };
  for (const body of ["", "not json", "[1,2]", "x".repeat(8193)]) {
    const { baked, resources } = bakedFixture(t, body);
    assert.deepStrictEqual(readExternallyManaged({
      env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: true,
    }), bare, `body ${JSON.stringify(body.slice(0, 12))}`);
  }
  const { baked, resources } = bakedFixture(t);
  fs.mkdirSync(baked); // a directory at the marker's name
  assert.deepStrictEqual(readExternallyManaged({
    env: {}, resourcesPath: resources, bakedMarkerPath: baked, isPackaged: true,
  }), bare, "directory at the baked path");
});

test("baked marker: the dev/test env seam still wins, and stays a LOOSE read", (t) => {
  // KIROCREW_EXTERNALLY_MANAGED (unpackaged only) names the file the harness
  // wants exercised; a baked marker must not pre-empt it, and the env-named
  // file keeps the provenance probe -- the seam exercises the loose path.
  const fs = require("node:fs");
  const path = require("node:path");
  const { baked, resources, root } = bakedFixture(t, JSON.stringify({ managedBy: "baked" }));
  const envMarker = path.join(root, "env-marker");
  fs.writeFileSync(envMarker, JSON.stringify({ managedBy: "env", updateCommand: "/usr/bin/x" }));
  let probedPath = "";
  assert.deepStrictEqual(readExternallyManaged({
    env: { KIROCREW_EXTERNALLY_MANAGED: envMarker },
    isPackaged: false,
    resourcesPath: resources,
    bakedMarkerPath: baked,
    probeMarkerRewritable: (p) => { probedPath = p; return false; },
  }), { managedBy: "env", updateCommand: "/usr/bin/x", checkCommand: "" });
  assert.strictEqual(probedPath, envMarker);
});

test("canRewriteMarker: a marker we do NOT own and cannot chmod is trusted", (t) => {
  // The positive half: without this the gate could refuse everything and still
  // pass every negative test. Asserted against a real foreign-owned path (the
  // shape a root-owned system install has) rather than a fabricated one.
  const foreign = foreignOwnedPath();
  if (!foreign) {
    t.skip("no foreign-owned path available on this host");
    return;
  }
  assert.strictEqual(canRewriteMarker(foreign), false,
    `${foreign} is owned by another account in a directory we cannot write`);
});

test("canRewriteMarker: ownership is what is probed, not the mode bits", (t) => {
  if (!canTestOwnership) {
    t.skip("needs a non-root POSIX host: root can rewrite anything");
    return;
  }
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => {
    fs.chmodSync(dir, 0o700);
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const marker = path.join(dir, "EXTERNALLY-MANAGED");
  fs.writeFileSync(marker, "{}");
  // Every mode an owner can set still answers "rewritable", because chmod is
  // ours: 0600 (plainly writable), 0400 (read-only file), 0000 (no bits at all).
  for (const mode of [0o600, 0o400, 0o000]) {
    fs.chmodSync(marker, mode);
    assert.strictEqual(canRewriteMarker(marker), true, `mode ${mode.toString(8)} is still ours`);
  }
  fs.chmodSync(marker, 0o600);
  // A marker that is absent cannot have its provenance established either.
  assert.strictEqual(canRewriteMarker(path.join(dir, "nope")), true);
});

test("canRewriteMarker: an ACL-granted write is rewritable even at a safe mode", (t) => {
  // POSIX mode bits do not model ACLs, so ownership + mode alone would call a
  // root-owned 0755 dir carrying a `chmod +a`/setfacl grant for this user
  // "not rewritable". A two-uid ACL fixture is not constructible in a test (it
  // needs a directory this account does not own), so the arm is asserted as a
  // DECLARED property, the same way the win32 branch below is.
  const js = require("node:fs").readFileSync(require.resolve("../auto-update"), "utf8");
  const probe = js.slice(js.indexOf("function canRewriteMarker"));
  assert.match(probe.slice(0, probe.indexOf("\n}")),
    /fs\.accessSync\(target, fs\.constants\.W_OK\);\s*\n\s*return true;/,
    "the probe must ask the kernel, not only the mode bits");
  // And the arm must not over-refuse: a foreign-owned path with no grant to us
  // still reads as trusted (covered positively above, re-asserted here against
  // the same subject so a broadened arm cannot pass silently).
  const foreign = foreignOwnedPath();
  if (!foreign) {
    t.diagnostic("no foreign-owned path available to re-assert the trusted case");
    return;
  }
  assert.strictEqual(canRewriteMarker(foreign), false);
});

test("canRewriteMarker: Windows is fail-closed by declaration", () => {
  // No POSIX owner to read and access(W_OK) does not model ACLs, so there is no
  // honest verdict; every Windows marker is refused. Asserted as a DECLARED
  // property so it cannot silently become an accident.
  const js = require("node:fs").readFileSync(require.resolve("../auto-update"), "utf8");
  assert.match(js, /if \(process\.platform === "win32" \|\| typeof process\.geteuid !== "function"\) return true;/,
    "the win32 branch must return 'rewritable' before any stat is attempted");
  if (process.platform === "win32") {
    const os = require("node:os");
    assert.strictEqual(canRewriteMarker(require("node:path").join(os.tmpdir(), "EXTERNALLY-MANAGED")), true);
  }
});

test("readExternallyManaged: a PACKAGED app ignores KIROCREW_EXTERNALLY_MANAGED", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "kc-res-"));
  t.after(() => {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(empty, { recursive: true, force: true });
  });
  const marker = path.join(dir, "custom-marker.json");
  fs.writeFileSync(marker, JSON.stringify({ managedBy: "env", updateCommand: "/bin/false" }));
  // The env var names a marker the probe would trust, yet a packaged app must
  // not consult it at all: the launch environment is user-writable.
  assert.strictEqual(
    readExternallyManaged({
      env: { KIROCREW_EXTERNALLY_MANAGED: marker },
      resourcesPath: empty,
      isPackaged: true,
      probeMarkerRewritable: () => false,
    }),
    null,
    "packaged: the env seam is off and the real resources dir has no marker",
  );
  // Unpackaged (the harness case) still honors it.
  assert.deepStrictEqual(
    readExternallyManaged({
      env: { KIROCREW_EXTERNALLY_MANAGED: marker },
      resourcesPath: empty,
      isPackaged: false,
      probeMarkerRewritable: () => false,
    }),
    { managedBy: "env", updateCommand: "/bin/false", checkCommand: "" },
  );
});

test("managed updater: a rewritable marker never arms the background check", async (t) => {
  // End-to-end wiring: the launch timer fires managedCheck() 30s after start
  // with no user action, so an unverified marker must never reach that path.
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(dir, "EXTERNALLY-MANAGED"),
    JSON.stringify({
      managedBy: "attacker",
      updateCommand: "/bin/sh -c 'touch /tmp/pwned'",
      checkCommand: "/bin/sh -c 'touch /tmp/pwned'",
    }),
  );
  // externallyManaged left UNSET so the module reads the real marker from disk.
  const { deps } = makeDeps({ resourcesPath: dir });
  delete deps.externallyManaged;
  const realST = global.setTimeout;
  const realSI = global.setInterval;
  let timerArmed = false;
  let pollArmed = false;
  global.setTimeout = (fn, ms) => { timerArmed = true; return realST(fn, ms); };
  global.setInterval = (fn, ms) => { pollArmed = true; return realSI(fn, ms); };
  const { commands, restore } = stubSpawn({ code: 0, out: "9.9.9" });
  t.after(() => { global.setTimeout = realST; global.setInterval = realSI; restore(); });
  const u = initAutoUpdate(deps);
  global.setTimeout = realST;
  global.setInterval = realSI;
  assert.strictEqual(u.disabled, "externally-managed",
    "a rewritable marker is managed-with-no-metadata, so the updater is off");
  assert.strictEqual(timerArmed, false, "no launch check may be scheduled");
  assert.strictEqual(pollArmed, false, "no background poll may be armed");
  // And the explicit surfaces are inert too.
  await u.check();
  await u.download();
  await u.install();
  assert.deepStrictEqual(commands, [], "no marker command may ever be shelled");
});

// Everything a shell reads as code. The managed command's environment is
// CONSTRUCTED, so none of these can reach it whether or not it is named here --
// this list is the adversary's side of the contract, not the implementation's.
const SHELL_CODE_VARS = [
  "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE", "PYTHONUSERBASE",
  "PYTHONWARNINGS", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
  "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "BASH_ENV", "ENV", "SHELLOPTS", "PS4",
  "IFS", "NODE_OPTIONS", "BASHOPTS", "PERL5OPT", "RUBYOPT",
  "BASH_FUNC_check%%", "BASH_FUNC_apply()", "BASH_FUNC_grep%%",
  // Not shell-interpreted, but an interpreter reads code from it: Python's
  // user-site dir comes from HOME, so a planted sitecustomize.py runs on every
  // `python` start.
  "HOME",
];

test("managed command env is CONSTRUCTED: nothing the shell reads as code is inherited", async (t) => {
  // A narrowed PATH stops a planted shim shadowing a command NAME. These go
  // further: an exported shell function shadows the name outright, PS4 under
  // SHELLOPTS=xtrace runs a command substitution before the command does, and
  // the loader family makes even a trusted absolute binary load planted code.
  // Because the env is built by naming what is ALLOWED, this also covers the
  // names nobody has thought of yet.
  const saved = new Map(SHELL_CODE_VARS.map((k) => [k, process.env[k]]));
  for (const k of SHELL_CODE_VARS) process.env[k] = "() { echo pwned; }";
  const savedProbe = process.env.KC_TEST_UNLISTED_PROBE;
  const savedLang = process.env.LANG;
  process.env.KC_TEST_UNLISTED_PROBE = "an-unlisted-variable";
  process.env.LANG = "C.UTF-8";
  t.after(() => {
    for (const [k, v] of saved) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    if (savedProbe === undefined) delete process.env.KC_TEST_UNLISTED_PROBE;
    else process.env.KC_TEST_UNLISTED_PROBE = savedProbe;
    if (savedLang === undefined) delete process.env.LANG;
    else process.env.LANG = savedLang;
  });
  const { deps } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "/usr/bin/apply", checkCommand: "/usr/bin/check" },
  });
  const { optsList, restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  assert.strictEqual(optsList.length, 1, "the check shelled exactly one command");
  const env = optsList[0].env;
  for (const k of SHELL_CODE_VARS) {
    assert.ok(!(k in env), `${k} must not reach the managed command`);
  }
  assert.ok(!Object.keys(env).some((k) => k.startsWith("BASH_FUNC_")),
    "no exported shell function may survive");
  // Constructed, not filtered: an UNLISTED variable is absent because it was
  // never copied. This is the assertion that holds against the next name.
  assert.ok(!("KC_TEST_UNLISTED_PROBE" in env),
    "an unlisted variable must be absent by construction, not by denylist");
  // The declared pass-through still arrives, or a packager's updater breaks.
  assert.strictEqual(env.LANG, "C.UTF-8", "declared pass-through vars must survive");
  // The rest of the hardened environment is unchanged.
  assert.ok(env.PATH && !env.PATH.includes(require("node:os").homedir()),
    "PATH stays the narrowed system one");
  // SET, not merely withheld: HOME is excluded so Python cannot find a planted
  // user-site, but on Windows that directory derives from APPDATA, which IS
  // passed through. Telling the interpreter directly closes both spellings.
  assert.strictEqual(env.PYTHONNOUSERSITE, "1",
    "a Python updater must never import a user-site sitecustomize, on any platform");
  // The SHELL is pinned, not discovered from the environment. On POSIX Node
  // resolves /bin/sh by path, so `true` is already pinned; on Windows the
  // system cmd.exe is named by path, because `shell: true` there reads the
  // user-level ComSpec -- the same injection class the env construction closes.
  const shellOpt = optsList[0].shell;
  if (process.platform === "win32") {
    assert.match(String(shellOpt), /\\System32\\cmd\.exe$/i, "win32 shell must be the system cmd.exe by path");
  } else {
    assert.strictEqual(shellOpt, true);
  }
  assert.ok(!("COMSPEC" in env) && !("ComSpec" in env),
    "the app's COMSPEC is not inherited (cmd.exe sets the child's own to the pinned shell)");
  // Derived, not inherited: the launching executable's absolute path, from
  // process.execPath (kernel command line), never from process.env.
  assert.strictEqual(env.KIROCREW_MANAGED_ARGV0, process.execPath);
  // The managed lane owns the executor: its spawn and its pinned shell live together.
  const src = require("node:fs").readFileSync(require.resolve("../runtime/update/managed-lane"), "utf8");
  assert.match(src, /shell: managedShell\(\)/, "spawn must take its shell from managedShell()");
  assert.match(src, /System32\\\\cmd\.exe`/, "managedShell() must pin the system cmd.exe on win32");
  assert.strictEqual(optsList[0].cwd, "/");
});
