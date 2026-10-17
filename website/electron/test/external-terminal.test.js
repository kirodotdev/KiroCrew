"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { EventEmitter } = require("node:events");

const MODULE_PATH = path.join(__dirname, "..", "external-terminal.js");
const SOURCE = fs.readFileSync(MODULE_PATH, "utf8");
const PRELOAD_SOURCE = fs.readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");
const {
  GRANT_REDEEM_PATH,
  LINUX_BIN_DIRS,
  TERMINALS,
  resolveTerminal,
  detectTerminals,
  redeemGrant,
  launchDirectory,
  launchExternalTerminal,
  launchFromGrant,
} = require(MODULE_PATH);

const PROJECT = "/home/user/work/project";

/**
 * A tiny in-memory filesystem: `entries` maps an absolute path to
 * "file" (executable), "plain" (not executable), "dir", or {link: target}.
 * Every path the code probes is recorded in `probed`.
 */
function fakeFs(entries = {}) {
  const probed = [];
  const enoent = (p) => Object.assign(new Error(`ENOENT: ${p}`), { code: "ENOENT" });
  const resolve = (p) => {
    let cur = p;
    for (let hops = 0; hops < 8; hops += 1) {
      const e = entries[cur];
      if (e && typeof e === "object" && e.link) {
        cur = e.link;
        continue;
      }
      return cur;
    }
    return cur;
  };
  return {
    probed,
    constants: { X_OK: 1 },
    statSync(p) {
      probed.push(p);
      const real = resolve(p);
      const e = entries[real];
      if (!e) throw enoent(p);
      return { isFile: () => e === "file" || e === "plain", isDirectory: () => e === "dir" };
    },
    accessSync(p) {
      probed.push(p);
      const e = entries[resolve(p)];
      if (e !== "file") throw Object.assign(new Error(`EACCES: ${p}`), { code: "EACCES" });
    },
    realpathSync(p) {
      const real = resolve(p);
      if (!entries[real]) throw enoent(p);
      return real;
    },
  };
}

/** A spawn stand-in: records its call and emits `outcome` on the next tick. */
function fakeSpawn(outcome = "spawn") {
  const calls = [];
  const children = [];
  const spawnFn = (bin, args, options) => {
    calls.push({ bin, args, options });
    if (outcome === "throw") throw Object.assign(new Error("spawn EINVAL"), { code: "EINVAL" });
    const child = new EventEmitter();
    child.unrefCalls = 0;
    child.unref = () => { child.unrefCalls += 1; };
    children.push(child);
    setImmediate(() => {
      if (outcome === "spawn") child.emit("spawn");
      else child.emit("error", Object.assign(new Error(`spawn ${bin} ENOENT`), { code: "ENOENT", path: bin }));
    });
    return child;
  };
  return { spawnFn, calls, children };
}

const MAC_KITTY = "/Applications/kitty.app/Contents/MacOS/kitty";
const MAC_WEZTERM = "/Applications/WezTerm.app/Contents/MacOS/wezterm";
const MAC_ALACRITTY = "/Applications/Alacritty.app/Contents/MacOS/alacritty";

// ── allowlist resolution ──────────────────────────────────────────────────

test("an allowlisted app resolves to its fixed install path", () => {
  const darwin = { platform: "darwin", fs: fakeFs({ [MAC_KITTY]: "file" }) };
  assert.equal(resolveTerminal("kitty", darwin).bin, MAC_KITTY);
  const linux = { platform: "linux", fs: fakeFs({ "/usr/local/bin/kitty": "file" }) };
  assert.equal(resolveTerminal("kitty", linux).bin, "/usr/local/bin/kitty");
});

test("an id outside the allowlist never resolves, even when a binary of that name exists", () => {
  const deps = {
    platform: "linux",
    fs: fakeFs({ "/usr/bin/bash": "file", "/bin/sh": "file", "/usr/bin/xterm": "file" }),
  };
  for (const id of ["bash", "/bin/sh", "../../bin/sh", "xterm", "", "KITTY", null, undefined, 7, {}]) {
    assert.equal(resolveTerminal(id, deps), null, `id ${String(id)} must not resolve`);
  }
});

test("only fixed system install paths are probed, never PATH or the home directory", () => {
  for (const platform of ["darwin", "linux", "win32"]) {
    const f = fakeFs({});
    for (const t of TERMINALS) resolveTerminal(t.id, { platform, fs: f });
    for (const probed of f.probed) {
      assert.ok(path.isAbsolute(probed), `${probed} is absolute`);
      const allowed = probed.startsWith("/Applications/")
        || LINUX_BIN_DIRS.includes(path.posix.dirname(probed));
      assert.ok(allowed, `${platform} probed ${probed}, outside the fixed install locations`);
      assert.doesNotMatch(probed, /\/home\/|\/Users\/|\.local|homebrew/);
    }
  }
});

test("an app without an entry for this platform does not resolve", () => {
  // Ghostty on macOS can only start through `open -na`, so it has no macOS entry.
  const f = fakeFs({ "/Applications/Ghostty.app/Contents/MacOS/ghostty": "file" });
  assert.equal(resolveTerminal("ghostty", { platform: "darwin", fs: f }), null);
  // No Windows entries at all.
  for (const t of TERMINALS) {
    assert.equal(t.candidates.win32, undefined, `${t.id} has no Windows entry`);
  }
});

test("detection lists only installed, executable apps in allowlist order", () => {
  const f = fakeFs({
    [MAC_WEZTERM]: "file",
    [MAC_KITTY]: "file",
    [MAC_ALACRITTY]: "plain",
    "/Applications/Ghostty.app/Contents/MacOS/ghostty": "file",
  });
  assert.deepEqual(detectTerminals({ platform: "darwin", fs: f }), [
    { id: "kitty", label: "kitty" },
    { id: "wezterm", label: "WezTerm" },
  ]);
});

test("a directory at an install path is not an executable", () => {
  const f = fakeFs({ "/usr/bin/konsole": "dir" });
  assert.deepEqual(detectTerminals({ platform: "linux", fs: f }), []);
});

test("a symlinked install (Snap) resolves through its link", () => {
  const f = fakeFs({ "/snap/bin/alacritty": { link: "/usr/bin/snap" }, "/usr/bin/snap": "file" });
  const hit = resolveTerminal("alacritty", { platform: "linux", fs: f });
  assert.equal(hit.bin, "/snap/bin/alacritty", "the link path, not its target, is started");
});

// ── argv pinned per app ───────────────────────────────────────────────────

test("every app's argv is pinned", () => {
  const expected = {
    alacritty: ["--working-directory", PROJECT],
    ghostty: [`--working-directory=${PROJECT}`],
    "gnome-terminal": [`--working-directory=${PROJECT}`],
    kitty: ["--directory", PROJECT],
    konsole: ["--workdir", PROJECT],
    wezterm: ["start", "--cwd", PROJECT],
    "xfce4-terminal": [`--working-directory=${PROJECT}`],
  };
  assert.deepEqual(TERMINALS.map((t) => t.id).sort(), Object.keys(expected).sort());
  for (const t of TERMINALS) {
    assert.deepEqual(t.args(PROJECT), expected[t.id], `${t.id} argv`);
  }
  assert.ok(Object.isFrozen(TERMINALS), "the allowlist cannot be extended at run time");
});

// ── the launch ────────────────────────────────────────────────────────────

test("a launch starts the app's own executable with the pinned argv and the project as cwd", async () => {
  const s = fakeSpawn("spawn");
  const f = fakeFs({ [MAC_KITTY]: "file", [PROJECT]: "dir" });
  const result = await launchExternalTerminal("kitty", PROJECT, { platform: "darwin", fs: f, spawnFn: s.spawnFn });
  assert.deepEqual(result, { ok: true, app: "kitty" });
  assert.equal(s.calls.length, 1);
  const [{ bin, args, options }] = s.calls;
  assert.equal(bin, MAC_KITTY);
  assert.deepEqual(args, ["--directory", PROJECT]);
  assert.equal(options.cwd, PROJECT);
  assert.equal(options.shell, false);
  assert.equal(options.detached, true);
  assert.equal(options.stdio, "ignore");
  assert.equal(s.children[0].unrefCalls, 1, "the terminal is not tied to this app's lifetime");
});

test("the launch uses the directory's realpath for both cwd and argv", async () => {
  const s = fakeSpawn("spawn");
  const f = fakeFs({
    "/usr/bin/wezterm": "file",
    "/home/user/link": { link: PROJECT },
    [PROJECT]: "dir",
  });
  await launchExternalTerminal("wezterm", "/home/user/link", { platform: "linux", fs: f, spawnFn: s.spawnFn });
  assert.equal(s.calls[0].options.cwd, PROJECT);
  assert.deepEqual(s.calls[0].args, ["start", "--cwd", PROJECT]);
});

test("a project path that is a file, missing, or relative fails closed without a spawn", async () => {
  const f = fakeFs({
    "/usr/bin/kitty": "file",
    "/tmp/payload.command": "file",
    "/home/user/swapped": { link: "/tmp/payload.command" },
  });
  for (const dir of ["/tmp/payload.command", "/home/user/swapped", "/nope", "relative/dir", "", null, 42]) {
    const s = fakeSpawn("spawn");
    const result = await launchExternalTerminal("kitty", dir, { platform: "linux", fs: f, spawnFn: s.spawnFn });
    assert.deepEqual(result, { ok: false, error: "bad-directory" }, `dir ${String(dir)}`);
    assert.equal(s.calls.length, 0, `dir ${String(dir)} must never reach spawn`);
  }
  assert.equal(launchDirectory("/tmp/payload.command", f), null);
});

test("an unknown or uninstalled app fails without a spawn", async () => {
  const f = fakeFs({ [PROJECT]: "dir" });
  for (const id of ["kitty", "bash", ""]) {
    const s = fakeSpawn("spawn");
    const result = await launchExternalTerminal(id, PROJECT, { platform: "linux", fs: f, spawnFn: s.spawnFn });
    assert.deepEqual(result, { ok: false, error: "no-app" });
    assert.equal(s.calls.length, 0);
  }
});

test("a process that does not start reports launch-failed", async () => {
  const f = fakeFs({ "/usr/bin/konsole": "file", [PROJECT]: "dir" });
  for (const outcome of ["error", "throw"]) {
    const s = fakeSpawn(outcome);
    const result = await launchExternalTerminal("konsole", PROJECT, { platform: "linux", fs: f, spawnFn: s.spawnFn });
    assert.deepEqual(result, { ok: false, error: "launch-failed" }, outcome);
  }
});

test("logs name the app, never the directory or the argv", async () => {
  const f = fakeFs({ "/usr/bin/kitty": "file", [PROJECT]: "dir", "/tmp/a-file": "file" });
  const logs = [];
  const log = (line) => logs.push(line);
  for (const [outcome, dir] of [["spawn", PROJECT], ["error", PROJECT], ["throw", PROJECT], ["spawn", "/tmp/a-file"]]) {
    await launchExternalTerminal("kitty", dir, { platform: "linux", fs: f, spawnFn: fakeSpawn(outcome).spawnFn, log });
  }
  assert.ok(logs.length >= 4);
  for (const line of logs) {
    assert.match(line, /kitty/);
    assert.doesNotMatch(line, /\/home\/user|\/tmp\/a-file|--directory|\/usr\/bin/);
  }
});

// ── the path never reaches an OS opener ───────────────────────────────────

test("no OS opener, document handler, or shell string is reachable from the launcher", async () => {
  const OPENERS = new Set(["open", "xdg-open", "gio", "start", "cmd", "cmd.exe", "osascript", "explorer", "explorer.exe", "sh", "bash"]);
  for (const t of TERMINALS) {
    for (const bins of Object.values(t.candidates)) {
      for (const bin of bins) {
        assert.ok(!OPENERS.has(path.posix.basename(bin)), `${t.id} starts ${bin}, an opener or shell`);
      }
    }
  }
  // The source holds no opener call and no shell-string construction.
  for (const banned of [
    /openPath/, /openExternal/, /showItemInFolder/, /xdg-open/, /osascript/,
    /\/usr\/bin\/open\b/, /["']open["']/, /\bexecSync\b/, /\bexec\(/, /shell:\s*true/,
    /require\(\s*["']electron["']\s*\)/,
  ]) {
    const code = SOURCE.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
    assert.doesNotMatch(code, banned, `external-terminal.js must not contain ${banned}`);
  }
  // At run time the project directory reaches spawn only as cwd and argv values.
  const s = fakeSpawn("spawn");
  const f = fakeFs({ "/usr/bin/gnome-terminal": "file", [PROJECT]: "dir" });
  await launchExternalTerminal("gnome-terminal", PROJECT, { platform: "linux", fs: f, spawnFn: s.spawnFn });
  assert.notEqual(s.calls[0].bin, PROJECT);
  assert.ok(!OPENERS.has(path.posix.basename(s.calls[0].bin)));
});

// ── launching only on the gateway's approval ──────────────────────────────

const GRANT = "A".repeat(43);
const BACKEND = "http://localhost:5476";

/** A fetch stand-in answering the redemption with `status` and `body`. */
function fakeFetch(status = 200, body = { app: "kitty", cwd: PROJECT }) {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, init });
    if (status === "throw") throw new Error("ECONNREFUSED");
    return { status, json: async () => body };
  };
  return { fetchFn, calls };
}

test("a grant is redeemed at this shell's gateway with the internal secret", async () => {
  const f = fakeFetch();
  const approved = await redeemGrant(GRANT, { backendUrl: BACKEND, getSecret: () => "s3cret", fetchFn: f.fetchFn });
  assert.deepEqual(approved, { app: "kitty", cwd: PROJECT });
  assert.equal(f.calls.length, 1);
  const [{ url, init }] = f.calls;
  assert.equal(url, `${BACKEND}${GRANT_REDEEM_PATH}`);
  assert.equal(GRANT_REDEEM_PATH, "/api/terminal/external-grant");
  assert.equal(init.method, "POST");
  assert.equal(init.headers["X-Internal-Secret"], "s3cret");
  assert.deepEqual(JSON.parse(init.body), { grant: GRANT });
});

test("a malformed grant or a missing secret makes no request", async () => {
  for (const grant of ["", "short", "../../etc", `${"A".repeat(40)} ;x`, null, 7, { grant: GRANT }]) {
    const f = fakeFetch();
    assert.equal(await redeemGrant(grant, { backendUrl: BACKEND, getSecret: () => "s", fetchFn: f.fetchFn }), null);
    assert.equal(f.calls.length, 0, `grant ${String(grant)}`);
  }
  const f = fakeFetch();
  assert.equal(await redeemGrant(GRANT, { backendUrl: BACKEND, getSecret: () => "", fetchFn: f.fetchFn }), null);
  assert.equal(f.calls.length, 0);
});

test("every gateway refusal or malformed answer redeems nothing", async () => {
  const cases = [
    [403, { code: "internal_secret_required" }],
    [404, { code: "external_terminal_no_grant" }],
    [409, { code: "external_terminal_not_configured" }],
    [200, { app: "kitty" }],
    [200, { cwd: PROJECT }],
    [200, null],
    ["throw", null],
  ];
  for (const [status, body] of cases) {
    const f = fakeFetch(status, body);
    assert.equal(
      await redeemGrant(GRANT, { backendUrl: BACKEND, getSecret: () => "s", fetchFn: f.fetchFn }),
      null,
      `${status} ${JSON.stringify(body)}`,
    );
  }
});

test("nothing starts without the gateway's approval", async () => {
  const s = fakeSpawn("spawn");
  const fsys = fakeFs({ "/usr/bin/kitty": "file", [PROJECT]: "dir" });
  const deps = { backendUrl: BACKEND, getSecret: () => "s", platform: "linux", fs: fsys, spawnFn: s.spawnFn };
  for (const status of [403, 404, 409]) {
    const result = await launchFromGrant(GRANT, { ...deps, fetchFn: fakeFetch(status, {}).fetchFn });
    assert.deepEqual(result, { ok: false, error: "no-grant" });
  }
  assert.equal(s.calls.length, 0);
});

test("an approved grant starts the app it names in the directory it names", async () => {
  const s = fakeSpawn("spawn");
  const fsys = fakeFs({ "/usr/bin/kitty": "file", [PROJECT]: "dir" });
  const result = await launchFromGrant(GRANT, {
    backendUrl: BACKEND, getSecret: () => "s", fetchFn: fakeFetch().fetchFn,
    platform: "linux", fs: fsys, spawnFn: s.spawnFn,
  });
  assert.deepEqual(result, { ok: true, app: "kitty" });
  assert.equal(s.calls[0].bin, "/usr/bin/kitty");
  assert.deepEqual(s.calls[0].args, ["--directory", PROJECT]);
  assert.equal(s.calls[0].options.cwd, PROJECT);
});

test("an approval is still checked here: an unlisted app or a non-directory starts nothing", async () => {
  const fsys = fakeFs({ "/usr/bin/kitty": "file", "/usr/bin/xterm": "file", "/tmp/f": "file", [PROJECT]: "dir" });
  for (const [body, error] of [
    [{ app: "xterm", cwd: PROJECT }, "no-app"],
    [{ app: "/usr/bin/kitty", cwd: PROJECT }, "no-app"],
    [{ app: "kitty", cwd: "/tmp/f" }, "bad-directory"],
    [{ app: "kitty", cwd: "relative" }, "bad-directory"],
  ]) {
    const s = fakeSpawn("spawn");
    const result = await launchFromGrant(GRANT, {
      backendUrl: BACKEND, getSecret: () => "s", fetchFn: fakeFetch(200, body).fetchFn,
      platform: "linux", fs: fsys, spawnFn: s.spawnFn,
    });
    assert.deepEqual(result, { ok: false, error }, JSON.stringify(body));
    assert.equal(s.calls.length, 0);
  }
});

test("the preload bridge carries only the grant to the main process", () => {
  assert.match(PRELOAD_SOURCE, /detect:\s*\(\)\s*=>\s*ipcRenderer\.invoke\("external-terminal:detect"\)/);
  assert.match(
    PRELOAD_SOURCE,
    /open:\s*\(grant\)\s*=>\s*ipcRenderer\.invoke\("external-terminal:open",\s*String\(grant \|\| ""\)\)/,
  );
});
