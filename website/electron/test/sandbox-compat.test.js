const test = require("node:test");
const assert = require("node:assert/strict");

const {
  parseSandboxCompat,
  sandboxCompatSwitches,
  initSandboxCompat,
  sandboxIsDisabled,
  knownTokens,
} = require("../sandbox-compat.js");

const {
  describeWindowsSandboxFailure,
  looksLikeSandboxStartupFailure,
  STATUS_BREAKPOINT_SIGNED,
  STATUS_BREAKPOINT_UNSIGNED,
} = require("../windows-sandbox-advice.js");

/** Record every appendSwitch call, preserving ARITY (see the valueless test). */
function recorder() {
  const calls = [];
  const logs = [];
  const warns = [];
  return {
    calls,
    logs,
    warns,
    appendSwitch: (...args) => calls.push(args),
    log: (m) => logs.push(m),
    warn: (m) => warns.push(m),
  };
}

//
// ── sandbox-compat: parsing ──────────────────────────────────────────────────
//

test("parseSandboxCompat: nothing requested by default", () => {
  const res = parseSandboxCompat({ env: {}, argv: ["node", "main.js"] });
  assert.deepEqual(res.requested, []);
  assert.deepEqual(res.unknown, []);
});

test("parseSandboxCompat: reads a single env token", () => {
  const res = parseSandboxCompat({
    env: { KIROCREW_SANDBOX_COMPAT: "renderer-code-integrity" },
    argv: [],
  });
  assert.deepEqual(res.requested, ["renderer-code-integrity"]);
});

test("parseSandboxCompat: tolerates case, padding, commas and spaces", () => {
  // This value is typed into a Group Policy field by a human, so every one of
  // these spellings has to mean the same thing.
  for (const raw of [
    "gpu-sandbox,off",
    " GPU-Sandbox , OFF ",
    "gpu-sandbox off",
    "off,gpu-sandbox",
    "off,,gpu-sandbox,",
  ]) {
    const res = parseSandboxCompat({
      env: { KIROCREW_SANDBOX_COMPAT: raw },
      argv: [],
    });
    assert.deepEqual(
      res.requested,
      ["gpu-sandbox", "off"],
      `expected ${JSON.stringify(raw)} to parse to the canonical pair`,
    );
  }
});

test("parseSandboxCompat: canonical order, not input order", () => {
  // The emitted command line must not depend on how the operator ordered the
  // tokens, or two hosts with identical intent get different switches.
  const res = parseSandboxCompat({
    env: {
      KIROCREW_SANDBOX_COMPAT:
        "network-service-sandbox,renderer-code-integrity,renderer-app-container",
    },
    argv: [],
  });
  assert.deepEqual(res.requested, [
    "renderer-code-integrity",
    "renderer-app-container",
    "network-service-sandbox",
  ]);
});

test("parseSandboxCompat: dedupes a token repeated across env and argv", () => {
  const res = parseSandboxCompat({
    env: { KIROCREW_SANDBOX_COMPAT: "off" },
    argv: ["node", "main.js", "--sandbox-compat=off"],
  });
  assert.deepEqual(res.requested, ["off"]);
});

test("parseSandboxCompat: honours the argv form", () => {
  const res = parseSandboxCompat({
    env: {},
    argv: ["node", "main.js", "--sandbox-compat=gpu-sandbox"],
  });
  assert.deepEqual(res.requested, ["gpu-sandbox"]);
});

test("parseSandboxCompat: unknown tokens are separated, never widened", () => {
  // The security property of a closed allowlist: a misremembered token must not
  // be read as a broader opt-out, and must not smuggle a raw Chromium switch.
  const res = parseSandboxCompat({
    env: {
      KIROCREW_SANDBOX_COMPAT:
        "no-sandbox,--remote-debugging-port=9222,disable-web-security,true,1",
    },
    argv: [],
  });
  assert.deepEqual(res.requested, [], "no unknown token may enable anything");
  assert.deepEqual(res.unknown, [
    "no-sandbox",
    "--remote-debugging-port=9222",
    "disable-web-security",
    "true",
    "1",
  ]);
});

test("parseSandboxCompat: a valid token survives an invalid neighbour", () => {
  const res = parseSandboxCompat({
    env: { KIROCREW_SANDBOX_COMPAT: "nonsense,gpu-sandbox" },
    argv: [],
  });
  assert.deepEqual(res.requested, ["gpu-sandbox"]);
  assert.deepEqual(res.unknown, ["nonsense"]);
});

//
// ── sandbox-compat: switch generation ────────────────────────────────────────
//

test("sandboxCompatSwitches: exact Chromium spelling, no leading dashes", () => {
  // An unknown Chromium switch is ignored silently, so a typo here is a fix
  // that looks applied and does nothing. Pin the strings.
  assert.deepEqual(sandboxCompatSwitches(["renderer-code-integrity"]), [
    ["disable-features", "RendererCodeIntegrity"],
  ]);
  assert.deepEqual(sandboxCompatSwitches(["gpu-sandbox"]), [
    ["disable-gpu-sandbox", undefined],
  ]);
  assert.deepEqual(sandboxCompatSwitches(["off"]), [["no-sandbox", undefined]]);
});

test("sandboxCompatSwitches: features COMBINE into one --disable-features", () => {
  // Chromium keeps one value per switch name, so emitting the switch twice
  // makes the second overwrite the first and silently leaves the operator's
  // earlier layer enabled. This is the whole reason the value is assembled.
  const pairs = sandboxCompatSwitches([
    "renderer-code-integrity",
    "renderer-app-container",
    "network-service-sandbox",
  ]);
  const featureSwitches = pairs.filter(([name]) => name === "disable-features");
  assert.equal(featureSwitches.length, 1, "exactly one --disable-features");
  assert.deepEqual(featureSwitches[0], [
    "disable-features",
    "RendererCodeIntegrity,RendererAppContainer,NetworkServiceSandbox",
  ]);
});

test("sandboxCompatSwitches: features and plain switches coexist", () => {
  assert.deepEqual(
    sandboxCompatSwitches(["renderer-code-integrity", "gpu-sandbox", "off"]),
    [
      ["disable-features", "RendererCodeIntegrity"],
      ["disable-gpu-sandbox", undefined],
      ["no-sandbox", undefined],
    ],
  );
});

test("sandboxCompatSwitches: empty or junk input yields no switches", () => {
  assert.deepEqual(sandboxCompatSwitches([]), []);
  assert.deepEqual(sandboxCompatSwitches(undefined), []);
  assert.deepEqual(sandboxCompatSwitches(["not-a-token"]), []);
});

//
// ── sandbox-compat: application ──────────────────────────────────────────────
//

test("initSandboxCompat: appends nothing when nothing is requested", () => {
  const r = recorder();
  const res = initSandboxCompat({
    appendSwitch: r.appendSwitch,
    env: {},
    argv: [],
    log: r.log,
    warn: r.warn,
  });
  assert.deepEqual(r.calls, []);
  assert.equal(res.fullOptOut, false);
  assert.deepEqual(res.applied, []);
  assert.deepEqual(r.warns, [], "a default launch must not warn");
});

test("initSandboxCompat: a valueless switch reaches appendSwitch with ONE arg", () => {
  // Electron's binding takes an optional value; forwarding an explicit
  // `undefined` is what main.js guards against, so the contract is pinned here.
  const r = recorder();
  initSandboxCompat({
    appendSwitch: r.appendSwitch,
    env: { KIROCREW_SANDBOX_COMPAT: "gpu-sandbox" },
    argv: [],
    log: r.log,
    warn: r.warn,
  });
  assert.deepEqual(r.calls, [["disable-gpu-sandbox"]]);
  assert.equal(r.calls[0].length, 1, "no trailing undefined argument");
});

test("initSandboxCompat: a valued switch passes name AND value", () => {
  const r = recorder();
  initSandboxCompat({
    appendSwitch: r.appendSwitch,
    env: { KIROCREW_SANDBOX_COMPAT: "renderer-code-integrity" },
    argv: [],
    log: r.log,
    warn: r.warn,
  });
  assert.deepEqual(r.calls, [["disable-features", "RendererCodeIntegrity"]]);
});

test("initSandboxCompat: the full opt-out warns on the user channel", () => {
  const r = recorder();
  const res = initSandboxCompat({
    appendSwitch: r.appendSwitch,
    env: { KIROCREW_SANDBOX_COMPAT: "off" },
    argv: [],
    log: r.log,
    warn: r.warn,
  });
  assert.equal(res.fullOptOut, true);
  assert.deepEqual(r.calls, [["no-sandbox"]]);
  assert.equal(r.warns.length, 1, "exactly one user-facing warning");
  assert.match(r.warns[0], /^WARN sandbox DISABLED/);
  assert.match(r.warns[0], /KIROCREW_SANDBOX_COMPAT=off/);
});

test("initSandboxCompat: a NARROW token does not warn", () => {
  // The warning has to stay meaningful: it marks the state that gives up the
  // sandbox entirely, so the narrow levers must not raise it too.
  for (const token of knownTokens().filter((t) => t !== "off")) {
    const r = recorder();
    const res = initSandboxCompat({
      appendSwitch: r.appendSwitch,
      env: { KIROCREW_SANDBOX_COMPAT: token },
      argv: [],
      log: r.log,
      warn: r.warn,
    });
    assert.equal(res.fullOptOut, false, `${token} is not a full opt-out`);
    assert.deepEqual(r.warns, [], `${token} must not warn`);
  }
});

test("initSandboxCompat: warn falls back to log when no warn channel is given", () => {
  const r = recorder();
  initSandboxCompat({
    appendSwitch: r.appendSwitch,
    env: { KIROCREW_SANDBOX_COMPAT: "off" },
    argv: [],
    log: r.log,
  });
  assert.ok(
    r.logs.some((m) => m.startsWith("WARN sandbox DISABLED")),
    "the opt-out must be recorded even without a warn channel",
  );
});

test("initSandboxCompat: an unknown token is reported with the accepted list", () => {
  const r = recorder();
  const res = initSandboxCompat({
    appendSwitch: r.appendSwitch,
    env: { KIROCREW_SANDBOX_COMPAT: "no-sandbox" },
    argv: [],
    log: r.log,
    warn: r.warn,
  });
  assert.deepEqual(r.calls, [], "an unknown token appends nothing");
  assert.deepEqual(res.unknown, ["no-sandbox"]);
  const line = r.logs.find((m) => m.includes("unknown token"));
  assert.ok(line, "the ignored token is named");
  assert.ok(line.includes("no-sandbox"));
  for (const token of knownTokens()) {
    assert.ok(line.includes(token), `remedy lists ${token}`);
  }
});

test("initSandboxCompat: one throwing appendSwitch costs neither the others nor the boot", () => {
  const r = recorder();
  let res;
  assert.doesNotThrow(() => {
    res = initSandboxCompat({
      appendSwitch: (name, value) => {
        if (name === "disable-features") throw new Error("nope");
        r.calls.push(value === undefined ? [name] : [name, value]);
      },
      env: { KIROCREW_SANDBOX_COMPAT: "renderer-code-integrity,off" },
      argv: [],
      log: r.log,
      warn: r.warn,
    });
  });
  assert.deepEqual(r.calls, [["no-sandbox"]], "the surviving switch is applied");
  assert.deepEqual(res.applied, ["no-sandbox"]);
  assert.ok(r.logs.some((m) => m.includes("--disable-features failed")));
});

//
// ── windows-sandbox-advice ───────────────────────────────────────────────────
//

test("looksLikeSandboxStartupFailure: STATUS_BREAKPOINT, either signedness", () => {
  assert.equal(
    looksLikeSandboxStartupFailure({ exitCode: STATUS_BREAKPOINT_SIGNED }),
    true,
  );
  assert.equal(
    looksLikeSandboxStartupFailure({ exitCode: STATUS_BREAKPOINT_UNSIGNED }),
    true,
  );
  assert.equal(STATUS_BREAKPOINT_SIGNED, -2147483645, "0x80000003 as int32");
});

test("looksLikeSandboxStartupFailure: launch and integrity reasons qualify", () => {
  for (const reason of ["launch-failed", "integrity-failure"]) {
    assert.equal(looksLikeSandboxStartupFailure({ reason }), true, reason);
  }
});

test("looksLikeSandboxStartupFailure: an ordinary crash does NOT", () => {
  // A renderer that ran and then died is the case renderer-recovery was built
  // for; misreading it as a sandbox failure would send every OOM report down
  // the wrong path.
  for (const details of [
    { reason: "crashed", exitCode: 133 },
    { reason: "oom", exitCode: 5 },
    { reason: "abnormal-exit", exitCode: 1 },
    { reason: "killed", exitCode: null },
    {},
  ]) {
    assert.equal(
      looksLikeSandboxStartupFailure(details),
      false,
      JSON.stringify(details),
    );
  }
});

test("describeWindowsSandboxFailure: silent off Windows", () => {
  for (const platform of ["darwin", "linux"]) {
    assert.equal(
      describeWindowsSandboxFailure({
        platform,
        reason: "launch-failed",
        exitCode: STATUS_BREAKPOINT_SIGNED,
      }),
      null,
      platform,
    );
  }
});

test("describeWindowsSandboxFailure: names the cause and the ladder", () => {
  const advice = describeWindowsSandboxFailure({
    platform: "win32",
    reason: "launch-failed",
    exitCode: STATUS_BREAKPOINT_SIGNED,
  });
  assert.ok(advice, "expected advice on a Windows sandbox startup failure");
  assert.match(advice.cause, /cannot start a child process/);
  assert.match(advice.cause, /launch-failed/);
  assert.match(advice.cause, /-2147483645/);

  const remedy = advice.remedy.join("\n");
  // Narrowest lever first, full opt-out after it, and the real fix named last.
  assert.ok(
    remedy.indexOf("renderer-code-integrity") <
      remedy.indexOf("KIROCREW_SANDBOX_COMPAT=off"),
    "the narrow lever must be offered before the blunt one",
  );
  assert.match(remedy, /environment variable/);
  assert.match(remedy, /endpoint security/);
  for (const token of knownTokens()) {
    assert.ok(remedy.includes(token), `remedy names ${token}`);
  }
});

test("describeWindowsSandboxFailure: every offered token is a REAL token", () => {
  // The remedy is copied by hand into a policy field. A token that drifted out
  // of the allowlist would be silently ignored on the next launch, and the
  // reader would conclude the app is broken beyond repair.
  const advice = describeWindowsSandboxFailure({
    platform: "win32",
    reason: "launch-failed",
    exitCode: STATUS_BREAKPOINT_SIGNED,
  });
  const offered = advice.remedy
    .join("\n")
    .split("\n")
    .map((line) => line.match(/KIROCREW_SANDBOX_COMPAT=(\S+)/))
    .filter(Boolean)
    .map((m) => m[1]);
  assert.ok(offered.length >= 5, "the whole ladder is offered");
  for (const token of offered) {
    assert.ok(
      knownTokens().includes(token),
      `${token} is offered but is not an accepted token`,
    );
  }
});

test("describeWindowsSandboxFailure: stays silent once already opted out", () => {
  // With the sandbox already off, the sandbox is not what killed this renderer
  // and this advice would be a dead end.
  assert.equal(
    describeWindowsSandboxFailure({
      platform: "win32",
      reason: "launch-failed",
      exitCode: STATUS_BREAKPOINT_SIGNED,
      alreadyOptedOut: true,
    }),
    null,
  );
});

test("describeWindowsSandboxFailure: silent on an ordinary Windows crash", () => {
  assert.equal(
    describeWindowsSandboxFailure({
      platform: "win32",
      reason: "crashed",
      exitCode: 133,
    }),
    null,
  );
});

//
// ── sandboxIsDisabled: both routes to an off sandbox ─────────────────────────
//

test("sandboxIsDisabled: false on an ordinary launch", () => {
  assert.equal(sandboxIsDisabled({ env: {}, argv: ["node", "main.js"] }), false);
});

test("sandboxIsDisabled: true via this module's own off token", () => {
  assert.equal(
    sandboxIsDisabled({ env: { KIROCREW_SANDBOX_COMPAT: "off" }, argv: [] }),
    true,
  );
  assert.equal(
    sandboxIsDisabled({ env: {}, argv: ["node", "main.js", "--sandbox-compat=off"] }),
    true,
  );
});

test("sandboxIsDisabled: true via a RAW --no-sandbox passed to Chromium", () => {
  // Chromium honours this without any cooperation from this module, so the
  // advice path must treat it as an off sandbox. Missing it makes the advice
  // actively wrong: with the sandbox already off, the sandbox is not the cause
  // of a repeated renderer death, and the ladder is a dead end.
  assert.equal(
    sandboxIsDisabled({ env: {}, argv: ["node", "main.js", "--no-sandbox"] }),
    true,
  );
});

test("sandboxIsDisabled: a NARROW token is not an off sandbox", () => {
  for (const token of knownTokens().filter((t) => t !== "off")) {
    assert.equal(
      sandboxIsDisabled({ env: { KIROCREW_SANDBOX_COMPAT: token }, argv: [] }),
      false,
      `${token} leaves the sandbox on`,
    );
  }
});

//
// ── wiring ───────────────────────────────────────────────────────────────────
//

test("main.js actually APPLIES the policy, and window-lifecycle consults the advice", () => {
  // Every unit test above passes on a tree where the call sites were deleted:
  // the modules would be correct, registered for packaging, and dead. This is
  // the only check that fails when the feature is wired out. Source-text
  // assertion with a non-vacuity guard, matching shell-contract.test.js.
  const fs = require("node:fs");
  const path = require("node:path");
  const read = (f) => fs.readFileSync(path.join(__dirname, "..", f), "utf8");

  const main = read("main.js");
  assert.match(main, /require\("\.\/sandbox-compat"\)/, "main.js must require the module");
  assert.match(main, /initSandboxCompat\(\{/, "main.js must CALL initSandboxCompat");
  // Chromium reads these during initialization, so the call has to precede
  // app-ready; a call added after it is accepted and then ignored. Matched on
  // the real invocation, not the bare name — a prose comment near the top of
  // main.js also mentions app.whenReady().
  assert.ok(
    main.indexOf("initSandboxCompat({") < main.indexOf("app.whenReady().then"),
    "initSandboxCompat must run before app.whenReady()",
  );

  const lifecycle = read("window-lifecycle.js");
  assert.match(
    lifecycle,
    /describeWindowsSandboxFailure\(\{/,
    "window-lifecycle.js must consult the advice on give-up",
  );
  assert.match(
    lifecycle,
    /sandboxIsDisabled\(\{/,
    "the give-up path must suppress advice when the sandbox is already off",
  );
});

//
// ── documentation contract ───────────────────────────────────────────────────
//

test("the Windows guide documents exactly the accepted tokens", () => {
  // This table is what an administrator copies into a policy field, so a token
  // that drifted out of the allowlist would be silently ignored on the next
  // launch and read as "the app is simply broken". Both directions are checked:
  // nothing undocumented, nothing documented that does not exist.
  const fs = require("node:fs");
  const path = require("node:path");
  const guide = fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "docs", "guides", "windows-install.md"),
    "utf8",
  );

  assert.ok(
    guide.includes("KIROCREW_SANDBOX_COMPAT"),
    "the guide must document the escape hatch at all",
  );

  for (const token of knownTokens()) {
    assert.ok(
      guide.includes(`\`${token}\``),
      `${token} is accepted but not documented in the Windows guide`,
    );
  }

  // Every backticked token in the guide's compat table must be real. Scoped to
  // the table rows so unrelated backticked prose is not swept in.
  const documented = guide
    .split("\n")
    .filter((line) => /^\s*\|\s*`[a-z-]+`\s*\|/.test(line))
    .map((line) => line.match(/`([a-z-]+)`/)[1]);
  assert.ok(documented.length >= knownTokens().length, "the table lists the ladder");
  for (const token of documented) {
    assert.ok(
      knownTokens().includes(token),
      `the guide documents \`${token}\` but it is not an accepted token`,
    );
  }
});
