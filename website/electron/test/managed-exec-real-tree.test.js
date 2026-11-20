"use strict";

// Windows-only integration test for the managed command timeout. The other
// timeout tests stub child_process; this one runs a real command under the
// real pinned cmd.exe, so it runs only on a Windows host (CI: the
// electron-test-windows job) and is skipped elsewhere.
//
// The premise: on Windows the managed command's child is cmd.exe, and ending
// cmd.exe alone leaves the program it started running and holding the pipes,
// so the call would settle only when that program ends by itself. The timeout
// must end the whole tree, so nothing it started is left running and the
// program's own work is cut short.
//
// No clock is read: the program is `ping -n 31`, which prints one line naming
// 127.0.0.1 per echo, one second apart, plus a header and a statistics line.
// A run left to finish prints 33 such lines; a run the timeout cut short
// prints a few. Host load can only delay the kill, never make a cut-short run
// look like a finished one.
//
// Only processes this test started are read or ended: the cmd.exe pid the
// run logs, and the PING.EXE whose parent is that cmd.exe.

const { test } = require("node:test");
const assert = require("node:assert");
const { execFile } = require("node:child_process");

const { createManagedExec } = require("../runtime/update/managed-exec");

const IS_WIN = process.platform === "win32";
const skip = IS_WIN ? false : "the managed command runs under cmd.exe only on Windows";

const SYSTEM32 = `${process.env.SystemRoot || "C:\\Windows"}\\System32`;
const ECHOES = 31;

function run(file, args) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { encoding: "utf8", windowsHide: true, timeout: 15_000 }, (err, out) => {
      if (err) reject(err);
      else resolve(String(out));
    });
  });
}

// True while a process with this pid and image name is running.
async function isRunning(pid, image) {
  const out = await run(`${SYSTEM32}\\tasklist.exe`, ["/FI", `PID eq ${pid}`, "/FO", "CSV", "/NH"]);
  return out.toUpperCase().includes(`"${image.toUpperCase()}","${pid}"`);
}

// The PING.EXE pids whose parent is this cmd.exe.
async function pingsUnder(cmdPid) {
  const out = await run(`${SYSTEM32}\\WindowsPowerShell\\v1.0\\powershell.exe`, [
    "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
    `Get-CimInstance Win32_Process -Filter "ParentProcessId = ${cmdPid} AND Name = 'PING.EXE'" | ForEach-Object { $_.ProcessId }`,
  ]);
  return out.split(/\s+/).filter(Boolean).map(Number);
}

function taskkill(pid) {
  return new Promise((resolve) => {
    execFile(`${SYSTEM32}\\taskkill.exe`, ["/F", "/PID", String(pid)], { windowsHide: true }, () => resolve());
  });
}

// The interval between reads of the real OS process list, which has no event
// to await; each loop that uses it is bounded or ends when the run settles.
const tick = () => new Promise((r) => setTimeout(r, 100)); // flake-ok: poll interval for real tasklist reads, no event exists

test("on Windows a timeout ends the real cmd.exe tree before its command finishes", { skip, timeout: 90_000 }, async () => {
  const lines = [];
  const log = { info: (m) => lines.push(String(m)), error: (m) => lines.push(String(m)) };
  const exec = createManagedExec({
    managed: { updateCommand: "", checkCommand: "" },
    managedPath: () => process.env.PATH,
    log,
  });
  let owned = [];
  try {
    let settled = false;
    // The ceiling leaves time to find the ping through its parent link
    // (one PowerShell start per read) while the run is still going.
    const pending = exec.runManagedCommand(`"${SYSTEM32}\\PING.EXE" -n ${ECHOES} 127.0.0.1`, {
      timeout: 6000,
      label: "update command",
    }).finally(() => { settled = true; });

    const started = lines.find((l) => l.startsWith("[update] managed update command started (pid "));
    assert.ok(started, lines.join("\n"));
    const cmdPid = Number(started.slice("[update] managed update command started (pid ".length, -1));
    assert.ok(Number.isInteger(cmdPid) && cmdPid > 0, started);

    while (!settled && owned.length === 0) {
      owned = await pingsUnder(cmdPid);
      if (owned.length === 0) await tick();
    }
    const r = await pending;
    assert.deepStrictEqual({ timedOut: r.timedOut, failed: r.failed }, { timedOut: true, failed: true });

    const echoed = (r.out.match(/127\.0\.0\.1/g) || []).length;
    assert.ok(owned.length > 0 || echoed > 0, `ping never ran under cmd.exe; output: ${JSON.stringify(r.out)}`);
    assert.ok(echoed < ECHOES, `ping printed ${echoed} lines naming 127.0.0.1, so it ran to the end instead of being cut short`);

    // Give the kernel up to a second to reap the tree before reading it.
    const survivors = async () => {
      const left = [];
      for (const p of owned) if (await isRunning(p, "PING.EXE")) left.push(p);
      return left;
    };
    let leftPings = await survivors();
    let cmdAlive = await isRunning(cmdPid, "cmd.exe");
    for (let i = 0; i < 10 && (leftPings.length || cmdAlive); i += 1) {
      await tick();
      leftPings = await survivors();
      cmdAlive = await isRunning(cmdPid, "cmd.exe");
    }
    assert.deepStrictEqual(leftPings, [], "the command cmd.exe started is still running after the timeout");
    assert.strictEqual(cmdAlive, false, `cmd.exe pid ${cmdPid} is still running after the timeout`);
  } finally {
    for (const p of owned) if (await isRunning(p, "PING.EXE").catch(() => false)) await taskkill(p);
  }
});
