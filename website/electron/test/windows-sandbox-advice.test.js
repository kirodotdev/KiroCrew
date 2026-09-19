const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  buildSandboxFailureDialog,
  describeWindowsSandboxFailure,
  looksLikeSandboxStartupFailure,
  SANDBOX_OFF_SWITCH,
  STATUS_BREAKPOINT_SIGNED,
  STATUS_BREAKPOINT_UNSIGNED,
} = require("../windows-sandbox-advice.js");
const { createRendererRecovery } = require("../renderer-recovery.js");

const ELECTRON_DIR = path.join(__dirname, "..");
const read = (...f) => fs.readFileSync(path.join(ELECTRON_DIR, ...f), "utf8");
const GUIDE_PATH = path.join(
  ELECTRON_DIR,
  "..",
  "..",
  "docs",
  "guides",
  "windows-install.md",
);

/** A win32 death that never got a load in. */
const blocked = (over = {}) => ({
  platform: "win32",
  reason: "launch-failed",
  exitCode: STATUS_BREAKPOINT_SIGNED,
  sandboxedRendererRan: false,
  ...over,
});

//
// ── classification ───────────────────────────────────────────────────────────
//

test("a launch reason qualifies when nothing has ever loaded", () => {
  for (const reason of ["launch-failed", "integrity-failure"]) {
    assert.equal(
      looksLikeSandboxStartupFailure({ reason, sandboxedRendererRan: false }),
      true,
      reason,
    );
  }
});

test("a CHECK reported as an ordinary crash STILL qualifies before any load", () => {
  // The reported incident. A sandbox CHECK inside a child that was created
  // reads as `crashed`, not `launch-failed`, so requiring a launch reason would
  // miss the exact failure this module exists for. Both signednesses.
  for (const exitCode of [STATUS_BREAKPOINT_SIGNED, STATUS_BREAKPOINT_UNSIGNED]) {
    assert.equal(
      looksLikeSandboxStartupFailure({
        reason: "crashed",
        exitCode,
        sandboxedRendererRan: false,
      }),
      true,
      `crashed/${exitCode}`,
    );
  }
});

test("a conclusive launch failure OVERRIDES an earlier success", () => {
  // Endpoint software can start injecting mid-run — a definition update, a
  // driver DLL loaded later, a policy push. A renderer that ran an hour ago
  // says nothing about the child being replaced now, so `launch-failed` and
  // `integrity-failure`, which describe THIS child failing to launch, must beat
  // the latch. Both reviewers found this independently; the ordering of the two
  // checks in looksLikeSandboxStartupFailure is the whole fix.
  for (const reason of ["launch-failed", "integrity-failure"]) {
    assert.equal(
      looksLikeSandboxStartupFailure({ reason, sandboxedRendererRan: true }),
      true,
      `${reason} after a successful load`,
    );
  }
});

test("a successful load disqualifies an AMBIGUOUS later death", () => {
  // The V8-abort case: same exit code, and `crashed` cannot distinguish it, so
  // here the earlier success rules. Only the ambiguous signals are gated —
  // launch-failed/integrity-failure are covered by the test above.
  for (const reason of ["crashed", "oom", "abnormal-exit", "killed"]) {
    for (const exitCode of [STATUS_BREAKPOINT_SIGNED, STATUS_BREAKPOINT_UNSIGNED, 133]) {
      assert.equal(
        looksLikeSandboxStartupFailure({ reason, exitCode, sandboxedRendererRan: true }),
        false,
        `${reason}/${exitCode} after a successful load`,
      );
    }
  }
});

test("an ordinary pre-load death with an unrelated code does NOT qualify", () => {
  for (const details of [
    { reason: "crashed", exitCode: 133 },
    { reason: "oom", exitCode: 5 },
    { reason: "abnormal-exit", exitCode: 1 },
    { reason: "killed", exitCode: null },
    { reason: "clean-exit", exitCode: 0 },
    {},
  ]) {
    assert.equal(
      looksLikeSandboxStartupFailure({ ...details, sandboxedRendererRan: false }),
      false,
      JSON.stringify(details),
    );
  }
});

test("sandboxedRendererRan defaults to false, so a silent caller gets the diagnosis", () => {
  // Fail toward telling the user something: the remedy's first step corrects a
  // wrong guess, whereas silence leaves them with the original dead end.
  assert.equal(
    looksLikeSandboxStartupFailure({ reason: "launch-failed" }),
    true,
  );
});

//
// ── description ──────────────────────────────────────────────────────────────
//

test("describeWindowsSandboxFailure: silent off Windows", () => {
  for (const platform of ["darwin", "linux"]) {
    assert.equal(describeWindowsSandboxFailure(blocked({ platform })), null, platform);
  }
});

test("describeWindowsSandboxFailure: silent once a renderer has loaded", () => {
  assert.equal(
    describeWindowsSandboxFailure(
      blocked({ reason: "crashed", sandboxedRendererRan: true }),
    ),
    null,
  );
});

test("describeWindowsSandboxFailure: silent when the sandbox is already off", () => {
  assert.equal(
    describeWindowsSandboxFailure(blocked({ sandboxAlreadyOff: true })),
    null,
  );
});

test("describeWindowsSandboxFailure: names the never-loaded evidence and the fix", () => {
  const advice = describeWindowsSandboxFailure(blocked({ attempts: 3 }));
  assert.ok(advice, "expected advice on a pre-load Windows failure");
  assert.match(advice.cause, /After 3 reload attempts/);
  // Read verbatim by a stressed user: no "attempt(s)" hack anywhere, and the
  // grammar must agree with the number.
  assert.doesNotMatch(advice.cause, /attempt\(s\)/, "no pluralization hack");
  assert.match(
    describeWindowsSandboxFailure(blocked({ attempts: 1 })).cause,
    /After 1 reload attempt(?!s)/,
    "a single attempt must read in the singular",
  );
  assert.match(advice.cause, /without any document ever finishing a load/);
  assert.match(advice.cause, /launch-failed/);
  assert.match(advice.cause, /-2147483645/);
  assert.match(advice.cause, /STATUS_BREAKPOINT/);
  assert.match(advice.cause, /One common cause/);
  assert.match(advice.cause, /damaged install/);

  const remedy = advice.remedy.join("\n");
  // Disconfirmation FIRST: raising an endpoint-exclusion request on a damaged
  // install wastes the reader's time and their security team's, and one launch
  // separates the two. Anchored on the confirming step's own words, NOT on
  // "--no-sandbox", which also appears in the do-not-keep warning and would
  // match whichever mention came first even with the steps reordered.
  assert.ok(
    remedy.indexOf("First, confirm the sandbox") >= 0,
    "non-vacuity: the confirming step must exist to be ordered",
  );
  assert.ok(
    remedy.indexOf("First, confirm the sandbox") < remedy.indexOf("process exclusion"),
    "the confirming launch must come before the exclusion request",
  );
  assert.match(remedy, /the sandbox is NOT the cause/);
  assert.match(remedy, /Event Viewer/);
  assert.match(remedy, /faulting module/);
  assert.match(remedy, /Edge/);
});

test("describeWindowsSandboxFailure: claims no repetition it cannot evidence", () => {
  // renderer-recovery keeps timestamps, not per-death {reason, exitCode}
  // signatures, so a mixed sequence (crashed, oom, killed, launch-failed) can
  // reach give-up. The text may report the COUNT and the FINAL death; it must
  // not assert the earlier ones matched.
  const advice = describeWindowsSandboxFailure(blocked({ attempts: 4 }));
  assert.doesNotMatch(advice.cause, /the same way/);
  assert.doesNotMatch(advice.cause, /every replacement/i);
  assert.match(advice.cause, /the last one/);
});

test("describeWindowsSandboxFailure: a mid-run refusal tells a DIFFERENT story", () => {
  // When something had already loaded, "nothing ever started" is false and
  // would send the reader looking for the wrong thing. The evidence here is a
  // child refused AFTER the sandbox had been working, which points at endpoint
  // software that began injecting mid-session.
  const advice = describeWindowsSandboxFailure(
    blocked({ reason: "integrity-failure", attempts: 3, sandboxedRendererRan: true }),
  );
  assert.ok(advice, "a conclusive launch failure must advise even after a load");
  assert.match(advice.cause, /could not be launched at all/);
  assert.match(advice.cause, /An earlier renderer on this device did run/);
  assert.match(advice.cause, /something changed since/);
  // It must NOT claim nothing ever loaded, which is the other branch's text.
  assert.doesNotMatch(advice.cause, /without any document ever finishing a load/);

  // And the pre-load branch must still tell its own story, so the two cannot
  // collapse into one.
  const preLoad = describeWindowsSandboxFailure(
    blocked({ attempts: 3, sandboxedRendererRan: false }),
  );
  assert.match(preLoad.cause, /without any document ever finishing a load/);
  assert.doesNotMatch(preLoad.cause, /An earlier renderer on this device did run/);
});

test("the remedy never dead-ends a reader whose sandbox is not the cause", () => {
  // The disconfirmation step sends some readers away from the exclusion path;
  // they need a real next step, not just "reinstall", since a reinstall does not
  // fix a failing GPU/rendering path.
  const remedy = describeWindowsSandboxFailure(blocked()).remedy.join("\n");
  assert.match(remedy, /KIROCREW_DISABLE_GPU=1/);
  assert.match(remedy, /Do not raise an endpoint-security exclusion for this/);
  // Step one tells the reader to pass a flag and set a variable. The audience is
  // someone with an INSTALLED app and no checkout, so the log line must carry
  // the mechanics itself; a bare repo path is unreachable for them (docs are not
  // in build.files, and extraResources ships only backend-dist).
  //
  // The command must name THIS build's binary. A hardcoded install path is wrong
  // for a nightly, an all-users install and a portable copy — and worse than
  // useless, since it can launch a DIFFERENT app than the one that died, so the
  // disconfirmation step answers the wrong question.
  const exe = "C:\\Users\\a b\\AppData\\Local\\Programs\\KiroCrew Nightly\\KiroCrew Nightly.exe";
  const withPath = describeWindowsSandboxFailure(blocked({ execPath: exe })).remedy.join("\n");
  assert.ok(
    withPath.includes(`"${exe}" --no-sandbox`),
    "the remedy must quote the running executable path verbatim",
  );
  for (const guess of ["%LOCALAPPDATA%", "%PROGRAMFILES%", "Program Files"]) {
    assert.ok(
      !withPath.includes(guess),
      `the remedy must not guess an install layout (found ${guess})`,
    );
  }
  // With no path supplied it must still be actionable, not name a wrong one.
  assert.match(remedy, /Open file location/, "the fallback must stay actionable");
  assert.ok(
    !remedy.includes(".exe"),
    "the fallback must not invent an executable name",
  );
  assert.match(
    remedy,
    /single-instance/,
    "the remedy must warn that the flag is ignored unless the app is fully exited",
  );
  // Any pointer to the guide must be a fetchable URL, never a repo-relative
  // path. This is the regression this assertion exists for.
  for (const ref of remedy.match(/\S*docs\/guides\/\S*/g) || []) {
    assert.ok(
      ref.startsWith("https://"),
      `guide reference must be a URL, got ${ref}`,
    );
  }
  assert.ok(
    fs.existsSync(GUIDE_PATH),
    "non-vacuity: the guide the remedy points at must exist in-tree",
  );
});

test("describeWindowsSandboxFailure: an absent attempt count degrades cleanly", () => {
  for (const attempts of [undefined, null, 0, NaN, "x"]) {
    const advice = describeWindowsSandboxFailure(blocked({ attempts }));
    assert.ok(advice, String(attempts));
    assert.doesNotMatch(advice.cause, /undefined|NaN|null/);
  }
});

test("describeWindowsSandboxFailure: mentions the breakpoint only when it applies", () => {
  const other = describeWindowsSandboxFailure(
    blocked({ reason: "integrity-failure", exitCode: 1 }),
  );
  assert.ok(other, "a pre-load launch failure with another code still advises");
  assert.doesNotMatch(other.cause, /STATUS_BREAKPOINT/);
});

//
// ── behaviour through the real recovery module ───────────────────────────────
//

test("a blocked-sandbox sequence through renderer-recovery emits the advice", () => {
  // Composition test, not a source grep: the real createRendererRecovery drives
  // real render-process-gone events until its budget is spent, and the give-up
  // handler is wired exactly as window-lifecycle wires it. This is what proves
  // the advice actually reaches a log line for the reported failure.
  const logs = [];
  let rendererRan = false;
  const recovery = createRendererRecovery({
    isQuitting: () => false,
    log: (m) => logs.push(m),
    reload: () => {},
    onGiveUp: ({ reason, exitCode, attempts }) => {
      const advice = describeWindowsSandboxFailure({
        platform: "win32",
        reason,
        exitCode,
        attempts,
        sandboxedRendererRan: rendererRan,
        sandboxAlreadyOff: false,
      });
      if (!advice) return;
      logs.push(`renderer recovery: ${advice.cause}`);
      for (const line of advice.remedy) logs.push(`renderer recovery: ${line}`);
    },
  });

  // The reported shape: a CHECK during sandbox setup, reported as `crashed`.
  const death = { reason: "crashed", exitCode: STATUS_BREAKPOINT_SIGNED };
  for (let i = 0; i < 6; i += 1) recovery.handleGone(death);

  const joined = logs.join("\n");
  assert.match(joined, /without any document ever finishing a load/, "the diagnosis must be logged");
  assert.match(joined, /process exclusion/, "the remedy must be logged");
});

test("the same sequence AFTER a successful load emits no advice", () => {
  // Mutation-resistant pair to the test above: identical events, one bit of
  // state different, and the whole diagnosis must disappear.
  const logs = [];
  const rendererRan = true;
  const recovery = createRendererRecovery({
    isQuitting: () => false,
    log: (m) => logs.push(m),
    reload: () => {},
    onGiveUp: ({ reason, exitCode, attempts }) => {
      const advice = describeWindowsSandboxFailure({
        platform: "win32",
        reason,
        exitCode,
        attempts,
        sandboxedRendererRan: rendererRan,
        sandboxAlreadyOff: false,
      });
      if (!advice) return;
      logs.push(`renderer recovery: ${advice.cause}`);
    },
  });
  const death = { reason: "crashed", exitCode: STATUS_BREAKPOINT_SIGNED };
  for (let i = 0; i < 6; i += 1) recovery.handleGone(death);

  const joined = logs.join("\n");
  assert.match(
    joined,
    /giving up to avoid a reload loop/,
    "non-vacuity: recovery must have run to exhaustion and logged",
  );
  assert.doesNotMatch(joined, /process exclusion/, "no sandbox advice after a load");
});

test("the boot splash counts as evidence, and that is deliberate", () => {
  // This app's WebContents loads loading.html BEFORE the dashboard
  // (gateway-supervisor.js), so the splash finishing is what usually sets the
  // latch. It is still valid evidence and must keep suppressing advice: the
  // question is whether a renderer can start AT ALL, Electron sandboxes
  // renderers by default (webPreferences.sandbox defaults true since Electron
  // 20, disabled only by nodeIntegration:true, which the dashboard window does
  // not set), so a rendered splash IS a sandboxed renderer that ran.
  //
  // A reviewer once read this as a bug and asked for the latch to require a
  // dashboard-origin load. That change would report an SPA-side V8 abort as a
  // sandbox failure and send the user to their endpoint-security team over our
  // own JavaScript. This test exists so the trade is chosen, not stumbled into.
  const afterSplash = describeWindowsSandboxFailure(
    blocked({ reason: "crashed", sandboxedRendererRan: true }),
  );
  assert.equal(afterSplash, null, "a rendered splash must suppress the advice");

  // The inverse is the reported incident: the splash never rendered either, so
  // the window was blank and nothing set the latch.
  const splashNeverRendered = describeWindowsSandboxFailure(
    blocked({ reason: "crashed", sandboxedRendererRan: false }),
  );
  assert.ok(splashNeverRendered, "a blank window with no rendered document advises");
});

//
// ── no durable isolation opt-out anywhere in the shipped tree ────────────────
//
test("the advice offers NO durable way to disable isolation", () => {
  const remedy = describeWindowsSandboxFailure(blocked()).remedy.join("\n");
  assert.doesNotMatch(remedy, /KIROCREW_[A-Z_]*SANDBOX/, "no env-var opt-out");
  assert.doesNotMatch(remedy, /--disable-features/, "no Chromium feature ladder");
  assert.match(remedy, /Do not keep --no-sandbox/, "the flag carries its warning");
  assert.match(
    remedy,
    /diagnostic, not a setting/,
    "the warning must say what --no-sandbox is FOR, not merely forbid it",
  );
});

test("no shipped electron module can durably disable the sandbox", () => {
  // Class-level guard. Recursive over the whole shipped tree, and semantic as
  // well as name-based: an earlier version scanned only top-level files for one
  // uppercase env-var spelling, which a nested module, a differently named
  // variable, or a direct appendSwitch("no-sandbox") all walked straight past.
  const skipDirs = new Set(["node_modules", "test", "dist", "out", ".git"]);
  const files = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!skipDirs.has(entry.name)) walk(full);
      } else if (/\.(js|cjs|mjs|json)$/.test(entry.name)) {
        files.push(full);
      }
    }
  };
  walk(ELECTRON_DIR);
  assert.ok(files.length > 20, `non-vacuity: only ${files.length} files scanned`);
  assert.ok(
    files.some((f) => f.endsWith("windows-sandbox-advice.js")),
    "non-vacuity: the scan must reach the module under test",
  );

  const offenders = [];
  for (const file of files) {
    const src = fs.readFileSync(file, "utf8");
    const rel = path.relative(ELECTRON_DIR, file);
    // Any env var whose name mentions the sandbox, however it is prefixed.
    if (/(?:process\.env|env)\s*(?:\.|\[["'])\s*[A-Z_]*SANDBOX[A-Z_]*/.test(src)) {
      offenders.push(`${rel}: sandbox env var`);
    }
    // Applying the switch ourselves, rather than merely naming it as a string
    // the user may pass. appendSwitch is the only way this app could do it.
    if (/appendSwitch\(\s*["'`](?:--)?no-sandbox/.test(src)) {
      offenders.push(`${rel}: appendSwitch("no-sandbox")`);
    }
    if (/appendSwitch\(\s*["'`](?:--)?disable-features/.test(src)) {
      offenders.push(`${rel}: appendSwitch("disable-features")`);
    }
  }
  assert.deepEqual(offenders, [], "a durable sandbox opt-out was reintroduced");
});

test("the advice module delegates switch parsing to Chromium", () => {
  // The module must not reparse a command line itself: Chromium accepts
  // --no-sandbox=1, -no-sandbox and /no-sandbox, and stops honouring switches
  // after a bare --, none of which a string comparison gets right.
  assert.equal(SANDBOX_OFF_SWITCH, "no-sandbox", "the switch name Electron is asked for");
  // Comments deliberately DISCUSS process.argv to explain why it is not used,
  // so the scan must see code only or it can never pass.
  const code = read("windows-sandbox-advice.js")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .filter((line) => !/^\s*\/\//.test(line))
    .join("\n");
  assert.ok(code.includes("SANDBOX_OFF_SWITCH"), "non-vacuity: code survived stripping");
  assert.doesNotMatch(code, /process\.argv/, "the module must not read argv");
  assert.doesNotMatch(code, /"--no-sandbox"/, "no exact-string argv comparison");

  const lifecycle = read("window-lifecycle.js");
  assert.match(
    lifecycle,
    /app\.commandLine\.hasSwitch\(SANDBOX_OFF_SWITCH\)/,
    "the caller must ask Electron's own parser",
  );
});

//
// ── wiring ───────────────────────────────────────────────────────────────────
//

test("window-lifecycle wires the load latch and the advice into give-up", () => {
  // The behaviour tests above compose recovery with the advice directly, which
  // cannot execute window-lifecycle's own callback (that needs a live
  // BrowserWindow). These assertions cover the gap: they fail if the feature is
  // wired out of the lifecycle, or if the latch stops being maintained.
  const lifecycle = read("window-lifecycle.js");
  assert.match(
    lifecycle,
    /require\("\.\/windows-sandbox-advice"\)/,
    "window-lifecycle.js must require the module",
  );
  assert.match(
    lifecycle,
    /describeWindowsSandboxFailure\(\{/,
    "window-lifecycle.js must CALL the advice on give-up",
  );
  // The latch must be SET from a real load event and READ by the advice call,
  // or the classifier is fed a constant.
  assert.match(
    lifecycle,
    /did-finish-load["']\s*,\s*\(\)\s*=>\s*\{\s*sandboxedRendererRan = true/,
    "the latch must be set by a real did-finish-load event",
  );
  assert.match(lifecycle, /sandboxedRendererRan,/, "the latch must be passed to the advice");
  // Without this the advice silently degrades to the generic "find your
  // shortcut" fallback, which reads as working but loses the exact command.
  assert.match(
    lifecycle,
    /execPath:\s*process\.execPath/,
    "the lifecycle must pass the running executable path",
  );
  // The latch must be declared OUTSIDE createWindow. Nested inside it, the flag
  // is recreated as false whenever a window is recreated, and the advice then
  // reports "no document ever finished a load" for a second window's death —
  // false at app scope, which is the scope the cause text claims. Positional,
  // because the invariant is which scope holds it.
  const latchDecl = lifecycle.indexOf("let sandboxedRendererRan = false;");
  const createWindowFn = lifecycle.indexOf("function createWindow()");
  assert.ok(latchDecl > 0, "the latch must be declared");
  assert.ok(createWindowFn > 0, "createWindow must exist for this to be meaningful");
  assert.ok(
    latchDecl < createWindowFn,
    "the latch must be declared outside createWindow so window recreation cannot reset it",
  );
  // onGiveUp must destructure everything the advice needs; a handler taking
  // `{ reason }` alone compiles and reports undefined forever.
  assert.match(
    lifecycle,
    /onGiveUp:\s*\(\{\s*reason,\s*exitCode,\s*attempts\s*\}\)/,
    "onGiveUp must receive exitCode and attempts, not just reason",
  );
});

test("the dialog reuses the advice strings verbatim rather than re-authoring them", () => {
  // The dialog and the log are two renderings of ONE diagnosis. If the dialog
  // paraphrased, the app could tell a user one story and their support engineer
  // another from the same failure, and only one of the two could be corrected by
  // a later fix. Assert containment rather than similarity.
  const advice = describeWindowsSandboxFailure(blocked());
  const box = buildSandboxFailureDialog(advice, { logPath: "C:\\logs\\gateway-launch.log" });
  assert.ok(box.content.includes(advice.cause), "the dialog must carry the cause verbatim");
  for (const line of advice.remedy) {
    assert.ok(
      box.content.includes(line),
      `the dialog must carry every remedy step verbatim, missing: ${line.slice(0, 40)}`,
    );
  }
});

test("the dialog carries the FIX, not only the disconfirmation step", () => {
  // A truncated dialog would tell the reader what is broken and leave the cure
  // in the log — which is the delivery gap this dialog exists to close. Pinned by
  // the two steps that matter most: the one that identifies the cause and the one
  // that fixes it.
  const advice = describeWindowsSandboxFailure(blocked());
  const box = buildSandboxFailureDialog(advice, { logPath: "C:\\logs\\x.log" });
  assert.match(box.content, /--no-sandbox/, "the disconfirmation step must be present");
  assert.match(box.content, /process exclusion/, "the actual fix must be present");
  assert.equal(
    box.content.match(/^\d+\. /gm).length,
    advice.remedy.length,
    "every remedy step must be numbered into the dialog, none dropped",
  );
});

test("the dialog names the log file, and degrades cleanly without it", () => {
  const advice = describeWindowsSandboxFailure(blocked());
  const named = buildSandboxFailureDialog(advice, {
    logPath: "C:\\Users\\dev\\AppData\\Roaming\\KiroCrew\\logs\\gateway-launch.log",
  });
  assert.match(named.content, /gateway-launch\.log/, "the reader must be able to quote the log");
  // A caller that cannot resolve the path must still get the diagnosis, with no
  // dangling label and no literal "undefined" in front of a stressed user.
  const bare = buildSandboxFailureDialog(advice, {});
  assert.ok(bare.content.includes(advice.cause), "the cause survives a missing path");
  assert.doesNotMatch(bare.content, /undefined|also written to/, "no empty pointer");
  assert.match(named.title, /Kiro Crew/, "the title must name the app the user launched");
});

test("a blocked-sandbox sequence shows ONE dialog, after the log, and none when quitting", () => {
  // Composition test over the real recovery module, wired as window-lifecycle
  // wires it — including the latch and the quitting guard, which are the two
  // reasons a modal here could go wrong: a stack of boxes on a second window's
  // death, or a box blocking the quit the user just asked for.
  const run = ({ quitting }) => {
    const events = [];
    let dialogShown = false;
    const recovery = createRendererRecovery({
      isQuitting: () => false,
      log: (m) => events.push({ kind: "log", text: m }),
      reload: () => {},
      onGiveUp: ({ reason, exitCode, attempts }) => {
        const advice = describeWindowsSandboxFailure({
          platform: "win32",
          reason,
          exitCode,
          attempts,
          sandboxedRendererRan: false,
          sandboxAlreadyOff: false,
        });
        if (!advice) return;
        events.push({ kind: "log", text: `renderer recovery: ${advice.cause}` });
        for (const line of advice.remedy) {
          events.push({ kind: "log", text: `renderer recovery: ${line}` });
        }
        if (dialogShown || quitting) return;
        dialogShown = true;
        const box = buildSandboxFailureDialog(advice, { logPath: "C:\\logs\\g.log" });
        events.push({ kind: "dialog", text: box.content });
      },
    });
    const death = { reason: "crashed", exitCode: STATUS_BREAKPOINT_SIGNED };
    // Enough deaths to exhaust the budget twice over, as a second window would.
    for (let i = 0; i < 14; i += 1) recovery.handleGone(death);
    return events;
  };

  const shown = run({ quitting: false });
  const boxes = shown.filter((e) => e.kind === "dialog");
  assert.equal(boxes.length, 1, "exactly one dialog however many times recovery is exhausted");
  assert.match(boxes[0].text, /process exclusion/, "the dialog must carry the remedy");
  // The log is the channel that needs nothing from Chromium, so it must be on
  // disk before a modal that can block or fail.
  const firstDialog = shown.findIndex((e) => e.kind === "dialog");
  const causeLog = shown.findIndex(
    (e) => e.kind === "log" && /without any document ever finishing a load/.test(e.text),
  );
  assert.ok(causeLog >= 0, "the cause must be logged");
  assert.ok(causeLog < firstDialog, "the log line must precede the dialog");

  const quitting = run({ quitting: true });
  assert.equal(
    quitting.filter((e) => e.kind === "dialog").length,
    0,
    "no modal during shutdown",
  );
  assert.ok(
    quitting.some((e) => e.kind === "log"),
    "the diagnosis is still logged while quitting",
  );
});

test("window-lifecycle delivers the dialog, latched and fail-safe", () => {
  // Source guards for the same reason as the wiring test above: this callback
  // needs a live BrowserWindow, so its own copy cannot be executed here.
  const lifecycle = read("window-lifecycle.js");
  assert.match(
    lifecycle,
    /buildSandboxFailureDialog\(advice,\s*\{\s*logPath:\s*logPath\(\)\s*\}\)/,
    "the lifecycle must build the dialog from the advice and the real log path",
  );
  assert.match(
    lifecycle,
    /dialog\.showErrorBox\(box\.title,\s*box\.content\)/,
    "the dialog must actually be shown",
  );
  // Unlatched or unguarded, this stacks modal boxes or blocks a quit.
  assert.match(
    lifecycle,
    /if \(sandboxDialogShown \|\| isQuitting\(\)\) return;/,
    "the dialog must be latched once per run and suppressed while quitting",
  );
  // The latch must live outside createWindow, or a recreated window shows the
  // box again — the same scope invariant the load latch has.
  const dialogLatch = lifecycle.indexOf("let sandboxDialogShown = false;");
  const createWindowFn = lifecycle.indexOf("function createWindow()");
  assert.ok(dialogLatch > 0, "the dialog latch must be declared");
  assert.ok(
    dialogLatch < createWindowFn,
    "the dialog latch must be declared outside createWindow",
  );
  // showErrorBox on a host that cannot display one must not break the give-up
  // path, which still has cleanup after it.
  const giveUp = lifecycle.slice(lifecycle.indexOf("onGiveUp:"));
  const guarded = giveUp.slice(0, giveUp.indexOf("dialog.showErrorBox"));
  assert.match(guarded, /try \{/, "the dialog call must sit inside a try");
  // main.js must inject the real path, or the pointer silently degrades to "".
  // Scoped to the createWindowLifecycle call: `logPath: gatewayLogPath` appears
  // for another consumer too, so a whole-file match would pass with this one
  // deleted.
  const mainSrc = read("main.js");
  const callStart = mainSrc.indexOf("createWindowLifecycle({");
  assert.ok(callStart > 0, "main.js must construct the window lifecycle");
  const callArgs = mainSrc.slice(callStart, mainSrc.indexOf("\n});", callStart));
  assert.match(
    callArgs,
    /logPath:\s*gatewayLogPath,/,
    "createWindowLifecycle must receive gatewayLogPath",
  );
});

test("the packaged app ships the advice module", () => {
  // build.files is an explicit allowlist: an unregistered module is absent from
  // the installed app, so the feature would work in the repo and not in it.
  const files = JSON.parse(read("package.json")).build.files;
  assert.ok(
    files.includes("windows-sandbox-advice.js"),
    "windows-sandbox-advice.js must be registered in build.files",
  );
});

//
// ── documentation contract ───────────────────────────────────────────────────
//

test("the Windows guide's sandbox entry matches what the code does", () => {
  // Scoped to the troubleshooting bullet, not the whole 700-line guide: a
  // repo-wide substring search passes while this specific entry rots.
  const guide = fs.readFileSync(GUIDE_PATH, "utf8");
  const start = guide.indexOf("- **Desktop shortcut shows a gray/blank window");
  assert.ok(start > 0, "the troubleshooting entry must exist");
  const after = guide.indexOf("\n- **", start + 10);
  const end = after > 0 ? after : guide.indexOf("\n## ", start);
  assert.ok(end > start, "the entry must be delimited");
  const entry = guide.slice(start, end);
  assert.ok(entry.length > 800, `non-vacuity: entry only ${entry.length} chars`);

  assert.match(entry, /STATUS_BREAKPOINT/);
  assert.match(entry, /process exclusion/);
  assert.match(entry, /Event Viewer/);
  // The window cannot show the diagnosis (no renderer starts), so the log is
  // the only delivery channel and the entry must NAME it. "Check the logs" is
  // not actionable for the reader who could not decode 0x80000003.
  assert.match(entry, /gateway-launch\.log/, "the entry must name the log file");
  // The app now also puts the diagnosis in a native error dialog, which is the
  // only surface this failure leaves. An entry that says the log is the only
  // channel sends a reader to a file when the answer was already on screen, and
  // it also understates what a support engineer should ask them to screenshot.
  assert.match(entry, /error dialog/, "the entry must say the diagnosis is shown on screen");
  assert.doesNotMatch(
    entry,
    /the window itself cannot show you anything/,
    "stale claim: the diagnosis IS shown now",
  );
  // The reader is told to launch with a flag. On a single-instance app a second
  // launch hands argv to the running instance and exits, so the flag is
  // silently ignored and the reader concludes the sandbox is fine when it is
  // not. The entry must warn about that, or step one produces a false negative.
  assert.match(
    entry,
    /single-instance/,
    "the entry must warn that a flag is ignored unless the app is fully exited",
  );
  assert.match(
    entry,
    /Task Manager/,
    "the entry must say how to confirm the app really exited",
  );
  // Both log tokens: Chromium prints `exit_code=`, this app prints `exitCode=`,
  // and an admin greps whichever they are looking at.
  assert.match(entry, /exit_code=-2147483645/, "Chromium's own log token");
  assert.match(entry, /exitCode=-2147483645/, "this app's recovery log token");
  assert.match(entry, /2147483651/, "the unsigned form");
  // The condition the code actually requires. Documenting the exit code alone
  // would promise advice for a long-session crash that gets none.
  assert.match(
    entry,
    /without any document ever having finished loading/,
    "the entry must state the never-loaded condition, not just the exit code",
  );
  // Electron's did-finish-load means navigation completed and onload fired; it
  // does not promise pixels were presented, so the entry must not say rendered.
  assert.doesNotMatch(
    entry,
    /ever having finished rendering/,
    "did-finish-load is load completion, not proof of painting",
  );
  // Ordering contract, same as the emitted advice: confirm before escalating.
  assert.ok(
    entry.indexOf("First, confirm the sandbox") <
      entry.indexOf("**The fix is a process exclusion"),
    "the entry must tell the reader to confirm before requesting an exclusion",
  );
  assert.doesNotMatch(
    entry,
    /KIROCREW_[A-Z_]*SANDBOX/,
    "the entry documents a sandbox env var that does not exist",
  );
  assert.doesNotMatch(
    entry,
    /`renderer-code-integrity`/,
    "the entry must not prescribe the flag that is a no-op since Chromium 118",
  );
});
