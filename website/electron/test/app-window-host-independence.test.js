/**
 * The desktop shell's app couplings: the generic window host names no app, and
 * the app-specific main-process code that predates it can only shrink.
 *
 * A built-in app leaves this repository only once nothing in the shell reaches
 * it. The window host (app-window-host*.js, app-window-preload.js) is the seam
 * such an app moves onto, so it must stay app-agnostic: it never names a
 * built-in and never requires app-owned shell code. The rows below are the
 * main-process app code that still ships; a PR that moves an app onto the seam
 * deletes its rows, and no PR adds one.
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const SHELL = path.join(__dirname, "..");
const BUILTINS = path.join(SHELL, "..", "..", "src", "kiro_crew", "apps", "builtins");
const HOST_FILES = [
  "app-window-host.js",
  "app-window-host-rules.js",
  "app-window-preload.js",
  "shell-shortcut-reservations.js",
];

/** App-owned directories under website/electron/. Shrink only. */
const APP_SHELL_DIRS = ["crew-companion", "mochi"];

/** main.js's requires of app-owned shell code. Shrink only. */
const APP_SHELL_REQUIRES = ["./crew-companion/index", "./mochi-session-token", "./mochi/index"];

function builtinNames() {
  return fs
    .readdirSync(BUILTINS, { withFileTypes: true })
    .filter((d) => d.isDirectory() && !d.name.startsWith("_"))
    .flatMap((d) => [d.name, d.name.replace(/_/g, "-")]);
}

test("the builtin list this gate reads is not empty", () => {
  const names = builtinNames();
  assert.ok(names.includes("mochi") && names.includes("crew-companion"), names.join(","));
});

test("the window host names no built-in app", () => {
  const names = [...new Set(builtinNames())];
  const hits = [];
  for (const file of HOST_FILES) {
    const src = fs.readFileSync(path.join(SHELL, file), "utf-8");
    for (const name of names) {
      if (new RegExp(`(^|[^A-Za-z0-9_])${name}([^A-Za-z0-9_]|$)`).test(src)) hits.push(`${file}: ${name}`);
    }
  }
  assert.deepStrictEqual(hits, []);
});

test("the window host requires no app-owned shell code", () => {
  const hits = [];
  for (const file of HOST_FILES) {
    const src = fs.readFileSync(path.join(SHELL, file), "utf-8");
    for (const m of src.matchAll(/require\(\s*"([^"]+)"\s*\)/g)) {
      if (APP_SHELL_REQUIRES.includes(m[1]) || APP_SHELL_DIRS.some((d) => m[1].startsWith(`./${d}/`))) {
        hits.push(`${file}: ${m[1]}`);
      }
    }
  }
  assert.deepStrictEqual(hits, []);
});

test("no app-owned shell directory is added", () => {
  const names = new Set(builtinNames());
  const dirs = fs
    .readdirSync(SHELL, { withFileTypes: true })
    .filter((d) => d.isDirectory() && names.has(d.name))
    .map((d) => d.name)
    .sort();
  assert.deepStrictEqual(dirs, APP_SHELL_DIRS);
});

test("main.js requires no app-owned shell code beyond the rows above", () => {
  const src = fs.readFileSync(path.join(SHELL, "main.js"), "utf-8");
  const names = [...new Set(builtinNames())];
  const appRequires = [...src.matchAll(/require\(\s*"(\.[^"]+)"\s*\)/g)]
    .map((m) => m[1])
    .filter((req) => names.some((name) => req.includes(name)))
    .sort();
  assert.deepStrictEqual(appRequires, APP_SHELL_REQUIRES);
});
