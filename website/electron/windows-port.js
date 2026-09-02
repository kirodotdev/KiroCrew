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

function windowsGatewayExecutablePaths(
  gatewayBin,
  {
    pathEnv = process.env.Path || process.env.PATH || "",
    accessSync = fs.accessSync,
  } = {}
) {
  const bin = String(gatewayBin || "").replace(/\//g, "\\");
  let resolved = bin;
  if (!path.win32.isAbsolute(resolved)) {
    if (!resolved || /[\\/]/.test(resolved)) return [];
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
    if (!resolved) return [];
  }
  const normalized = path.win32.normalize(resolved);
  if (/\.cmd$/i.test(normalized)) {
    return [path.win32.resolve(path.win32.dirname(normalized), "..", "python.exe")];
  }
  const trusted = [normalized];
  if (
    /^kirocrew\.exe$/i.test(path.win32.basename(normalized))
    && /^scripts$/i.test(path.win32.basename(path.win32.dirname(normalized)))
  ) {
    // A distlib console launcher delegates to the venv interpreter. Source
    // venvs keep it beside the launcher; bundled target installs keep it at
    // the environment root.
    trusted.push(
      path.win32.join(path.win32.dirname(normalized), "python.exe"),
      path.win32.resolve(path.win32.dirname(normalized), "..", "python.exe")
    );
  }
  return [...new Set(trusted.map((candidate) => path.win32.normalize(candidate)))];
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
 * fallback. Failure to identify a process returns an empty string so callers
 * classify it as foreign and never terminate it.
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
  parseNetstatListenPids,
  windowsSystemToolPaths,
  windowsGatewayExecutablePaths,
  windowsListenPids,
  windowsProcessCommand,
  windowsTaskkill,
};
