"use strict";

// A Windows gateway left running from another install folder reads as a
// foreign port owner. resolveGatewayConflict offers a stop only once it is
// proven to be this data folder's Kiro Crew gateway.

const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert");

const { createHolderDecision } = require("../runtime/gateway/holder-decision");

const OLD_EXE = "C:\\Users\\u\\AppData\\Local\\Old Kiro Crew\\resources\\backend-dist\\python.exe";
const OLD_COMMAND = `"${OLD_EXE}" "${OLD_EXE}" -m kiro_crew gateway --no-open --port 5476`;
// The refusal names what the owner probe found on the port.
const FOREIGN_HOLDER = { foreignHolder: [{ pid: 4242, command: OLD_COMMAND }] };

function windowsHolder({
  response = 0,
  lock = { verdict: "captured", pid: 4242 },
  command = OLD_COMMAND,
  pids = [4242],
  health = { app: "kirocrew", version: "0.11.0" },
  commandAfterDialog = null,
} = {}) {
  const dialogs = [];
  const stops = [];
  const ownership = [];
  const probes = [];
  let dialogShown = false;
  const decision = createHolderDecision({
    app: { isPackaged: true, getVersion: () => "0.12.0" },
    store: { get: () => undefined },
    dialog: {
      async showMessageBox(options) { dialogs.push(options); dialogShown = true; return { response }; },
    },
    path: path.win32,
    processObj: { platform: "win32", resourcesPath: "C:\\New\\resources", env: {} },
    port: 5476,
    isWindows: true,
    log: () => {},
    quitting: () => false,
    mainWindow: () => null,
    sendStatus: () => {},
    setTimeoutFn: (fn) => setImmediate(fn),
    getRunLocalGateway: () => true,
    setGatewayOwnership: (value) => ownership.push(value),
    probeGatewayPortOwner: async (_port, { holders } = {}) => {
      holders?.push({ pid: 4242, command: OLD_COMMAND });
      return "foreign";
    },
    snapshotGatewayPortPids: async () => pids,
    probeLockHolderPid: async (options) => { probes.push(options); return lock; },
    fetchHealthInfo: async () => health,
    fetchGatewayReadiness: async () => "ready",
    waitForPortFree: async () => true,
    waitForIncumbentExit: async () => "exited",
    resolveFamilyConflict: async () => "family",
    windowsCommand: async () => (dialogShown && commandAfterDialog !== null ? commandAfterDialog : command),
    async windowsStopPid(pid, isTrustedCommand) {
      stops.push({ pid, trustsProven: isTrustedCommand(command), trustsOther: isTrustedCommand("\"C:\\x.exe\"") });
    },
  });
  return { decision, dialogs, stops, ownership, probes };
}

test("a proven older Windows gateway is named in a dialog and stopped on request", async () => {
  const { decision, dialogs, stops } = windowsHolder({ response: 0 });
  assert.strictEqual(await decision.resolveGatewayConflict(), "spawn");
  assert.strictEqual(dialogs.length, 1);
  assert.deepStrictEqual(dialogs[0].buttons, ["Stop it and restart", "Use it", "Quit"]);
  assert.match(dialogs[0].detail, /Process 4242: .*Old Kiro Crew/);
  assert.match(dialogs[0].detail, /version: 0\.11\.0 \(this app is version 0\.12\.0\)/);
  assert.deepStrictEqual(stops, [{ pid: 4242, trustsProven: true, trustsOther: false }]);
});

test("Use it adopts the proven gateway as a local one", async () => {
  const { decision, stops, ownership } = windowsHolder({ response: 1 });
  assert.strictEqual(await decision.resolveGatewayConflict(), "reuse");
  assert.deepStrictEqual(stops, []);
  assert.deepStrictEqual(ownership, ["reused-local"]);
});

test("Quit aborts and stops nothing", async () => {
  const { decision, stops } = windowsHolder({ response: 2 });
  assert.strictEqual(await decision.resolveGatewayConflict(), "abort");
  assert.deepStrictEqual(stops, []);
});

test("a gateway of the other release family is offered no Use it", async () => {
  const { decision, dialogs } = windowsHolder({ response: 1, health: { app: "kirocrew", version: "0.12.0-nightly.1" } });
  assert.strictEqual(await decision.resolveGatewayConflict(), "abort");
  assert.deepStrictEqual(dialogs[0].buttons, ["Stop it and restart", "Quit"]);
});

test("a holder that is not this home's lock holder stays foreign with no dialog", async () => {
  for (const lock of [{ verdict: "captured", pid: 7 }, { verdict: "held", pid: null }, { verdict: "refused", pid: null }]) {
    const { decision, dialogs, stops } = windowsHolder({ lock });
    assert.deepStrictEqual(await decision.resolveGatewayConflict(), FOREIGN_HOLDER);
    assert.strictEqual(dialogs.length, 0);
    assert.deepStrictEqual(stops, []);
  }
});

test("a lock holder whose command line is not a Kiro Crew server stays foreign", async () => {
  for (const command of ['"C:\\Tools\\other.exe" serve', `"${OLD_EXE}" -m kiro_crew mcp-core`, ""]) {
    const { decision, dialogs } = windowsHolder({ command });
    assert.deepStrictEqual(await decision.resolveGatewayConflict(), FOREIGN_HOLDER);
    assert.strictEqual(dialogs.length, 0);
  }
});

test("a holder that changed while the dialog was open is not stopped", async () => {
  const { decision, stops } = windowsHolder({ response: 0, commandAfterDialog: '"C:\\Tools\\other.exe" serve' });
  assert.deepStrictEqual(await decision.resolveGatewayConflict(), FOREIGN_HOLDER);
  assert.deepStrictEqual(stops, []);
});

test("the lock probe is asked about this port, so a Windows run record can name the holder", async () => {
  const { decision, probes } = windowsHolder({ response: 2 });
  await decision.resolveGatewayConflict();
  assert.ok(probes.length > 0);
  for (const options of probes) assert.deepStrictEqual(options, { port: 5476 });
});

test("a holder that changed while the dialog was open is not adopted by Use it", async () => {
  const { decision, ownership } = windowsHolder({ response: 1, commandAfterDialog: '"C:\\Tools\\other.exe" serve' });
  assert.deepStrictEqual(await decision.resolveGatewayConflict(), FOREIGN_HOLDER);
  assert.deepStrictEqual(ownership, []);
});

test("only a command line that opens with a quoted, non-empty path names an executable", async () => {
  const quoted = windowsHolder({ response: 2 });
  await quoted.decision.resolveGatewayConflict();
  assert.match(quoted.dialogs[0].detail, /Process 4242: C:\\Users\\u\\AppData\\Local\\Old Kiro Crew\\resources\\backend-dist\\python\.exe\n/);
  const args = "-m kiro_crew gateway --no-open --port 5476";
  // No space in this path, so a parser that fell back to the first word would
  // find a Kiro Crew server here; only the quoted form may name one.
  const bare = "C:\\kc\\python.exe";
  for (const command of [`${bare} ${bare} ${args}`, `"${bare} ${bare} ${args}`, `"" ${bare} ${args}`]) {
    const { decision, dialogs } = windowsHolder({ command });
    assert.deepStrictEqual(await decision.resolveGatewayConflict(), FOREIGN_HOLDER);
    assert.strictEqual(dialogs.length, 0);
  }
});
