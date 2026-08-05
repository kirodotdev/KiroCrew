// Input validation for remote tunnel settings.
// Extracted from main.js so it can be unit-tested without Electron.

// DNS label: starts/ends with alnum, hyphens allowed in the middle. Labels joined by single dots, or a single SSH alias
const HOSTNAME_RE = /^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*$/;
// Filesystem path: alnum, ~, /, ., _, - only. $HOME allowed at start for remote expansion.
const BINPATH_RE = /^(\$HOME\/)?[a-zA-Z0-9~\/._][a-zA-Z0-9\/._~-]*$/;
// Remote port: digits only, validated 1-65535 in code.
const REMOTE_PORT_RE = /^[0-9]{1,5}$/;
// Remote PATH: colon-separated filesystem paths (alnum, ~, /, ., _, -). e.g. ~/.toolbox/bin:/usr/bin:/bin
const REMOTE_PATH_RE = /^[a-zA-Z0-9~\/._][a-zA-Z0-9\/._~:\-]*$/;

function validateRemoteSettings(host, bin, remotePort, remotePath) {
  if (host && host.length > 253) return "Hostname too long (max 253 characters).";
  if (host && !HOSTNAME_RE.test(host)) return "Invalid hostname — must be a DNS name or SSH config alias (e.g. myhost.corp.example.com or clouddesk).";
  if (bin && bin.startsWith("-")) return "Binary path must not start with a dash.";
  if (bin && bin.includes("..")) return "Binary path must not contain path traversal (../).";
  if (bin && !BINPATH_RE.test(bin)) return "Invalid binary path — only filesystem path characters allowed (no spaces).";
  if (remotePort) {
    if (!REMOTE_PORT_RE.test(remotePort)) return "Remote port must be a number.";
    const n = parseInt(remotePort, 10);
    if (n < 1 || n > 65535) return "Remote port must be between 1 and 65535.";
  }
  if (remotePath) {
    if (remotePath.includes("..")) return "Remote PATH must not contain path traversal.";
    if (!REMOTE_PATH_RE.test(remotePath)) return "Remote PATH contains invalid characters.";
  }
  return null;
}

module.exports = { HOSTNAME_RE, BINPATH_RE, REMOTE_PORT_RE, REMOTE_PATH_RE, validateRemoteSettings };
