"use strict";

// Windows-only integration probe for the one platform premise the Windows ssh
// resolver rests on. Every other `findSshBin` test fakes `realpathSync.native`;
// this one asks the real OS, so it runs only on a Windows host (CI: the
// electron-test-windows job) and is skipped elsewhere.
//
// The premise: `fs.realpathSync.native("\\?\GLOBALROOT\SystemRoot")` resolves
// the kernel's own `\SystemRoot` object link to the running Windows directory
// as a plain `X:\Windows` (after the long-path prefix is stripped), so
// `findSshBin` can name the in-box OpenSSH client under it without consulting
// `%SystemRoot%` (writable under `HKCU\Environment` without elevation) or a
// fixed `C:\Windows`.

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { findSshBin } = require("../find-bin");

const IS_WIN = process.platform === "win32";
const skip = IS_WIN ? false : "the \\SystemRoot object link and the in-box OpenSSH client exist only on Windows";

test("findSshBin resolves the kernel's \\SystemRoot link to the in-box OpenSSH client", { skip }, () => {
  const resolved = findSshBin(fs, path, true);

  // The resolver proves the GLOBALROOT premise: a non-null result means
  // `realpathSync.native` opened the object-namespace path and it passed the
  // `X:\Windows` shape check. A null result means that premise is false on this
  // host, which this assertion surfaces rather than hiding behind a mock.
  assert.notStrictEqual(
    resolved,
    null,
    "the kernel's \\SystemRoot link did not resolve to a plain X:\\Windows on this Windows host",
  );
  assert.match(
    resolved,
    /^[A-Za-z]:\\Windows\\System32\\OpenSSH\\ssh\.exe$/,
    `findSshBin returned ${resolved}, not <drive>:\\Windows\\System32\\OpenSSH\\ssh.exe`,
  );

  // The resolved root must agree with the Windows directory the kernel reports
  // through its own realpath of the same link, independent of the environment.
  const kernelRoot = fs.realpathSync.native("\\\\?\\GLOBALROOT\\SystemRoot").replace(/^\\\\\?\\/, "");
  assert.strictEqual(
    resolved,
    path.win32.join(kernelRoot, "System32", "OpenSSH", "ssh.exe"),
  );
});
