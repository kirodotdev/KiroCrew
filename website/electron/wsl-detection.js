"use strict";
//
// WSL2 Discovery and distribution detection utilities.
//
// Provides pure output parsing and process execution to discover
// WSL2 distributions on Windows without shell concatenation, PATH hijacking,
// or unvalidated input.
//

const { execFile } = require("child_process");
const { windowsSystemToolPaths } = require("./windows-port");

/**
 * Strict regex for valid WSL distribution names.
 * Linux distro names in WSL consist of alphanumeric characters, underscores,
 * dots, and hyphens (e.g. "Ubuntu", "Ubuntu-24.04", "Debian", "docker-desktop").
 * Prevents command injection and flag injection (e.g. strings starting with '-').
 */
const SAFE_DISTRO_NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,254}$/;

/**
 * Validate that a distribution name conforms to safe identifier rules.
 *
 * @param {unknown} name
 * @returns {boolean}
 */
function isValidDistroName(name) {
  if (typeof name !== "string") return false;
  const trimmed = name.trim();
  if (!trimmed) return false;
  return SAFE_DISTRO_NAME_RE.test(trimmed);
}

/**
 * Decode raw stdout/stderr bytes from wsl.exe.
 *
 * The child runs with WSL_UTF8=1 (see detectWsl2), so every WSL build since
 * 0.51.01 emits UTF-8 — and UTF-8 emitted here never contains a NUL byte, so
 * the null-interleave check below is an unambiguous legacy marker, not a
 * heuristic: older builds that ignore the flag emit UTF-16LE, with or without
 * a BOM depending on version and pipe state.
 *
 * @param {Buffer|string} raw
 * @returns {string}
 */
function decodeWslOutput(raw) {
  if (!raw) return "";
  if (typeof raw === "string") {
    return raw.replace(/^\uFEFF/, "").trim();
  }

  if (!Buffer.isBuffer(raw)) {
    return "";
  }

  const isUtf16Le =
    (raw.length >= 2 && raw[0] === 0xff && raw[1] === 0xfe) ||
    (raw.length >= 4 && (raw[1] === 0x00 || raw[3] === 0x00));

  if (isUtf16Le) {
    try {
      return new TextDecoder("utf-16le").decode(raw).replace(/^\uFEFF/, "").trim();
    } catch {
      // Fall through to the UTF-8 decoder.
    }
  }

  return new TextDecoder("utf-8").decode(raw).replace(/^\uFEFF/, "").trim();
}

/**
 * Map a raw localized STATE column onto a stable enum.
 *
 * wsl.exe prints the state in the OS language ("Running", "En cours
 * d'exécution", "Wird ausgeführt", …), so no consumer may branch on the raw
 * text. `state` is the machine-readable value — running/stopped/unknown — and
 * `stateLabel` preserves the original string for display. Only the languages
 * Windows itself ships in are recognized; an unrecognized locale degrades to
 * "unknown" instead of leaking a localized string into what callers treat as
 * an enum.
 *
 * @param {string} rawState lowercased STATE column text
 * @returns {{ state: "running"|"stopped"|"unknown", stateLabel: string }}
 */
function normalizeWslState(rawState) {
  const stateLabel = rawState.trim();
  const norm = stateLabel.toLowerCase();
  if (
    norm.includes("running") ||
    norm.includes("en cours") ||
    norm.includes("ausgeführt") ||
    norm.includes("ejecución")
  ) {
    return { state: "running", stateLabel };
  }
  if (
    norm.includes("stopped") ||
    norm.includes("arrêté") ||
    norm.includes("beendet") ||
    norm.includes("detenido")
  ) {
    return { state: "stopped", stateLabel };
  }
  return { state: "unknown", stateLabel };
}

/**
 * Parse the output of `wsl.exe --list --verbose` (or `wsl.exe -l -v`).
 *
 * Example input:
 *   NAME                   STATE           VERSION
 * * Ubuntu                 Running         2
 *   Ubuntu-24.04           Stopped         2
 *   Debian                 En cours d'exécution 2  (localized French Windows)
 *   legacy-distro          Running         1
 *
 * Note: Only WSL version 2 distributions are returned. Handles localized Windows
 * state strings where STATE contains multiple words or spaces.
 *
 * @param {Buffer|string} stdout
 * @returns {Array<{ name: string, state: string, stateLabel: string, version: number, isDefault: boolean }>}
 */
function parseWslListOutput(stdout) {
  const text = decodeWslOutput(stdout);
  if (!text) return [];

  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length === 0) return [];

  const distros = [];

  for (const line of lines) {
    // Skip header line (e.g. "NAME STATE VERSION" or localized equivalents)
    if (/^NAME\s+STATE\s+VERSION/i.test(line.replace(/^\*\s*/, ""))) {
      continue;
    }

    // A line starts with an optional '*' indicating default distro
    const isDefault = line.startsWith("*");
    const cleanLine = isDefault ? line.slice(1).trim() : line;

    // Tokens: Name, [State tokens...], Version
    const tokens = cleanLine.split(/\s+/);
    if (tokens.length < 2) continue;

    // The version is always the last token on the line
    const versionNum = parseInt(tokens[tokens.length - 1], 10);
    if (versionNum !== 2) {
      continue;
    }

    const name = tokens[0];
    if (!isValidDistroName(name)) {
      continue;
    }

    // State is everything between the first token (name) and the last token (version)
    const rawState = tokens.slice(1, -1).join(" ");

    distros.push({
      name,
      ...normalizeWslState(rawState),
      version: versionNum,
      isDefault,
    });
  }

  return distros;
}

/**
 * Detect WSL2 availability and list all configured WSL2 distributions.
 *
 * @param {object} [opts]
 * @param {string} [opts.platform=process.platform]
 * @param {NodeJS.ProcessEnv} [opts.env=process.env]
 * @param {(file: string, args: string[], options: object, callback: Function) => any} [opts.execFileFn=execFile]
 * @param {number} [opts.timeoutMs=5000]
 * @returns {Promise<{
 *   available: boolean,
 *   distros: Array<{ name: string, state: string, stateLabel: string, version: number, isDefault: boolean }>,
 *   defaultDistro: string|null,
 *   error?: string,
 *   reason?: string
 * }>}
 */
function detectWsl2({
  platform = process.platform,
  env = process.env,
  execFileFn = execFile,
  timeoutMs = 5000,
} = {}) {
  return new Promise((resolve) => {
    if (platform !== "win32") {
      return resolve({
        available: false,
        distros: [],
        defaultDistro: null,
        reason: "not-windows",
      });
    }

    let wslBinary;
    try {
      // Resolved through windows-port's System32 table (validated root shape)
      // rather than PATH lookup, so a planted wsl.exe cannot be adopted.
      wslBinary = windowsSystemToolPaths(
        env.SystemRoot || env.WINDIR || "C:\\Windows"
      ).wsl;
    } catch (err) {
      return resolve({
        available: false,
        distros: [],
        defaultDistro: null,
        error: err instanceof Error ? err.message : String(err),
        reason: "wsl-error",
      });
    }

    execFileFn(
      wslBinary,
      ["--list", "--verbose"],
      {
        timeout: timeoutMs,
        windowsHide: true,
        encoding: "buffer",
        maxBuffer: 10 * 1024 * 1024,
        // wsl.exe emits UTF-8 with this flag (UTF-16LE before 0.51.01 —
        // decodeWslOutput keeps the BOM fallback for those builds).
        env: { ...env, WSL_UTF8: "1" },
      },
      (err, stdout, stderr) => {
        if (err) {
          const errMsg = stderr ? decodeWslOutput(stderr) : err.message;
          return resolve({
            available: false,
            distros: [],
            defaultDistro: null,
            error: errMsg,
            reason: err.code === "ENOENT" ? "wsl-not-found" : "wsl-error",
          });
        }

        const distros = parseWslListOutput(stdout);
        if (distros.length === 0) {
          return resolve({
            available: true,
            distros: [],
            defaultDistro: null,
            reason: "no-wsl2-distros",
          });
        }

        const defaultEntry = distros.find((d) => d.isDefault);
        const defaultDistro = defaultEntry ? defaultEntry.name : distros[0].name;

        resolve({
          available: true,
          distros,
          defaultDistro,
        });
      }
    );
  });
}

module.exports = {
  isValidDistroName,
  decodeWslOutput,
  normalizeWslState,
  parseWslListOutput,
  detectWsl2,
};
