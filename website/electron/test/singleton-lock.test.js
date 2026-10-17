"use strict";

const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { parseLockTarget, describeSingletonLock } = require("../singleton-lock");

const HOST = "test-host.local";
const posixOnly = { skip: process.platform === "win32" };

function errno(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

const killDead = () => {
  throw errno("ESRCH");
};
const killAlive = () => true;
const killDenied = () => {
  throw errno("EPERM");
};

describe("parseLockTarget", () => {
  it("splits on the last dash so hostnames with dashes survive", () => {
    assert.deepEqual(parseLockTarget("my-mac-book.local-4242"), { host: "my-mac-book.local", pid: 4242 });
  });

  it("rejects targets without a numeric positive pid", () => {
    for (const target of ["", "nohyphen", "-123", "host-", "host-12a", "host-0", null, undefined]) {
      assert.equal(parseLockTarget(target), null, JSON.stringify(target));
    }
  });
});

describe("describeSingletonLock against a real userData dir", posixOnly, () => {
  let dir;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "kirocrew-singleton-"));
  });

  afterEach(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  function describeWith(kill) {
    return describeSingletonLock({ userDataDir: dir, hostname: HOST, kill });
  }

  function writeLock(target) {
    fs.symlinkSync(target, path.join(dir, "SingletonLock"));
  }

  it("reports a missing lock", () => {
    assert.equal(describeWith(killAlive), "no SingletonLock file");
  });

  it("reports a same-host lock whose pid is gone", () => {
    writeLock(HOST + "-4242");
    assert.equal(
      describeWith(killDead),
      'SingletonLock target "test-host.local-4242", host matches, pid 4242 on this machine is not running',
    );
  });

  it("reports a same-host lock whose pid is running", () => {
    writeLock(HOST + "-4242");
    assert.match(describeWith(killAlive), /host matches, pid 4242 on this machine is running$/);
  });

  it("reports a host mismatch and the local probe result", () => {
    writeLock("old-name.local-4242");
    assert.equal(
      describeWith(killDead),
      'SingletonLock target "old-name.local-4242", host differs (this host is "test-host.local"), '
        + "pid 4242 on this machine is not running",
    );
  });

  it("reports a probe that could not decide", () => {
    writeLock(HOST + "-4242");
    assert.match(describeWith(killDenied), /pid 4242 on this machine could not be probed \(EPERM\)$/);
  });

  it("reports an unparseable target", () => {
    writeLock("garbage");
    assert.equal(describeWith(killAlive), 'SingletonLock target "garbage" is unparseable');
  });

  it("reports a lock that is a regular file", () => {
    fs.writeFileSync(path.join(dir, "SingletonLock"), "not a link");
    assert.equal(describeWith(killAlive), "SingletonLock is not a symlink");
  });

  it("never removes or changes the Singleton files", () => {
    writeLock(HOST + "-4242");
    fs.symlinkSync(path.join(dir, "gone", "SingletonSocket"), path.join(dir, "SingletonSocket"));
    describeWith(killDead);
    assert.equal(fs.readlinkSync(path.join(dir, "SingletonLock")), HOST + "-4242");
    assert.ok(fs.lstatSync(path.join(dir, "SingletonSocket")).isSymbolicLink());
  });
});

describe("describeSingletonLock failure handling", () => {
  it("reports an unreadable lock", () => {
    const fakeFs = {
      readlinkSync() {
        throw errno("EACCES");
      },
    };
    assert.equal(
      describeSingletonLock({ userDataDir: "/unreadable", fs: fakeFs, hostname: HOST, kill: killAlive }),
      "SingletonLock unreadable (EACCES)",
    );
  });

  it("never throws, even when its dependencies do", () => {
    const fakeFs = { readlinkSync: () => HOST + "-4242" };
    const kill = () => {
      throw new Error("boom");
    };
    assert.match(
      describeSingletonLock({ userDataDir: "/x", fs: fakeFs, hostname: HOST, kill }),
      /pid 4242 on this machine could not be probed \(Error: boom\)$/,
    );
    const badFs = { readlinkSync: () => ({ toString() { throw new Error("weird"); } }) };
    assert.doesNotThrow(() => describeSingletonLock({ userDataDir: "/x", fs: badFs, hostname: HOST, kill: killAlive }));
  });
});
