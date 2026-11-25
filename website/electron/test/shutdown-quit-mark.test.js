// A Quit after the gateway this app spawned has already crashed (its child
// cleared, no replacement yet) is still a stop the user chose: the gateway's next
// boot must end the YOLO and chat trust it keeps across every other exit.
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { createShutdown } = require("../runtime/gateway/shutdown");

function shutdownWith(ownership) {
  return createShutdown({
    fs,
    path,
    http: {},
    processObj: process,
    port: 5476,
    backendUrl: "http://127.0.0.1:5476",
    isWindows: false,
    log: () => {},
    setTimeoutFn: setTimeout,
    getGatewayProcess: () => null,
    setGatewayProcess: () => {},
    setSpawnedExecutablePaths: () => {},
    getRunLocalGateway: () => true,
    getGatewayOwnership: () => ownership,
  });
}

test("a Quit after the spawned gateway crashed writes the user-stop mark", async (t) => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "kc-quit-mark-"));
  const previous = process.env.KIROCREW_HOME;
  process.env.KIROCREW_HOME = home;
  t.after(() => {
    if (previous === undefined) delete process.env.KIROCREW_HOME;
    else process.env.KIROCREW_HOME = previous;
    fs.rmSync(home, { recursive: true, force: true });
  });
  const mark = path.join(home, "kept-grants", "user-stop");

  await shutdownWith("reused-local").stopGatewayGracefully({ userStop: true });
  assert.strictEqual(fs.existsSync(mark), false, "an adopted gateway is not this Quit's to end");

  await shutdownWith("spawned").stopGatewayGracefully({ userStop: false });
  assert.strictEqual(fs.existsSync(mark), false, "a stop before an update keeps the grants");

  await shutdownWith("spawned").stopGatewayGracefully({ userStop: true });
  assert.strictEqual(fs.existsSync(mark), true);
});
