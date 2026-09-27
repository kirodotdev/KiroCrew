"use strict";

// Whose crash is this? The single ownership rule both crash channels use: a
// minidump's first module and a macOS report's filename are ours only when the
// name is one of this install's own names (display or executable), its base
// helper, or an Electron helper variant of it. Filenames in the shared
// DiagnosticReports directory are judged BEFORE anything is opened, so the scan
// never becomes a way to learn what else the user runs.

/**
 * Basename of a path that may use EITHER separator.
 *
 * `path.basename` follows the HOST platform, but a module name inside a
 * minidump follows the platform that PRODUCED it. A Windows dump opened for
 * inspection on a posix host would otherwise come back as the whole
 * `C:\...\KiroCrew.exe` string and never match the app name.
 */
function anyBasename(value) {
  const text = String(value || "");
  const cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
  return cut >= 0 ? text.slice(cut + 1) : text;
}

/** Lowercase, strip spaces and a trailing `.exe`. Comparison form only. */
function normalizeName(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\.exe$/, "")
    .replace(/\s+/g, "");
}

/**
 * Every name this install goes by, in comparison form. Order and duplicates
 * do not matter; emptiness does, and empties are dropped.
 *
 * Ownership is membership in an ENUMERATED set, and this function is where the
 * enumeration happens. It exists because the two names an Electron app has are
 * different strings that neither derives from the other:
 *
 * - the DISPLAY name, `app.getName()` — `Kiro Crew`, or `Kiro Crew Nightly`;
 * - the EXECUTABLE name, `path.basename(process.execPath)` — on macOS that is
 *   the bundle binary and happens to match, but `electron/package.json` sets
 *   `executableName: "kirocrew-desktop"` for Linux and Windows, and
 *   `packaging/build-desktop.sh` overrides the nightly channel to
 *   `kirocrew-desktop-nightly`.
 *
 * So a rule anchored on the display name alone reads `kirocrew-desktop` as a
 * foreign process and throws away every Linux crash of our own app — and off
 * darwin a minidump is the ONLY crash channel we have, because `main.js` passes
 * no `diagnosticReportsDir` there. Worse than thrown away: a dump we classify
 * as proven-foreign joins the seen-set, so the artifact is written off for good
 * and a later fix never revisits it. Guessing one name from the other cannot
 * work either — `kirocrew-desktop-nightly` is not a prefix-extension of
 * `kirocrewnightly` in any direction.
 */
function ownNames(value) {
  const names = [];
  for (const item of Array.isArray(value) ? value : [value]) {
    const name = normalizeName(item);
    if (name && !names.includes(name)) names.push(name);
  }
  return names;
}

/**
 * Is this one of our own process names, allowing for helpers?
 *
 * The single ownership rule, used by BOTH branches of the ownership chain:
 * `isOwnModule` for a minidump's main module, `ipsBelongsToApp` for a macOS
 * report's filename. A name of ours must be the whole of the candidate, our
 * base helper (`<productName> Helper`) exactly, or an Electron helper variant —
 * macOS names those `<productName> Helper (Renderer)` / ` (GPU)`, which after
 * `normalizeName` (spaces stripped, lowercased) read as `<name>helper(...)`.
 *
 * A bare prefix/substring test was the earlier spelling of both branches, and
 * it claimed any process whose name merely BEGINS with one of ours: a sibling
 * release channel (`KiroCrew Nightly`), or an unrelated vendor's `KiroCrewX`.
 * The helper arm had the same flaw one level down — matching a bare
 * `<name>helper` PREFIX also claims a foreign `<name> HelperX`, a different
 * program that merely begins with our helper name — so it must match the base
 * helper exactly or continue into the parenthesized Electron suffix, never an
 * open prefix.
 * Both inputs come from a directory shared with other programs, so that is not
 * hypothetical — it writes another build's crash into our ledger and shows the
 * user a banner for a crash that never happened in this install.
 *
 * `appNames` is a string or a list of them; see `ownNames` for why one name is
 * never enough.
 */
function nameIsOurs(candidate, appNames) {
  if (!candidate) return false;
  return ownNames(appNames).some(
    (app) =>
      candidate === app ||
      candidate === `${app}helper` ||
      candidate.startsWith(`${app}helper(`),
  );
}

/**
 * Does this module name belong to our app?
 *
 * Exactly `nameIsOurs` applied to a minidump's main-module path, with no extra
 * allowance of its own. A `npm start` dev run is still covered, and covered
 * BETTER than by a special case: `main.js` always passes
 * `path.basename(process.execPath)` as one of `appNames`, which in a dev run is
 * `Electron`, so `nameIsOurs` matches it exactly.
 *
 * This deliberately does NOT fall back to "any module named `electron*`". That
 * fallback was here, and it reinstated for minidumps precisely the bare-prefix
 * test `nameIsOurs` documents as wrong: `crashDumps` is shared with every other
 * Electron app and every Electron project run from this machine, so a foreign
 * `electron`, `electron-helper`, or another checkout's `Electron` would be
 * claimed as ours — a crash we never had, entered in our ledger, banner and all,
 * and then marked seen so the real owner's evidence is written off too. The
 * other branch of the chain (`ipsBelongsToApp`) never had such a fallback; both
 * branches now decide ownership by the same single rule.
 */
function isOwnModule(moduleName, appNames) {
  const base = normalizeName(anyBasename(moduleName));
  if (!base) return false;
  return nameIsOurs(base, appNames);
}

/**
 * Is this module name an Electron runtime -- ours or not?
 *
 * `crashDumps` is `<userData>/Crashpad`, so it is per-app, not machine-wide; but
 * "per-app" is keyed on `app.getName()`, and a dev `npm start` of this SAME app
 * shares the directory with the installed build while its main module is
 * `Electron`, which `isOwnModule` rightly refuses to claim. That dump is a
 * genuine Electron crash that the other build's collector will read as its own
 * on its next launch. So an electron-shaped foreign dump is acknowledged (not
 * ours to report) but never deleted (not ours to destroy). The children this
 * fix exists for -- ruby, python, node, chrome-headless-shell -- are not
 * electron-shaped and stay deletable. This is the ONE place a bare `electron`
 * prefix test is right: it decides what to keep, never what to claim.
 */
function isElectronShaped(moduleName) {
  const base = normalizeName(anyBasename(moduleName));
  if (!base) return false;
  return base === "electron" || base.startsWith("electronhelper") || base.startsWith("electron.");
}

/**
 * The `-YYYY-MM-DD-HHMMSS` stamp macOS appends to every `.ips` it writes, plus
 * the `.NNN` counter it adds when two reports land in the SAME second.
 *
 * The counter is not an edge case and it is not optional: of 212 reports in one
 * real `~/Library/Logs/DiagnosticReports`, 38 (18%) carried one — `.000`,
 * `.0002`, `.0003`, `.0004` — and they appear precisely during a burst, when a
 * process and its helpers go down together or a crash loop retries. Anchoring
 * the stamp at `$` without it dropped exactly the reports this collector exists
 * to surface, including the `Google Chrome Helper (GPU)` / `(Renderer)` shape
 * that is the same Chromium helper naming our own app produces.
 */
const IPS_STAMP_RE = /-\d{4}-\d{2}-\d{2}-\d{6}(\.\d+)?$/;

/**
 * Filename-only ownership test for a shared crash-report directory.
 *
 * Runs BEFORE the file is opened, which is the point: DiagnosticReports holds
 * every application's crash reports, and this scan must not become a way to
 * enumerate them. macOS names these `<ProcessName>-<date>-<time>.ips`.
 *
 * The stamp is stripped FIRST so there is a boundary to anchor against, and the
 * process name that remains has to satisfy `nameIsOurs` outright. Testing the
 * whole filename for our prefix instead — which is what this did — makes
 * `KiroCrew Nightly-2026-09-03-101530.ips` read as a stable-channel crash,
 * because "kirocrew" is a prefix of "kirocrewnightly-2026-...".
 *
 * A name carrying no stamp is therefore refused, and the asymmetry is
 * deliberate: skipping a report of ours costs the banner one launch and leaves
 * the artifact on disk for the next scan, while claiming somebody else's writes
 * a crash that never happened into a ledger a human will later read as fact.
 */
function ipsBelongsToApp(basename, appNames) {
  const name = String(basename || "");
  if (!name.toLowerCase().endsWith(".ips")) return false;
  const stem = name.slice(0, -".ips".length);
  if (!IPS_STAMP_RE.test(stem)) return false;
  return nameIsOurs(normalizeName(stem.replace(IPS_STAMP_RE, "")), appNames);
}

module.exports = {
  anyBasename,
  isOwnModule,
  isElectronShaped,
  ipsBelongsToApp,
};
