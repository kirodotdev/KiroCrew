"use strict";
//
// Driver for early-boot-guard.test.js: loads main.js under a stubbed `electron`
// in a child `node` so the boot sequence is parsed AND executed without a real
// Electron binary or a display.
//
// argv: <logsDir> <userDataDir> <mode>
//   mode "ok"          every stub call succeeds; the driver prints MAIN_LOADED
//                      and exits 0 once main.js has finished loading.
//   mode "throw-once"  the FIRST `app.getPath` call throws, the way a broken
//                      profile directory does. main.js's own guard must catch
//                      that: it prints SHOW_ERROR_BOX (from the dialog stub),
//                      writes to <logsDir>/gateway-launch.log, and exits 1.
//
// Stubbing happens at the `require` boundary via `Module._load`, the same seam
// ipc-registrar.test.js and global-hotkey.test.js use, so every module in the
// tree — electron-store included — sees the same fake.
//
// Nothing here is wrapped in try/catch on purpose: a throw from `require` has
// to reach the process as an uncaught exception, because that is the path the
// guard under test listens on.

const Module = require("node:module");
const path = require("node:path");

const [logsDir, userDataDir, mode] = process.argv.slice(2);
if (!logsDir || !userDataDir || !mode) {
  process.stderr.write("usage: early-boot-driver.js <logsDir> <userDataDir> ok|throw-once\n");
  process.exit(2);
}

/** A surface whose every property is a no-op function (or a nested surface). */
function noopSurface(name) {
  const fn = function noop() {};
  return new Proxy(fn, {
    get(target, key) {
      if (key === Symbol.toPrimitive || key === "toString") return () => `[fake ${name}]`;
      if (key === "then") return undefined; // not a thenable
      if (key in target) return target[key];
      return noopSurface(`${name}.${String(key)}`);
    },
    apply() {
      return undefined;
    },
    construct() {
      return noopSurface(`${name}()`);
    },
  });
}

let getPathThrows = mode === "throw-once";
const MAIN_PATH = path.join(__dirname, "..", "main.js");
const pathsByName = {
  logs: logsDir,
  userData: userDataDir,
  crashDumps: path.join(userDataDir, "Crashpad"),
  home: userDataDir,
  temp: userDataDir,
  appData: path.dirname(userDataDir),
  exe: process.execPath,
};

/**
 * Whether the current `getPath` call comes from main.js itself. Dependencies
 * (mochi/index.js among them) call `app.getPath` during their OWN module load,
 * i.e. inside the `require` block, before main.js has run a statement of its
 * own — that span belongs to Node's default fatal handler, not to the guard.
 * The induced throw therefore lands on main.js's first own call,
 * `seedRenamedStore(app.getPath("userData"))`, which is inside the guarded span.
 */
function calledFromMain() {
  const stack = String(new Error().stack || "");
  const frames = stack.split("\n").slice(2); // drop the message and this frame
  const caller = frames.find((line) => !line.includes(__filename));
  return Boolean(caller && caller.includes(MAIN_PATH));
}

const app = {
  name: "",
  isPackaged: false,
  getPath(name) {
    if (getPathThrows && calledFromMain()) {
      getPathThrows = false;
      throw new Error("induced: getPath(" + name + ") unavailable");
    }
    return pathsByName[name] || path.join(userDataDir, name);
  },
  getVersion: () => "0.0.0-test",
  getName: () => "Kiro Crew",
  getAppPath: () => path.join(__dirname, ".."),
  requestSingleInstanceLock: () => true,
  setAppUserModelId() {},
  on() {},
  once() {},
  whenReady: () => new Promise(() => {}),
  commandLine: { appendSwitch() {}, hasSwitch: () => false },
  exit(code) {
    process.stdout.write("APP_EXIT:" + code + "\n");
    process.exit(code);
  },
  quit() {
    process.exit(0);
  },
};

const dialog = {
  showErrorBox(title, content) {
    process.stdout.write("SHOW_ERROR_BOX:" + title + "\n");
    process.stdout.write("SHOW_ERROR_BOX_CONTENT:" + content.split("\n")[0] + "\n");
  },
  showMessageBox: () => Promise.resolve({ response: 1 }),
};

const fakeElectron = new Proxy(
  // `ipcRenderer` is undefined in a real main process; electron-store branches
  // on it, so a truthy stand-in would send it down the renderer path.
  { app, dialog, ipcRenderer: undefined },
  {
    get(target, key) {
      if (key in target) return target[key];
      if (typeof key !== "string") return undefined;
      const surface = noopSurface("electron." + key);
      target[key] = surface;
      return surface;
    },
  },
);

const originalLoad = Module._load;
Module._load = function loadWithFakeElectron(request, parent, isMain) {
  if (request === "electron") return fakeElectron;
  return originalLoad.call(this, request, parent, isMain);
};

require(path.join(__dirname, "..", "main.js"));
process.stdout.write("MAIN_LOADED\n");
process.exit(0);
