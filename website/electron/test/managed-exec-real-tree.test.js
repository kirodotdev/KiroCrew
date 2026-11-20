"use strict";

// Windows-only integration test for the managed command timeout. The other
// timeout tests stub child_process; this one runs a real command under the
// real pinned cmd.exe, so it runs only on a Windows host (CI: the
// electron-test-windows job) and is skipped elsewhere.
//
// The premise: on Windows the managed command's child is cmd.exe, and ending
// cmd.exe alone leaves the program it started running and holding the pipes,
// so the call would settle only when that program ends by itself. The timeout
// must end the whole tree, so the call settles near the ceiling and nothing
// it started is left running.

const { test } = require("node:test");
const assert = require("node:assert");
const { execFileSync } = require("node:child_process");

const { createManagedExec } = require("../runtime/update/managed-exec");

const IS_WIN = process.platform === "win32";
const skip = IS_WIN ? false : "the managed command runs under cmd.exe only on Windows";

const SYSTEM32 = `${process.env.SystemRoot || "C:\\Windows"}\\System32`;

// PIDs of running processes with this image name, read from tasklist.
function pidsOf(image) {
  const out = execFileSync(`${SYSTEM32}\\tasklist.exe`, ["/FI", `IMAGENAME eq ${image}`, "/FO", "CSV", "/NH"], {
    encoding: "utf8",
    windowsHide: true,
  });
  const pids = [];
  for (const m of out.matchAll(/^"[^"]+","(\d+)"/gm)) pids.push(Number(m[1]));
  return pids;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test("on Windows a timeout ends the real cmd.exe tree and settles near the ceiling", { skip, timeout: 30_000 }, async () => {
  const lines = [];
  const log = { info: (m) => lines.push(String(m)), error: (m) => lines.push(String(m)) };
  const exec = createManagedExec({
    managed: { updateCommand: "", checkCommand: "" },
    managedPath: () => process.env.PATH,
    log,
  });
  const pingsBefore = new Set(pidsOf("PING.EXE"));
  let started = [];
  try {
    const t0 = Date.now();
    // About 8 s: ping waits one second between its nine echoes.
    const run = exec.runManagedCommand(`"${SYSTEM32}\\PING.EXE" -n 9 127.0.0.1`, { timeout: 1000, label: "update command" });
    await sleep(500);
    started = pidsOf("PING.EXE").filter((p) => !pingsBefore.has(p));
    const r = await run;
    const settledMs = Date.now() - t0;

    assert.ok(started.length > 0, "the command's PING.EXE was never seen running, so the test proves nothing");
    assert.ok(settledMs < 4000, `settled after ${settledMs} ms; the 1000 ms ceiling did not end the command`);
    assert.deepStrictEqual({ timedOut: r.timedOut, failed: r.failed }, { timedOut: true, failed: true });

    const cmdLine = lines.find((l) => l.startsWith("[update] managed update command started (pid "));
    assert.ok(cmdLine, lines.join("\n"));
    const cmdPid = Number(cmdLine.slice("[update] managed update command started (pid ".length, -1));

    // The tree is gone once the call settles; allow the kernel a moment to
    // reap it before reading the process list.
    let leftPings = started;
    let cmdAlive = true;
    for (let i = 0; i < 10 && (leftPings.length || cmdAlive); i += 1) {
      const pings = pidsOf("PING.EXE");
      leftPings = started.filter((p) => pings.includes(p));
      cmdAlive = pidsOf("cmd.exe").includes(cmdPid);
      if (leftPings.length || cmdAlive) await sleep(100);
    }
    assert.deepStrictEqual(leftPings, [], "the command cmd.exe started is still running after the timeout");
    assert.strictEqual(cmdAlive, false, `cmd.exe pid ${cmdPid} is still running after the timeout`);
  } finally {
    const pings = pidsOf("PING.EXE");
    for (const p of started.filter((p) => pings.includes(p))) {
      try {
        execFileSync(`${SYSTEM32}\\taskkill.exe`, ["/F", "/PID", String(p)], { windowsHide: true, stdio: "ignore" });
      } catch { /* already gone */ }
    }
  }
});
