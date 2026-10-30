/**
 * Windows-native port ownership and process-control adapters.
 *
 * The parsing and identity checks live outside main.js so they can be tested
 * on every CI host. Runtime effects are limited to netstat, PowerShell/WMIC,
 * and taskkill, with execFile injection for deterministic tests.
 */

const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");

const FALLBACK_WINDOWS_ROOT = "C:\\Windows";
const CONFIGURED_WINDOWS_ROOT = process.platform === "win32"
  ? process.env.SystemRoot || process.env.WINDIR || FALLBACK_WINDOWS_ROOT
  : FALLBACK_WINDOWS_ROOT;

function windowsSystemToolPaths(
  systemRoot = CONFIGURED_WINDOWS_ROOT
) {
  const root = path.win32.normalize(String(systemRoot || "")).replace(/[\\/]+$/, "");
  if (!/^[A-Za-z]:\\Windows$/i.test(root)) {
    throw new TypeError("Windows system root must be a drive-root Windows directory");
  }
  const system32 = path.win32.join(root, "System32");
  return Object.freeze({
    netstat: path.win32.join(system32, "netstat.exe"),
    powershell: path.win32.join(
      system32,
      "WindowsPowerShell",
      "v1.0",
      "powershell.exe"
    ),
    wmic: path.win32.join(system32, "wbem", "wmic.exe"),
    taskkill: path.win32.join(system32, "taskkill.exe"),
    wsl: path.win32.join(system32, "wsl.exe"),
  });
}

/**
 * Resolve a drive-letter Windows path through every junction and symlink on it,
 * returning "" for any other spelling (UNC, `\\?\`, relative) or when resolution
 * fails. Only drive-letter paths reach realpath: a UNC path names a network
 * share, and resolving one would block the port-owner probe on remote I/O.
 * `realpathSync.native` is the only Node realpath that follows NTFS junctions;
 * the JS fallback merely normalizes. The long-path prefix Node may return is
 * stripped so the result compares equal to the Win32 path CIM reports.
 */
function canonicalWindowsPath(candidate, realpathSync = fs.realpathSync.native) {
  const value = String(candidate || "");
  if (!/^[A-Za-z]:\\/.test(value)) return "";
  try {
    const resolved = String(realpathSync(value) || "");
    return path.win32.normalize(resolved.replace(/^\\\\\?\\/, ""));
  } catch {
    return "";
  }
}

// The absolute Windows path a launch of `gatewayBin` runs: as given when
// absolute, else the first PATH entry holding an executable of that bare name.
// "" when it names nothing.
function resolveWindowsGatewayBin(gatewayBin, { pathEnv, accessSync }) {
  const bin = String(gatewayBin || "").replace(/\//g, "\\");
  let resolved = bin;
  if (!path.win32.isAbsolute(resolved)) {
    if (!resolved || /[\\/]/.test(resolved)) return "";
    resolved = "";
    for (const rawDir of String(pathEnv).split(path.win32.delimiter)) {
      const dir = rawDir.trim().replace(/^"(.*)"$/, "$1");
      if (!path.win32.isAbsolute(dir)) continue;
      const candidate = path.win32.join(dir, bin);
      try {
        accessSync(candidate, fs.constants.X_OK);
        resolved = candidate;
        break;
      } catch { /* try the next PATH entry */ }
    }
    if (!resolved) return "";
  }
  return path.win32.normalize(resolved);
}

// A distlib console launcher: `<env>\Scripts\kirocrew.exe`.
function isScriptsConsoleLauncher(normalized) {
  return /^kirocrew\.exe$/i.test(path.win32.basename(normalized))
    && /^scripts$/i.test(path.win32.basename(path.win32.dirname(normalized)));
}

// The spelled path plus its junction-resolved spelling, when that exists.
function withCanonicalSpellings(candidates, realpathSync) {
  const spellings = [];
  for (const candidate of candidates) {
    const normalized = path.win32.normalize(candidate);
    spellings.push(normalized);
    const canonical = canonicalWindowsPath(normalized, realpathSync);
    if (canonical) spellings.push(canonical);
  }
  return [...new Set(spellings)];
}

function windowsGatewayExecutablePaths(
  gatewayBin,
  {
    pathEnv = process.env.Path || process.env.PATH || "",
    accessSync = fs.accessSync,
    realpathSync = fs.realpathSync.native,
  } = {}
) {
  const normalized = resolveWindowsGatewayBin(gatewayBin, { pathEnv, accessSync });
  if (!normalized) return [];
  // The bundled .cmd shim is never spawned itself: the launcher unwraps it and
  // runs the interpreter at the tree root (gateway-supervisor.js), so that
  // interpreter is the only executable the shim can put on the port.
  const trusted = /\.cmd$/i.test(normalized)
    ? [path.win32.resolve(path.win32.dirname(normalized), "..", "python.exe")]
    : [normalized];
  if (isScriptsConsoleLauncher(normalized)) {
    // A distlib console launcher delegates to the venv interpreter. Source
    // venvs keep it beside the launcher; bundled target installs keep it at
    // the environment root.
    trusted.push(
      path.win32.join(path.win32.dirname(normalized), "python.exe"),
      path.win32.resolve(path.win32.dirname(normalized), "..", "python.exe")
    );
  }
  // A Toolbox-style install reaches the bundle through a `current` junction.
  // The launcher resolves its executables through that junction, but Windows
  // reports a running process by the path the junction pointed at, so the two
  // spellings of one file must both be trusted. Only resolutions that exist
  // are added; a path that fails to resolve stays as spelled.
  return withCanonicalSpellings(trusted, realpathSync);
}

const DRIVE_PATH_RE = /^[A-Za-z]:\\/;
const BASE_PYTHON_RE = /^python(?:\d+(?:\.\d+)*)?\.exe$/i;

/**
 * The base interpreters a CPython venv's `pyvenv.cfg` names: `python.exe` in
 * its `home` directory, which the redirector starts, and its `executable` key,
 * which can differ once the base install moved. Only drive-letter paths to a
 * `python*.exe` count.
 */
function pyvenvBaseInterpreters(text) {
  const values = {};
  for (const line of String(text || "").split(/\r?\n/)) {
    const match = /^\s*([A-Za-z_-]+)\s*=\s*(.*?)\s*$/.exec(line);
    if (match) values[match[1].toLowerCase()] = match[2].replace(/\//g, "\\");
  }
  const candidates = [
    values.home ? path.win32.join(values.home, "python.exe") : "",
    values.executable || "",
  ];
  const interpreters = new Map();
  for (const candidate of candidates) {
    if (!DRIVE_PATH_RE.test(candidate)) continue;
    const normalized = path.win32.normalize(candidate);
    if (!BASE_PYTHON_RE.test(path.win32.basename(normalized))) continue;
    if (!interpreters.has(normalized.toLowerCase())) interpreters.set(normalized.toLowerCase(), normalized);
  }
  return [...interpreters.values()];
}

/**
 * The launches a source or pip venv's console launcher hands to its venv's
 * BASE interpreter.
 *
 * On Windows a CPython venv's `Scripts\python.exe` is a redirector: it starts
 * the interpreter `pyvenv.cfg` names as a child, so the process that binds the
 * gateway port runs that shared interpreter, which every Python program on the
 * host can run. Its path alone therefore proves nothing and is never added to
 * the trusted executables. A launch entry ties it to this venv instead: the
 * interpreter is trusted only for a command line whose script is exactly this
 * venv's `Scripts\kirocrew.exe` (`isKirocrewCommand`'s `trustedVenvLaunches`).
 *
 * Empty unless the resolved launcher is a `Scripts\kirocrew.exe` whose
 * environment root holds a readable `pyvenv.cfg` naming one: a
 * bundled tree or a non-venv install runs its own interpreter, which
 * `windowsGatewayExecutablePaths` already trusts.
 *
 * @returns {{interpreters:string[], launchers:string[], venvInterpreters:string[]}[]}
 */
function windowsVenvBaseInterpreterLaunches(
  gatewayBin,
  {
    pathEnv = process.env.Path || process.env.PATH || "",
    accessSync = fs.accessSync,
    readFileSync = fs.readFileSync,
    realpathSync = fs.realpathSync.native,
  } = {}
) {
  const launcher = resolveWindowsGatewayBin(gatewayBin, { pathEnv, accessSync });
  if (!launcher || !DRIVE_PATH_RE.test(launcher) || !isScriptsConsoleLauncher(launcher)) {
    return [];
  }
  const scripts = path.win32.dirname(launcher);
  const config = path.win32.join(path.win32.dirname(scripts), "pyvenv.cfg");
  let text;
  try {
    text = readFileSync(config, "utf8");
  } catch {
    return [];
  }
  const interpreters = pyvenvBaseInterpreters(text);
  if (!interpreters.length) return [];
  return [{
    interpreters: withCanonicalSpellings(interpreters, realpathSync),
    launchers: withCanonicalSpellings([launcher], realpathSync),
    venvInterpreters: withCanonicalSpellings([path.win32.join(scripts, "python.exe")], realpathSync),
  }];
}

let WINDOWS_SYSTEM_TOOLS;
try {
  WINDOWS_SYSTEM_TOOLS = windowsSystemToolPaths();
} catch {
  WINDOWS_SYSTEM_TOOLS = windowsSystemToolPaths(FALLBACK_WINDOWS_ROOT);
}

function positiveInteger(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new TypeError(`${label} must be a positive integer`);
  }
  return parsed;
}

function endpointPort(endpoint) {
  const separator = String(endpoint || "").lastIndexOf(":");
  if (separator < 0) return null;
  const parsed = Number(String(endpoint).slice(separator + 1));
  return Number.isInteger(parsed) ? parsed : null;
}

/**
 * Parse `netstat -ano` without depending on the localized state text.
 * A TCP listener is identified by its local port and wildcard foreign endpoint.
 */
function parseNetstatListenPids(stdout, port) {
  const wantedPort = positiveInteger(port, "port");
  const pids = new Set();
  for (const line of String(stdout || "").split(/\r?\n/)) {
    const columns = line.trim().split(/\s+/);
    if (columns.length < 5 || columns[0].toUpperCase() !== "TCP") continue;
    if (endpointPort(columns[1]) !== wantedPort) continue;
    if (columns[2] !== "0.0.0.0:0" && columns[2] !== "[::]:0") continue;
    const pid = Number(columns[columns.length - 1]);
    if (Number.isInteger(pid) && pid > 0) pids.add(pid);
  }
  return [...pids];
}

function execFileText(execFileFn, command, args, timeout) {
  return new Promise((resolve, reject) => {
    execFileFn(command, args, { timeout }, (err, stdout) => {
      if (err) return reject(err);
      resolve(String(stdout || ""));
    });
  });
}

async function windowsListenPids(
  port,
  { execFileFn = execFile, timeoutMs = 5000, tools = WINDOWS_SYSTEM_TOOLS } = {}
) {
  const stdout = await execFileText(
    execFileFn,
    tools.netstat,
    // `-p TCP` restricts Windows output to IPv4 and hides IPv6-only listeners.
    ["-ano"],
    timeoutMs
  );
  return parseNetstatListenPids(stdout, port);
}

function describeExecFailure(error, timeoutMs) {
  if (error && (error.killed || error.code === "ETIMEDOUT")) {
    return `timed out after ${timeoutMs}ms`;
  }
  const code = error && error.code !== undefined ? ` (exit ${error.code})` : "";
  return `${(error && error.message) || String(error)}`.trim() + code;
}

/**
 * windowsListenPids for the wedge-recovery force-stop, which turns ANY probe
 * failure into a dead-end "restart required". One slow netstat spawn on a busy
 * host (a security scanner inspecting every new process, a loop that just
 * stalled) must not decide that. Each attempt is still bounded, and a probe that
 * never succeeds still rejects, so the caller keeps failing closed; only a
 * transient failure is absorbed. The rejection says how it failed, because a
 * bare "Command failed: netstat.exe -ano" cannot tell a timeout from an exit.
 */
async function windowsListenPidsRetrying(
  port,
  {
    attempts = 3,
    retryDelayMs = 500,
    timeoutMs = 10000,
    sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); }),
    log = () => {},
    listenPids = windowsListenPids,
    ...probeOptions
  } = {}
) {
  const total = positiveInteger(attempts, "attempts");
  let lastError;
  for (let attempt = 1; attempt <= total; attempt += 1) {
    try {
      return await listenPids(port, { ...probeOptions, timeoutMs });
    } catch (error) {
      lastError = error;
      log(`netstat probe ${attempt}/${total} on :${port} failed: ${describeExecFailure(error, timeoutMs)}`);
      if (attempt < total) await sleep(retryDelayMs);
    }
  }
  throw new Error(
    `netstat failed ${total} times on :${port}; last: ${describeExecFailure(lastError, timeoutMs)}`
  );
}

function processIdentity(executablePath, commandLine) {
  const executable = String(executablePath || "").trim();
  const command = String(commandLine || "").trim();
  if (!executable) return "";
  return `"${executable}" ${command}`.trim();
}

function unwrapWmicProcessIdentity(stdout) {
  let executablePath = "";
  let commandLine = "";
  for (const line of String(stdout || "").split(/\r?\n/)) {
    if (/^\s*ExecutablePath=/i.test(line)) {
      executablePath = line.replace(/^\s*ExecutablePath=/i, "").trim();
    } else if (/^\s*CommandLine=/i.test(line)) {
      commandLine = line.replace(/^\s*CommandLine=/i, "").trim();
    }
  }
  return processIdentity(executablePath, commandLine);
}

/**
 * Return a process command line. PowerShell is preferred because WMIC is
 * optional on current Windows releases; WMIC remains the compatibility
 * fallback. Failure to identify a process returns an empty string, which
 * callers treat as an UNIDENTIFIED holder -- never as a process proven to be
 * someone else's, and never as one safe to terminate.
 */
async function windowsProcessCommand(
  pid,
  {
    execFileFn = execFile,
    powershellTimeoutMs = 8000,
    wmicTimeoutMs = 5000,
    tools = WINDOWS_SYSTEM_TOOLS,
  } = {}
) {
  const safePid = positiveInteger(pid, "pid");
  const script = `$p = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = ${safePid}"; `
    + `if ($null -ne $p -and $p.ExecutablePath) { `
    + `[Console]::Out.Write(('"' + $p.ExecutablePath + '" ' + $p.CommandLine).Trim()) }`;
  try {
    const command = (await execFileText(
      execFileFn,
      tools.powershell,
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
      powershellTimeoutMs
    )).trim();
    if (command) return command;
  } catch { /* try WMIC */ }

  try {
    const output = await execFileText(
      execFileFn,
      tools.wmic,
      [
        "process", "where", `ProcessId=${safePid}`, "get",
        "ExecutablePath,CommandLine", "/FORMAT:LIST",
      ],
      wmicTimeoutMs
    );
    return unwrapWmicProcessIdentity(output);
  } catch {
    return "";
  }
}

async function windowsTaskkill(
  pid,
  {
    execFileFn = execFile,
    timeoutMs = 10000,
    tools = WINDOWS_SYSTEM_TOOLS,
    getCommandFn = windowsProcessCommand,
    isTrustedCommand = () => false,
  } = {}
) {
  const safePid = positiveInteger(pid, "pid");
  const command = await getCommandFn(safePid);
  if (!isTrustedCommand(command)) {
    throw new Error(`Refusing taskkill for pid ${safePid}: process identity changed`);
  }
  // /T takes the whole subtree, not just the listening PID. The gateway is not a
  // leaf: it spawns detached kiro-cli ACP runtimes, MCP servers and app servers,
  // and Windows has no process group a single kill can reach. Killing the parent
  // alone frees the PORT -- so the caller's verification reports success -- while
  // those children survive, reparented, still holding the data home's locks and
  // the same .local_secret, and the respawned gateway then races orphans from the
  // generation it just replaced. The identity check above still gates on the
  // PARENT, which is the process whose ownership we can prove; /T then follows the
  // kernel's own parent links rather than a name match, so it cannot widen the
  // kill to a process we did not authorise.
  //
  // The Python half of the product already settled this: cli_server.py's stop
  // path uses platform_compat.kill_process_tree ("a single-PID kill_pid would
  // orphan them"). Same invariant, same reason, both sides.
  await execFileText(
    execFileFn,
    tools.taskkill,
    ["/T", "/F", "/PID", String(safePid)],
    timeoutMs
  );
}

module.exports = {
  canonicalWindowsPath,
  parseNetstatListenPids,
  windowsSystemToolPaths,
  windowsGatewayExecutablePaths,
  windowsVenvBaseInterpreterLaunches,
  windowsListenPids,
  windowsListenPidsRetrying,
  windowsProcessCommand,
  windowsTaskkill,
};
