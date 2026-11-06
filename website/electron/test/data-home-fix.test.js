"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const {
  elevationRoute,
  repairTarget,
  elevatedCommand,
  sameHome,
  canFix,
  fixDataHome,
} = require(path.join(__dirname, "..", "data-home-fix.js"));

const HOME = "/srv/crew-data";

/** In-memory fs: `entries` maps an absolute path to "file", "dir" or {link: target}. */
function fakeFs(entries) {
  const kind = (p) => entries[p];
  const stat = (p, follow) => {
    let k = kind(p);
    if (follow && k && typeof k === "object") k = kind(k.link);
    if (!k) { const e = new Error(`ENOENT ${p}`); e.code = "ENOENT"; throw e; }
    return {
      isFile: () => k === "file",
      isDirectory: () => k === "dir",
      isSymbolicLink: () => typeof k === "object",
    };
  };
  return {
    statSync: (p) => stat(p, true),
    lstatSync: (p) => stat(p, false),
    realpathSync: (p) => (typeof kind(p) === "object" ? kind(p).link : p),
  };
}

const MAC = { "/usr/bin/osascript": "file", "/usr/sbin/chown": "file", "/usr/bin/find": "file", "/bin/chmod": "file", [HOME]: "dir" };
const LINUX = { "/usr/bin/pkexec": "file", "/usr/bin/chown": "file", "/usr/bin/find": "file", "/bin/chmod": "file", [HOME]: "dir" };

/** The privileged find: another account's entries only, never a hard-linked file or a link's target. */
const narrowed = (find, chown, uid, target) => [
  find, target, "-xdev", "!", "-user", String(uid),
  "(", "-type", "d", "-o", "-links", "1", ")",
  "-exec", chown, "-h", String(uid), "{}", "+",
];

/** A fake execFile that records each call and answers from `answers` in order. */
function fakeExec(answers = []) {
  const calls = [];
  const execFile = (file, args, _opts, cb) => {
    calls.push([file, args]);
    const answer = answers.shift() || {};
    cb(answer.error || null, "", answer.stderr || "");
  };
  return { calls, execFile };
}

test("macOS and Linux offer a route only where their prompt and chown exist", () => {
  assert.equal(elevationRoute({ platform: "darwin", fs: fakeFs(MAC) }).kind, "osascript");
  assert.equal(elevationRoute({ platform: "linux", fs: fakeFs(LINUX) }).kind, "pkexec");
  assert.equal(elevationRoute({ platform: "linux", fs: fakeFs({ "/usr/bin/chown": "file" }) }), null);
  const noFind = { ...LINUX };
  delete noFind["/usr/bin/find"];
  assert.equal(elevationRoute({ platform: "linux", fs: fakeFs(noFind) }), null);
  assert.equal(elevationRoute({ platform: "win32", fs: fakeFs(MAC) }), null);
});

test("the target is the data home's real directory, never a link or a root", () => {
  assert.equal(repairTarget(HOME, { fs: fakeFs({ [HOME]: "dir" }) }), HOME);
  assert.throws(() => repairTarget(HOME, { fs: fakeFs({ [HOME]: { link: "/" }, "/": "dir" }) }), /link/);
  assert.throws(() => repairTarget(HOME, { fs: fakeFs({ [HOME]: "file" }) }), /not a directory/);
  assert.throws(() => repairTarget(HOME, { fs: fakeFs({}) }), /ENOENT/);
  assert.throws(() => repairTarget("/", { fs: fakeFs({ "/": "dir" }) }), /root/);
});

test("pkexec runs only the narrowed find, chowning another account's entries to this uid", () => {
  const route = { kind: "pkexec", bin: "/usr/bin/pkexec", chown: "/usr/bin/chown", find: "/usr/bin/find" };
  const [file, args] = elevatedCommand(route, 501, HOME);
  assert.equal(file, "/usr/bin/pkexec");
  assert.deepEqual(args, narrowed("/usr/bin/find", "/usr/bin/chown", 501, HOME));
  assert.ok(!args.includes("-R") && !args.includes("-L"), "nothing recursive follows a link");
});

test("osascript quotes the path for both the shell and AppleScript", () => {
  const odd = `/srv/it's "crew"`;
  const route = { kind: "osascript", bin: "/usr/bin/osascript", chown: "/usr/sbin/chown", find: "/usr/bin/find" };
  const [file, args] = elevatedCommand(route, 501, odd);
  assert.equal(file, "/usr/bin/osascript");
  assert.equal(args[0], "-e");
  assert.equal(
    args[1],
    `do shell script "'/usr/bin/find' '/srv/it'\\\\''s \\"crew\\"' '-xdev' '!' '-user' '501' '(' '-type' 'd' '-o' '-links' '1' ')' '-exec' '/usr/sbin/chown' '-h' '501' '{}' '+'" with administrator privileges`,
  );
});

test("root or an unknown uid is never the account the files go to", () => {
  const route = { kind: "pkexec", bin: "/usr/bin/pkexec", chown: "/usr/bin/chown", find: "/usr/bin/find" };
  assert.throws(() => elevatedCommand(route, 0, HOME));
  assert.throws(() => elevatedCommand(route, -1, HOME));
});

test("a gateway on another data home gets no repair here", async () => {
  const fs = fakeFs({ ...LINUX, "/srv/other-data": "dir" });
  assert.equal(canFix({ gatewayHome: HOME, platform: "linux", fs, home: HOME }), true);
  for (const gatewayHome of ["/srv/other-data", "", undefined, "/srv/missing"]) {
    assert.equal(canFix({ gatewayHome, platform: "linux", fs, home: HOME }), false, String(gatewayHome));
    const exec = fakeExec();
    assert.deepEqual(
      await fixDataHome({ gatewayHome, platform: "linux", fs, execFile: exec.execFile, getuid: () => 1000, home: HOME }),
      { ok: false, reason: "unavailable" },
    );
    assert.equal(exec.calls.length, 0, `${gatewayHome}: nothing may run`);
  }
});

test("a link to the same directory counts as the same data home", () => {
  const fs = fakeFs({ ...LINUX, "/srv/crew-link": { link: HOME } });
  assert.equal(sameHome("/srv/crew-link", HOME, { fs }), true);
});

test("a successful fix chowns with privilege, then restores modes as the person", async () => {
  const exec = fakeExec();
  const result = await fixDataHome({ gatewayHome: HOME, platform: "linux", fs: fakeFs(LINUX), execFile: exec.execFile, getuid: () => 1000, home: HOME });
  assert.deepEqual(result, { ok: true });
  assert.deepEqual(exec.calls, [
    ["/usr/bin/pkexec", narrowed("/usr/bin/find", "/usr/bin/chown", 1000, HOME)],
    ["/bin/chmod", ["-R", "u+rwX", HOME]],
  ]);
});

test("a chmod that fails or is missing is a failed fix, not a fixed one", async () => {
  const failing = fakeExec([{}, { error: Object.assign(new Error("exit 1"), { code: 1 }) }]);
  assert.deepEqual(
    await fixDataHome({ gatewayHome: HOME, platform: "linux", fs: fakeFs(LINUX), execFile: failing.execFile, getuid: () => 1000, home: HOME }),
    { ok: false, reason: "failed" },
  );
  const noChmod = { ...LINUX };
  delete noChmod["/bin/chmod"];
  const missing = fakeExec();
  assert.deepEqual(
    await fixDataHome({ gatewayHome: HOME, platform: "linux", fs: fakeFs(noChmod), execFile: missing.execFile, getuid: () => 1000, home: HOME }),
    { ok: false, reason: "failed" },
  );
});

test("a dismissed admin prompt is reported as cancelled and changes nothing else", async () => {
  const mac = fakeExec([{ error: Object.assign(new Error("exit 1"), { code: 1 }), stderr: "execution error: User canceled. (-128)" }]);
  assert.deepEqual(
    await fixDataHome({ gatewayHome: HOME, platform: "darwin", fs: fakeFs(MAC), execFile: mac.execFile, getuid: () => 501, home: HOME }),
    { ok: false, reason: "cancelled" },
  );
  assert.equal(mac.calls.length, 1);
  const linux = fakeExec([{ error: Object.assign(new Error("exit 126"), { code: 126 }) }]);
  assert.deepEqual(
    await fixDataHome({ gatewayHome: HOME, platform: "linux", fs: fakeFs(LINUX), execFile: linux.execFile, getuid: () => 1000, home: HOME }),
    { ok: false, reason: "cancelled" },
  );
});

test("nothing runs without a route, as root, or on a linked data home", async () => {
  for (const [label, opts] of [
    ["no route", { platform: "win32", fs: fakeFs(MAC), getuid: () => 501 }],
    ["root", { platform: "darwin", fs: fakeFs(MAC), getuid: () => 0 }],
    ["link", { platform: "darwin", fs: fakeFs({ ...MAC, [HOME]: { link: "/etc" }, "/etc": "dir" }), getuid: () => 501 }],
  ]) {
    const exec = fakeExec();
    const result = await fixDataHome({ ...opts, gatewayHome: HOME, execFile: exec.execFile, home: HOME });
    assert.equal(result.ok, false, label);
    assert.equal(exec.calls.length, 0, `${label}: nothing may run`);
  }
});
