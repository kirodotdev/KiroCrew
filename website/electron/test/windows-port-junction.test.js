"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const {
  canonicalWindowsPath,
  windowsGatewayExecutablePaths,
} = require("../windows-port");
const { classifyPortOwner, isKirocrewCommand } = require("../gateway-stop");

// A Toolbox-managed install launches the shell through a `current` junction.
// The launcher spells every executable through that junction; Windows reports
// the running backend by the versioned directory the junction resolved to.
const JUNCTION_ROOT = "C:\\Toolbox\\kirocrew\\current";
const VERSION_ROOT = "C:\\Toolbox\\kirocrew\\0.7.0.5";
const OLD_VERSION_ROOT = "C:\\Toolbox\\kirocrew\\0.7.0.3";
const BACKEND_TAIL = "\\resources\\backend-dist\\kirocrew-backend";
const JUNCTION_LAUNCHER = `${JUNCTION_ROOT}${BACKEND_TAIL}\\bin\\kirocrew.cmd`;
const JUNCTION_PYTHON = `${JUNCTION_ROOT}${BACKEND_TAIL}\\python.exe`;
const VERSION_PYTHON = `${VERSION_ROOT}${BACKEND_TAIL}\\python.exe`;
const OLD_VERSION_PYTHON = `${OLD_VERSION_ROOT}${BACKEND_TAIL}\\python.exe`;

// realpath that follows the `current` junction and knows no other file.
function junctionRealpath(candidate) {
  const value = String(candidate);
  if (value.toLowerCase().startsWith(JUNCTION_ROOT.toLowerCase())) {
    return VERSION_ROOT + value.slice(JUNCTION_ROOT.length);
  }
  if (value.toLowerCase().startsWith(VERSION_ROOT.toLowerCase())) return value;
  throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
}

// Win32_Process identity: ExecutablePath quoted, then the OS command line.
function gatewayCommand(executable) {
  return `"${executable}" "${executable}" -s -m kiro_crew gateway --no-open --port 5476`;
}

test("canonicalWindowsPath follows junctions and strips the long-path prefix", () => {
  assert.strictEqual(canonicalWindowsPath(JUNCTION_PYTHON, junctionRealpath), VERSION_PYTHON);
  assert.strictEqual(
    canonicalWindowsPath("D:\\x\\y.exe", () => "\\\\?\\D:\\real\\y.exe"),
    "D:\\real\\y.exe"
  );
  assert.strictEqual(canonicalWindowsPath("D:\\missing.exe", junctionRealpath), "");
  let uncRealpathCalled = false;
  assert.strictEqual(
    canonicalWindowsPath("\\\\server\\share\\python.exe", () => {
      uncRealpathCalled = true;
      throw new Error("UNC paths must not reach realpath");
    }),
    ""
  );
  assert.strictEqual(uncRealpathCalled, false);
  assert.strictEqual(canonicalWindowsPath("", junctionRealpath), "");
});

test("windowsGatewayExecutablePaths trusts both spellings of a junction-launched interpreter", () => {
  assert.deepStrictEqual(
    windowsGatewayExecutablePaths(JUNCTION_LAUNCHER, { realpathSync: junctionRealpath }),
    [JUNCTION_PYTHON, VERSION_PYTHON]
  );
  // A path realpath cannot resolve keeps only its own spelling.
  assert.deepStrictEqual(
    windowsGatewayExecutablePaths("D:\\venv\\Scripts\\kirocrew.exe", {
      realpathSync: junctionRealpath,
    }),
    [
      "D:\\venv\\Scripts\\kirocrew.exe",
      "D:\\venv\\Scripts\\python.exe",
      "D:\\venv\\python.exe",
    ]
  );
});

test("a backend reported through the junction target is the launcher's own gateway", () => {
  // Before the fix this was the bug report's `held by NON-KiroCrew` line: the
  // launcher trusted only the `current\...` spelling while CIM reported the
  // `0.7.0.5\...` directory the junction pointed at.
  const trustedExecutablePaths = windowsGatewayExecutablePaths(JUNCTION_LAUNCHER, {
    realpathSync: () => { throw new Error("ENOENT"); },
  });
  assert.deepStrictEqual(trustedExecutablePaths, [JUNCTION_PYTHON]);
  const canonicalizePath = (candidate) => canonicalWindowsPath(candidate, junctionRealpath);

  assert.strictEqual(
    isKirocrewCommand(gatewayCommand(VERSION_PYTHON), { trustedExecutablePaths }),
    false,
    "without a canonicalizer the two spellings stay distinct (path-bound default)"
  );
  assert.strictEqual(
    isKirocrewCommand(gatewayCommand(VERSION_PYTHON), { trustedExecutablePaths, canonicalizePath }),
    true,
    "the resolver's junction spelling and the reported target are one file"
  );
  assert.strictEqual(
    isKirocrewCommand(gatewayCommand(JUNCTION_PYTHON), {
      trustedExecutablePaths: [VERSION_PYTHON],
      canonicalizePath,
    }),
    true,
    "the reverse spelling (resolver already canonical, CIM through the junction) also matches"
  );
  // A canonicalizer that throws or answers nothing never widens trust.
  assert.strictEqual(
    isKirocrewCommand(gatewayCommand(OLD_VERSION_PYTHON), { trustedExecutablePaths, canonicalizePath }),
    false
  );
  assert.strictEqual(
    isKirocrewCommand(gatewayCommand(VERSION_PYTHON), {
      trustedExecutablePaths,
      canonicalizePath: () => { throw new Error("boom"); },
    }),
    false
  );
});

test("a backend at a path the resolver never selected stays foreign", () => {
  // Toolbox left a 0.7.0.3 backend running while this shell resolves to 0.7.0.5
  // through the junction. Neither spelling matches, so it is foreign: the shell
  // adopts it (same-family reuse) but never claims it or kills it.
  const trustedExecutablePaths = windowsGatewayExecutablePaths(JUNCTION_LAUNCHER, {
    realpathSync: junctionRealpath,
  });
  const canonicalizePath = (candidate) => canonicalWindowsPath(candidate, junctionRealpath);
  assert.strictEqual(
    isKirocrewCommand(gatewayCommand(OLD_VERSION_PYTHON), { trustedExecutablePaths, canonicalizePath }),
    false
  );
  assert.strictEqual(
    isKirocrewCommand(gatewayCommand("C:\\Temp\\python.exe"), { trustedExecutablePaths, canonicalizePath }),
    false
  );
  assert.strictEqual(
    isKirocrewCommand("C:\\Temp\\kirocrew.exe gateway", { trustedExecutablePaths, canonicalizePath }),
    false,
    "a matching basename at an unselected location can still never authorize a kill"
  );
});

test("classifyPortOwner recognises the junction-resolved backend as a local Kiro Crew gateway", async () => {
  const trustedExecutablePaths = [JUNCTION_PYTHON];
  const logs = [];
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [33000],
    getCommand: async () => gatewayCommand(VERSION_PYTHON),
    isKirocrew: (command) => isKirocrewCommand(command, {
      trustedExecutablePaths,
      canonicalizePath: (candidate) => canonicalWindowsPath(candidate, junctionRealpath),
    }),
    log: (line) => logs.push(line),
  });
  assert.strictEqual(owner, "kirocrew");
  // The log line quotes the existing owner vocabulary verbatim.
  assert.ok(logs.some((line) => line.includes("held by local KiroCrew pid=33000"))); // brand-ok
});
