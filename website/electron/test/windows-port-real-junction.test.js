"use strict";

// Windows-only integration probes for the two platform premises the junction
// fix rests on. Every other test in this directory fakes realpath and CIM; these
// two ask the real OS, so they run only on a Windows host (CI: the
// electron-test-windows job) and are skipped elsewhere.
//
//   1. `fs.realpathSync.native` follows an NTFS directory junction, so the two
//      spellings of one interpreter canonicalise to the same path.
//   2. However Win32_Process spells the ExecutablePath of a process launched
//      THROUGH a junction, canonicalising both sides makes the launcher's
//      resolver spelling and the reported spelling one file, and the
//      path-bound matcher accepts it.

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const {
  canonicalWindowsPath,
  windowsGatewayExecutablePaths,
  windowsProcessCommand,
} = require("../windows-port");
const { isKirocrewCommand } = require("../gateway-stop");

const IS_WIN = process.platform === "win32";
const skip = IS_WIN ? false : "real NTFS junctions and Win32_Process exist only on Windows";

// Bundle layout under one versioned root: python.exe at the tree root, the
// launcher shim under bin\ (build-desktop.sh build_backend_windows).
function makeBundle(root, version) {
  const tree = path.join(root, version, "resources", "backend-dist", "kirocrew-backend");
  fs.mkdirSync(path.join(tree, "bin"), { recursive: true });
  return tree;
}

function withScratch(fn) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "kc-junction-"));
  return Promise.resolve()
    .then(() => fn(root))
    .finally(() => fs.rmSync(root, { recursive: true, force: true }));
}

test("realpathSync.native follows a real directory junction to the versioned tree", { skip }, () => withScratch((root) => {
  const target = makeBundle(root, "0.7.0.5");
  fs.writeFileSync(path.join(target, "python.exe"), "");
  const link = path.join(root, "current");
  fs.symlinkSync(path.join(root, "0.7.0.5"), link, "junction");
  const viaLink = path.join(link, "resources", "backend-dist", "kirocrew-backend");

  const linkPython = path.join(viaLink, "python.exe");
  const targetPython = path.join(target, "python.exe");
  const canonicalLink = canonicalWindowsPath(linkPython);
  const canonicalTarget = canonicalWindowsPath(targetPython);
  assert.ok(canonicalLink, "the junction spelling resolves");
  assert.strictEqual(canonicalLink.toLowerCase(), canonicalTarget.toLowerCase(),
    "both spellings canonicalise to one file");
  assert.notStrictEqual(canonicalLink.toLowerCase(), path.win32.normalize(linkPython).toLowerCase(),
    "the junction was actually followed, not merely normalised");

  // What the launcher trusts when its resolver picked the shim through the junction.
  const trusted = windowsGatewayExecutablePaths(path.join(viaLink, "bin", "kirocrew.cmd"));
  assert.ok(
    trusted.some((candidate) => candidate.toLowerCase() === canonicalTarget.toLowerCase()),
    `trusted set names the resolved interpreter: ${JSON.stringify(trusted)}`
  );
}));

// `windowsProcessCommand`'s defaults (PowerShell 8 s, WMIC 5 s) are the
// launcher's stop-path budget: a probe that overruns them returns "" so the
// caller treats the process as foreign. This test proves the spelling
// contract, not that budget, and a hosted Windows runner's first
// Get-CimInstance call can take longer than 8 s while wmic.exe is absent on
// current runner images. The asserted probe therefore gets a CI-sized budget,
// and the CIM cold start is paid once beforehand against our own pid.
const CI_PROBE_BUDGET = { powershellTimeoutMs: 30000, wmicTimeoutMs: 15000 };
const CIM_WARMUP_BUDGET = { powershellTimeoutMs: 60000, wmicTimeoutMs: 15000 };

async function warmUpCimProvider() {
  await windowsProcessCommand(process.pid, CIM_WARMUP_BUDGET);
}

test("a process launched through a junction is matched however Win32_Process spells it", { skip }, () => withScratch(async (root) => {
  // A real interpreter is needed so the OS has a process to report. Node
  // itself stands in, copied under the name the matcher expects.
  const target = makeBundle(root, "0.7.0.5");
  const targetPython = path.join(target, "python.exe");
  fs.copyFileSync(process.execPath, targetPython);
  const link = path.join(root, "current");
  fs.symlinkSync(path.join(root, "0.7.0.5"), link, "junction");
  const viaLink = path.join(link, "resources", "backend-dist", "kirocrew-backend");
  const linkPython = path.join(viaLink, "python.exe");

  await warmUpCimProvider();

  const child = spawn(linkPython, ["-e", "setTimeout(() => {}, 120000)"], {
    stdio: "ignore",
    windowsHide: true,
  });
  try {
    await new Promise((resolve, reject) => {
      child.once("spawn", resolve);
      child.once("error", reject);
    });
    const identity = await windowsProcessCommand(child.pid, CI_PROBE_BUDGET);
    assert.ok(
      identity,
      "identity probe returned nothing within "
        + `${CI_PROBE_BUDGET.powershellTimeoutMs} ms (PowerShell timed out or failed and WMIC was unavailable)`
    );
    const reported = /^"([^"]+)"/.exec(identity);
    assert.ok(reported, `identity starts with a quoted ExecutablePath: ${identity}`);

    // The reported spelling is whatever Windows chose. Canonicalised, it is
    // the same file the resolver spelled through the junction.
    assert.strictEqual(
      canonicalWindowsPath(reported[1]).toLowerCase(),
      canonicalWindowsPath(linkPython).toLowerCase()
    );

    // The path-bound matcher, fed the real ExecutablePath spelling and the
    // gateway argv the launcher would have used, accepts the process as ours.
    // (Node cannot run `-m kiro_crew`, so the argv is synthesised; the
    // executable spelling under test is the OS's.)
    const trustedExecutablePaths = windowsGatewayExecutablePaths(
      path.join(viaLink, "bin", "kirocrew.cmd")
    );
    const command = `"${reported[1]}" "${reported[1]}" -s -m kiro_crew gateway --no-open --port 5476`;
    assert.strictEqual(
      isKirocrewCommand(command, { trustedExecutablePaths, canonicalizePath: canonicalWindowsPath }),
      true,
      `reported=${reported[1]} trusted=${JSON.stringify(trustedExecutablePaths)}`
    );
  } finally {
    child.kill();
    await new Promise((resolve) => child.once("exit", resolve));
  }
}));
