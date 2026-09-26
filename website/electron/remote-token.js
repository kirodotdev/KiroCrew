// Helpers for Remote Tunnel mode: build the command executed on the remote
// dev desktop over SSH to fetch a KiroCrew dashboard token.
//
// Split out from main.js so the shell-construction logic can be unit-tested
// without spinning up Electron.

const DEFAULT_REMOTE_BIN = "~/.local/bin/kirocrew";
const DEFAULT_REMOTE_PATH = "~/.toolbox/bin:/usr/bin:/bin";

// Non-interactive SSH shells don't source ~/.zshrc, so PATH won't include
// `~/.toolbox/bin`. Each candidate is a full path so the remote shell can
// exec it directly without relying on PATH.
const REMOTE_BIN_CANDIDATES = [
  "~/.toolbox/bin/kirocrew",           // toolbox install (recommended per wiki)
  "~/.local/bin/kirocrew",             // install.sh / source install
  "~/.kirocrew-app/.venv/bin/kirocrew", // one-liner installer venv
];

// Build a shell snippet that tries each candidate path in order and execs the
// first one that's executable.
//
// Tilde (~) is left unquoted here so the remote shell expands it at parse time.
// Candidates are hard-coded literals (no user input) so embedding without
// quotes is safe. User-supplied binPath is double-quoted for injection safety
// and uses $HOME expansion instead — see buildRemoteTokenCommand().
function buildCandidateTokenCommand(candidates, { port, remotePath } = {}) {
  const pathStr = remotePath || DEFAULT_REMOTE_PATH;
  const portExport = port ? ` KIROCREW_PORT=${port}` : "";
  const expanded = candidates.join(" ");
  return [
    `export PATH=${pathStr}${portExport};`,
    `for b in ${expanded}; do`,
    '  if [ -x "$b" ]; then',
    '    exec "$b" token;',
    '  fi;',
    'done;',
    `echo "kirocrew binary not found in any of: ${candidates.join(", ")}" >&2;`,
    'exit 127',
  ].join(" ");
}

// Pick the right remote command given the user's stored binPath.
//   - If binPath is the default sentinel, try every candidate in order.
//   - Otherwise respect the user's customization.
// Options: { port, remotePath, candidates }
function buildRemoteTokenCommand(binPath, options = {}) {
  const { port, remotePath, candidates = REMOTE_BIN_CANDIDATES } = options;
  const pathStr = remotePath || DEFAULT_REMOTE_PATH;
  const portExport = port ? ` KIROCREW_PORT=${port}` : "";
  if (!binPath || binPath === DEFAULT_REMOTE_BIN) {
    return buildCandidateTokenCommand(candidates, { port, remotePath });
  }
  // Double-quote binPath and PATH for injection safety (binPath is user input
  // validated by regex, but quotes add defense-in-depth). Rewrite `~/` to
  // `$HOME/` since tilde doesn't expand inside double quotes; $HOME is
  // evaluated by the remote shell (execFile bypasses any local shell).
  const expanded = binPath.replace(/^~\//, "$HOME/");
  const expandedPath = pathStr.replace(/~\//g, "$HOME/");
  return `export PATH="${expandedPath}"${portExport}; "${expanded}" token`;
}

// Local ssh argv for the token fetch, matching the gateway's mint
// (`instances/token_mint.py` `_build_ssh_argv`). `-n` reads ssh's stdin from
// the null device: `execFile` never closes the child's stdin pipe, and through
// a ProxyCommand ssh then waits after the remote command exits for an EOF that
// never arrives, until the timeout kills it (#9380, the Node twin of #9360).
// `BatchMode=yes` fails fast instead of waiting on a prompt nobody can answer.
// `ConnectTimeout` takes the caller's whole budget, as the mint does: on
// OpenSSH >= 8.6 it also bounds the banner/KEX a slow ProxyCommand spends it on,
// so a fixed smaller value would fail hosts the user already raised it for.
function buildRemoteTokenSshArgs(remoteHost, remoteCommand, { timeoutMs }) {
  return [
    "-n",
    "-o", "BatchMode=yes",
    "-o", `ConnectTimeout=${Math.max(1, Math.round(timeoutMs / 1000))}`,
    remoteHost,
    remoteCommand,
  ];
}

// Turn an `execFile` failure into the reason the user needs. A missing ssh
// binary and a timeout kill used to surface as the same generic failure.
function describeSshFailure(error, stderr, { sshBin, remoteHost, timeoutMs }) {
  const detail = (stderr || "").trim();
  if (error.code === "ENOENT") {
    return `ssh client not found: ${sshBin}. Install the OpenSSH client and retry.`;
  }
  if (error.killed) {
    const timedOut = `ssh ${remoteHost} timed out after ${Math.round(timeoutMs / 1000)} s`;
    return detail ? `${timedOut}: ${detail}` : timedOut;
  }
  return detail || error.message;
}

// Extract the JWT from a `kirocrew token` URL. The command prints:
//   http://localhost:5476?token=eyJ...
// or in some configurations `https://.../?token=...&foo=bar` — match either.
function parseTokenFromStdout(stdout) {
  const match = stdout.trim().match(/[?&]token=([^\s&]+)/);
  return match ? match[1] : "";
}

module.exports = {
  DEFAULT_REMOTE_BIN,
  DEFAULT_REMOTE_PATH,
  REMOTE_BIN_CANDIDATES,
  buildCandidateTokenCommand,
  buildRemoteTokenCommand,
  buildRemoteTokenSshArgs,
  describeSshFailure,
  parseTokenFromStdout,
};
