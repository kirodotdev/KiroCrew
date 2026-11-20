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
// No clock is read: the program is `ping -n 21`, which prints one line naming
// 127.0.0.1 per echo, one second apart, plus a header and a statistics line.
// A run left to finish prints 23 such lines; a run the timeout cut short
// prints a few. Host load can only delay the kill, never make a cut-short run
// look like a finished one.

const { test } = require("node:test");
const assert = require("node:assert");
const { execFile } = require("node:child_process");

const { createManagedExec } = require("../runtime/update/managed-exec");

const IS_WIN = process.platform === "win32";
const skip = IS_WIN ? false : "the managed command runs under cmd.exe only on Windows";

const SYSTEM32 = `${process.env.SystemRoot || "C:\\Windows"}\\System32`;
const ECHOES = 21;

// PIDs of running processes with this image name, read from tasklist.
function pidsOf(image) {
  return new Promise((resolve, reject) => {
    execFile(`${SYSTEM32}\\tasklist.exe`, ["/FI", `IMAGENAME eq ${image}`, "/FO", "CSV", "/NH"], {
      encoding: "utf8",
      windowsHide: true,
    }, (err, out) => {
      if (err) return reject(err);
      const pids = [];
      for (const m of String(out).matchAll(/^"[^"]+","(\d+)"/gm)) pids.push(Number(m[1]));
      resolve(pids);
    });
  });
}

function taskkill(pid) {
  return new Promise((resolve) => {
    execFile(`${SYSTEM32}\\taskkill.exe`, ["/F", "/PID", String(pid)], { windowsHide: true }, () => resolve());
  });
}

// The interval between reads of the real OS process list, which has no event
// to await; each loop that uses it is bounded or ends when the run settles.
const tick = () => new Promise((r) => setTimeout(r, 100)); // flake-ok: poll interval for real tasklist reads, no event exists

test("on Windows a timeout ends the real cmd.exe tree before its command finishes", { skip, timeout: 60_000 }, async () => {
  const lines = [];
  const log = { info: (m) => lines.push(String(m)), error: (m) => lines.push(String(m)) };
  const exec = createManagedExec({
    managed: { updateCommand: "", checkCommand: "" },
    managedPath: () => process.env.PATH,
    log,
  });
  const pingsBefore = new Set(await pidsOf("PING.EXE"));
  const ours = async () => (await pidsOf("PING.EXE")).filter((p) => !pingsBefore.has(p));
  try {
    let settled = false;
    const run = exec.runManagedCommand(`"${SYSTEM32}\\PING.EXE" -n ${ECHOES} 127.0.0.1`, {
      timeout: 2000,
      label: "update command",
    }).finally(() => { settled = true; });
    // Watch for the ping while the run lasts, so the test knows it really ran
    // even if ping's piped output is lost when it is killed.
    let seenRunning = false;
    while (!settled && !seenRunning) {
      seenRunning = (await ours()).length > 0;
      if (!seenRunning) await tick();
    }
    const r = await run;
    assert.deepStrictEqual({ timedOut: r.timedOut, failed: r.failed }, { timedOut: true, failed: true });

    const echoed = (r.out.match(/127\.0\.0\.1/g) || []).length;
    assert.ok(seenRunning || echoed > 0, `ping never ran under cmd.exe; output: ${JSON.stringify(r.out)}`);
    assert.ok(echoed < ECHOES, `ping printed ${echoed} lines naming 127.0.0.1, so it ran to the end instead of being cut short`);

    const started = lines.find((l) => l.startsWith("[update] managed update command started (pid "));
    assert.ok(started, lines.join("\n"));
    const cmdPid = Number(started.slice("[update] managed update command started (pid ".length, -1));

    // Give the kernel up to a second to reap the tree before reading it.
    let leftPings = await ours();
    let cmdAlive = (await pidsOf("cmd.exe")).includes(cmdPid);
    for (let i = 0; i < 10 && (leftPings.length || cmdAlive); i += 1) {
      await tick();
      leftPings = await ours();
      cmdAlive = (await pidsOf("cmd.exe")).includes(cmdPid);
    }
    assert.deepStrictEqual(leftPings, [], "the command cmd.exe started is still running after the timeout");
    assert.strictEqual(cmdAlive, false, `cmd.exe pid ${cmdPid} is still running after the timeout`);
  } finally {
    for (const p of await ours()) await taskkill(p);
  }
});
