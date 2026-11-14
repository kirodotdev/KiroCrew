"use strict";

// Windows-only probe for the premises the Windows proxy reader rests on. Every
// other test feeds hand-written `reg query` text; this one runs the real
// `reg.exe` under the kernel-named Windows directory, so it runs only on a
// Windows host (CI: the electron-test-windows job) and is skipped elsewhere.

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const childProcess = require("node:child_process");
const { parseRegQuery, readSystemProxyEnvironment, WININET_KEY } = require("../system-proxy-env");
const { windowsSystemRoot } = require("../find-bin");

const IS_WIN = process.platform === "win32";
const skip = IS_WIN ? false : "reg.exe and the WinINet key exist only on Windows";

function realRegQuery() {
  const root = windowsSystemRoot(fs);
  assert.notStrictEqual(root, null, "the kernel's \\SystemRoot link did not resolve on this host");
  return childProcess.execFileSync(`${root}\\System32\\reg.exe`, ["query", WININET_KEY], {
    encoding: "utf8",
    windowsHide: true,
  });
}

test("real reg query output parses, with ProxyEnable as a REG_DWORD", { skip }, () => {
  const out = realRegQuery();
  const values = parseRegQuery(out);
  assert.ok(values.has("proxyenable"), `ProxyEnable not found in:\n${out}`);
  const line = out.split("\n").find((l) => l.trim().toLowerCase().startsWith("proxyenable"));
  assert.ok(line && line.includes("REG_DWORD"), `ProxyEnable is not a REG_DWORD: ${line}`);
  assert.ok(values.get("proxyenable").startsWith("0x"), `unexpected ProxyEnable data: ${values.get("proxyenable")}`);
});

test("a runner with the system proxy disabled discovers nothing", { skip }, (t) => {
  const enabled = Number.parseInt(parseRegQuery(realRegQuery()).get("proxyenable"), 16) === 1;
  if (enabled) {
    t.skip("this runner has a system proxy enabled");
    return;
  }
  const env = readSystemProxyEnvironment({ execFileSync: childProcess.execFileSync, platform: "win32", fs });
  assert.strictEqual(env, null);
});
