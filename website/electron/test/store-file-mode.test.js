"use strict";

// The shell's electron-store files are owner-only (store-file-mode.js).
// config.json holds the remote hosts and binary path the app runs over ssh,
// and conf's default configFileMode (0o666) is chmodded onto every write past
// the umask, so a store built without STORE_FILE_MODE is group- and
// world-writable after each save, and a manual chmod 600 does not survive one.

const { test, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Store = require("electron-store");

const { STORE_FILE_MODE, tightenStoreFileMode } = require("../store-file-mode");

const DRIVER = path.join(__dirname, "early-boot-driver.js");
// POSIX modes say who can open a file only off Windows.
const posixOnly = { skip: process.platform === "win32" };

const tmpDirs = [];
function tmpDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-store-mode-"));
  tmpDirs.push(dir);
  return dir;
}
after(() => {
  for (const dir of tmpDirs) fs.rmSync(dir, { recursive: true, force: true });
});

const modeOf = (file) => fs.statSync(file).mode & 0o777;
const octal = (mode) => mode.toString(8);

function fileAtMode(mode) {
  const file = path.join(tmpDir(), "config.json");
  fs.writeFileSync(file, "{}");
  fs.chmodSync(file, mode);
  return file;
}

test("the store file mode is owner read and write only", () => {
  assert.equal(STORE_FILE_MODE, 0o600);
});

test("tightens a store file any group or other bit can reach", posixOnly, () => {
  for (const mode of [0o666, 0o644, 0o620, 0o604]) {
    const file = fileAtMode(mode);
    const notes = [];
    assert.equal(tightenStoreFileMode(file, { log: (m) => notes.push(m) }), true, octal(mode));
    assert.equal(octal(modeOf(file)), "600", `${octal(mode)} must end at 0600`);
    assert.deepEqual(notes, [`config.json mode ${octal(mode)} tightened to 600`]);
  }
});

test("leaves an owner-only store file as it is", posixOnly, () => {
  for (const mode of [0o600, 0o400]) {
    const file = fileAtMode(mode);
    const notes = [];
    assert.equal(tightenStoreFileMode(file, { log: (m) => notes.push(m) }), false, octal(mode));
    assert.equal(octal(modeOf(file)), octal(mode), "an owner-only mode is the user's choice");
    assert.deepEqual(notes, []);
  }
});

test("a store file it cannot stat is logged, never thrown", () => {
  const notes = [];
  const missing = path.join(tmpDir(), "config.json");
  assert.equal(tightenStoreFileMode(missing, { platform: "linux", log: (m) => notes.push(m) }), false);
  assert.equal(notes.length, 1);
  assert.match(notes[0], /^could not tighten config\.json: /);
});

test("does nothing on Windows", () => {
  const untouchable = new Proxy({}, {
    get(_target, key) {
      throw new Error(`fs.${String(key)} must not be called on win32`);
    },
  });
  assert.equal(tightenStoreFileMode("C:\\x\\config.json", { fsImpl: untouchable, platform: "win32" }), false);
});

test("a store opened with STORE_FILE_MODE saves owner-only, over an open mode too", posixOnly, () => {
  const store = new Store({ cwd: tmpDir(), configFileMode: STORE_FILE_MODE, defaults: { remoteHosts: {} } });
  assert.equal(octal(modeOf(store.path)), "600", "created owner-only");
  fs.chmodSync(store.path, 0o666);
  store.set("remoteHosts", { 5476: { host: "box" } });
  assert.equal(octal(modeOf(store.path)), "600", "a save writes 0600 over a 0666 file");
});

// Each shipped Store must pass the mode itself. conf writes a new file once, at
// construction, and the boot repair runs right after, so the boot test below
// still sees 0600 from a Store built without configFileMode; every later save
// (Mochi's shortcuts and pet pointer, any shell setting) would write 0666.
function topLevelStoreOptions(relFile, binding) {
  const src = fs.readFileSync(path.join(__dirname, "..", relFile), "utf8").replace(/\r\n/g, "\n");
  const head = `const ${binding} = new Store({`;
  const start = src.indexOf(head);
  assert.notEqual(start, -1, `${relFile} must build ${binding} as \`${head}\``);
  let depth = 1;
  let topLevel = "";
  for (let i = start + head.length; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return topLevel;
    if (depth === 1) topLevel += src[i];
  }
  assert.fail(`${relFile}: the ${binding} Store options never close`);
}

for (const [relFile, binding] of [
  ["main.js", "store"],
  ["mochi/index.js", "machineStore"],
]) {
  test(`${relFile} builds ${binding} with configFileMode STORE_FILE_MODE`, () => {
    assert.match(topLevelStoreOptions(relFile, binding), /^\s*configFileMode: STORE_FILE_MODE,$/m);
  });
}

// Boot main.js for real (stubbed electron, real electron-store and fs) twice in
// one userData directory, the way the early-boot load test does.
test("main.js boot keeps config.json and mochi-machine.json owner-only", posixOnly, () => {
  const root = tmpDir();
  const logsDir = path.join(root, "logs");
  const userData = path.join(root, "userData");
  const home = path.join(root, "home");
  for (const dir of [logsDir, userData, home]) fs.mkdirSync(dir, { recursive: true });
  const boot = () => {
    const run = spawnSync(process.execPath, [DRIVER, logsDir, userData, "ok"], {
      cwd: root,
      encoding: "utf8",
      timeout: 60_000,
      env: { ...process.env, KIROCREW_HOME: home, KIROCREW_PORT: "5476" },
    });
    assert.equal(run.status, 0, `driver exited ${run.status}\n${run.stdout}\n${run.stderr}`);
  };
  const files = ["config.json", "mochi-machine.json"].map((name) => path.join(userData, name));
  const modes = () => files.map((file) => `${path.basename(file)}=${octal(modeOf(file))}`);

  // A fresh install creates both files and saves config.json again.
  boot();
  assert.deepEqual(modes(), ["config.json=600", "mochi-machine.json=600"], "fresh install");

  // Complete files an earlier save left at 0666: the next boot tightens them.
  for (const file of files) fs.chmodSync(file, 0o666);
  boot();
  assert.deepEqual(modes(), ["config.json=600", "mochi-machine.json=600"], "boot repair");
  const log = fs.readFileSync(path.join(logsDir, "gateway-launch.log"), "utf8");
  assert.match(log, /store mode: config\.json mode 666 tightened to 600/);
});
