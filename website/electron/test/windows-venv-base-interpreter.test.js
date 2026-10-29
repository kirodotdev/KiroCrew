"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const { windowsVenvBaseInterpreterLaunches } = require("../windows-port");
const { classifyPortOwner, isKirocrewCommand, isKirocrewGatewayCommand } = require("../gateway-stop");

// A source or pip venv on Windows: `Scripts\python.exe` is a redirector that
// starts the base interpreter `pyvenv.cfg` names, so the gateway's listener
// runs C:\Python312\python.exe with this venv's console launcher as its script.
const VENV = "D:\\src\\kc\\.venv";
const LAUNCHER = `${VENV}\\Scripts\\kirocrew.exe`;
const REDIRECTOR = `${VENV}\\Scripts\\python.exe`;
const BASE = "C:\\Python312\\python.exe";
const PYVENV = `${VENV}\\pyvenv.cfg`;
const PYVENV_TEXT = [
  "home = C:\\Python312",
  "include-system-site-packages = false",
  "version = 3.12.10",
  "executable = C:\\Python312\\python.exe",
  "command = C:\\Python312\\python.exe -m venv D:\\src\\kc\\.venv",
  "",
].join("\r\n");

function readFrom(files) {
  return (file) => {
    const key = Object.keys(files).find((name) => name.toLowerCase() === String(file).toLowerCase());
    if (key === undefined) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
    return files[key];
  };
}

// realpath for a host with no junctions: every path resolves to itself.
const identityRealpath = (candidate) => String(candidate);

function launchesFor(bin, files = { [PYVENV]: PYVENV_TEXT }) {
  return windowsVenvBaseInterpreterLaunches(bin, {
    readFileSync: readFrom(files),
    realpathSync: identityRealpath,
    pathEnv: "",
  });
}

function trusts(command, launches = launchesFor(LAUNCHER)) {
  return isKirocrewCommand(command, {
    trustedExecutablePaths: [LAUNCHER, REDIRECTOR, `${VENV}\\python.exe`],
    trustedVenvLaunches: launches,
    canonicalizePath: identityRealpath,
  });
}

// Win32_Process identity: ExecutablePath quoted, then the OS command line.
const listener = (argv0, script = LAUNCHER, rest = "gateway --no-open --port 5599") =>
  `"${BASE}" "${argv0}" "${script}" ${rest}`;

test("a venv launcher's pyvenv.cfg names the base interpreter it hands the gateway to", () => {
  assert.deepStrictEqual(launchesFor(LAUNCHER), [{
    interpreters: [BASE],
    launchers: [LAUNCHER],
    venvInterpreters: [REDIRECTOR],
  }]);
  // Without an `executable` key the interpreter is python.exe in `home`.
  const homeOnly = launchesFor(LAUNCHER, { [PYVENV]: "home = E:\\Py\\3.12\nversion = 3.12.1\n" });
  assert.deepStrictEqual(homeOnly[0].interpreters, ["E:\\Py\\3.12\\python.exe"]);
  // A moved base install leaves `executable` stale; the redirector reads `home`.
  const moved = launchesFor(LAUNCHER, {
    [PYVENV]: "home = E:\\Py\\3.12\nexecutable = C:\\Python312\\python.exe\n",
  });
  assert.deepStrictEqual(moved[0].interpreters, ["E:\\Py\\3.12\\python.exe", BASE]);
});

test("no venv launch is derived where nothing names a venv base interpreter", () => {
  // No pyvenv.cfg: a global install's Scripts launcher, already trusted by path.
  assert.deepStrictEqual(launchesFor(LAUNCHER, {}), []);
  // The bundled .cmd shim and a bare executable are not console launchers.
  assert.deepStrictEqual(launchesFor("C:\\App\\backend-dist\\kirocrew-backend\\bin\\kirocrew.cmd"), []);
  assert.deepStrictEqual(launchesFor(`${VENV}\\kirocrew.exe`), []);
  // A pyvenv.cfg naming no drive-letter python*.exe is not evidence.
  for (const text of [
    "home = \\\\server\\share\\py\n",
    "executable = C:\\Python312\\cmd.exe\n",
    "home = relative\\py\n",
    "version = 3.12\n",
  ]) {
    assert.deepStrictEqual(launchesFor(LAUNCHER, { [PYVENV]: text }), [], text);
  }
});

test("the venv's base interpreter running its own launcher with a server subcommand is ours", () => {
  for (const command of [
    listener(BASE),
    listener(REDIRECTOR),
    `"${BASE}" ${BASE} ${LAUNCHER} gateway --no-open --port 5599`,
    listener(BASE.toLowerCase(), LAUNCHER.toUpperCase()),
    listener(BASE, LAUNCHER, "start"),
  ]) {
    assert.strictEqual(trusts(command), true, command);
    assert.strictEqual(
      isKirocrewGatewayCommand(command, { trustedVenvLaunches: launchesFor(LAUNCHER) }),
      true,
      command,
    );
  }
});

test("the shared base interpreter running anything else stays foreign", () => {
  const otherVenvLauncher = "D:\\other\\.venv\\Scripts\\kirocrew.exe";
  for (const command of [
    // Another Python program on the same interpreter.
    `"${BASE}" "${BASE}" C:\\tools\\serve.py --port 5599`,
    `"${BASE}" "${BASE}" -m http.server 5599`,
    `"${BASE}" "${BASE}" -c "import socket" "${LAUNCHER}" gateway`,
    `"${BASE}" "${BASE}" -m kiro_crew gateway --port 5599`,
    // A kirocrew-named script that is not this venv's launcher.
    listener(BASE, "C:\\Users\\x\\Downloads\\kirocrew.exe"),
    listener(BASE, otherVenvLauncher),
    listener(BASE, "D:\\src\\kc\\.venv\\Scripts\\kirocrew.py"),
    // An option between argv0 and the launcher.
    `"${BASE}" "${BASE}" -X dev "${LAUNCHER}" gateway`,
    // Not a server subcommand: the gateway's own MCP children.
    listener(BASE, LAUNCHER, "mcp-core"),
    listener(BASE, LAUNCHER, ""),
    // An argv0 from another venv.
    listener("D:\\other\\.venv\\Scripts\\python.exe"),
    // The launcher spelled as argv0: Python then runs `.\gateway` as its script.
    `"${BASE}" "${LAUNCHER}" gateway --no-open --port 5599`,
    `"${BASE}" "${LAUNCHER}" gateway "${LAUNCHER}" gateway`,
    // Argument boundaries the C runtime reads differently than a naive split.
    `"${BASE}" "${BASE}" "${LAUNCHER}"gateway`,
    `"${BASE}" "${BASE}" "${LAUNCHER}" "gateway"x`,
    `"${BASE}" "${BASE}"x "${LAUNCHER}" gateway`,
    `"${BASE}" "${BASE}" "${LAUNCHER}\\" gateway`,
    `"${BASE}" "${BASE}" ${LAUNCHER}"" gateway`,
    `"${BASE}" "${BASE}" "${LAUNCHER} gateway`,
    // A character the runtime keeps inside an argument but `\s` splits on:
    // argv0 would swallow the launcher and Python would run `.\gateway`.
    ...["\u000B", "\f", "\n", "\r", "\u00A0", "\u3000", "\u2028"].flatMap((char) => [
      `"${BASE}" ${BASE}${char}${LAUNCHER} gateway`,
      `"${BASE}" "${BASE}"${char}"${LAUNCHER}" gateway`,
      `"${BASE}" "${BASE}" "${LAUNCHER}"${char}gateway`,
    ]),
    // CommandLine starting with whitespace shifts its argv0 to "".
    `"${BASE}"  "${BASE}" "${LAUNCHER}" gateway`,
    `"${BASE}" \t"${BASE}" "${LAUNCHER}" gateway`,
    ` "${BASE}" "${BASE}" "${LAUNCHER}" gateway`,
  ]) {
    assert.strictEqual(trusts(command), false, command);
  }
});

test("a different interpreter, or a launch derived from another venv, is not trusted", () => {
  // Same launcher argv, but run by an interpreter pyvenv.cfg does not name.
  assert.strictEqual(trusts(`"C:\\Python311\\python.exe" "${LAUNCHER}" gateway`), false);
  // The resolver selected another venv: this venv's listener is not its launch.
  const otherVenv = "D:\\other\\.venv";
  const otherLaunches = launchesFor(`${otherVenv}\\Scripts\\kirocrew.exe`, {
    [`${otherVenv}\\pyvenv.cfg`]: PYVENV_TEXT,
  });
  assert.strictEqual(trusts(listener(BASE), otherLaunches), false);
  // No venv launch at all is today's path-bound rule: the base interpreter is foreign.
  assert.strictEqual(trusts(listener(BASE), []), false);
});

test("a junction-reached base interpreter matches by its resolved spelling", () => {
  const junction = (candidate) => String(candidate).replace(/^C:\\Python312\\/i, "C:\\Py\\3.12.10\\");
  const launches = windowsVenvBaseInterpreterLaunches(LAUNCHER, {
    readFileSync: readFrom({ [PYVENV]: PYVENV_TEXT }),
    realpathSync: junction,
    pathEnv: "",
  });
  assert.deepStrictEqual(launches[0].interpreters, [BASE, "C:\\Py\\3.12.10\\python.exe"]);
  const command = `"C:\\Py\\3.12.10\\python.exe" "${BASE}" "${LAUNCHER}" gateway --port 5599`;
  assert.strictEqual(
    isKirocrewCommand(command, { trustedVenvLaunches: launches, canonicalizePath: junction }),
    true,
  );
});

test("the port-owner probe classifies the venv listener as ours and everything else as foreign", async () => {
  const launches = launchesFor(LAUNCHER);
  const classify = (command) => classifyPortOwner(5599, {
    getListenPids: async () => [4242],
    getCommand: async () => command,
    isKirocrew: (cmd) => isKirocrewCommand(cmd, {
      trustedExecutablePaths: [LAUNCHER, REDIRECTOR],
      trustedVenvLaunches: launches,
      canonicalizePath: identityRealpath,
    }),
  });
  assert.strictEqual(await classify(listener(BASE)), "kirocrew");
  assert.strictEqual(await classify(`"${BASE}" "${BASE}" C:\\tools\\serve.py`), "foreign");
  assert.strictEqual(await classify(listener(BASE, "D:\\other\\.venv\\Scripts\\kirocrew.exe")), "foreign");
});
