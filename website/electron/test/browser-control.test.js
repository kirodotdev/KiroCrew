const { test } = require("node:test");
const assert = require("node:assert");
const {
  OWNER,
  planTransition,
  canAgentControl,
  isLoopbackUrl,
  mayBootstrapView,
  createControlPlane,
} = require("../browser-control");

// ── planTransition (the single-owner state machine) ──

test("transition: same owner is a no-op", () => {
  for (const o of [OWNER.NONE, OWNER.LIGHT, OWNER.PLAYWRIGHT]) {
    assert.strictEqual(planTransition(o, o).noop, true);
  }
});

test("transition: NONE -> LIGHT attaches, claims no external session", () => {
  const p = planTransition(OWNER.NONE, OWNER.LIGHT);
  assert.deepStrictEqual(
    { d: p.detachLight, a: p.attachLight, o: p.acquireExternal, c: p.releaseExternal },
    { d: false, a: true, o: false, c: false },
  );
});

test("transition: LIGHT -> PLAYWRIGHT detaches BEFORE claiming the external session", () => {
  // This is the overlap Chromium would otherwise allow (§12): an external CDP
  // client can drive a page the in-process debugger still holds.
  const p = planTransition(OWNER.LIGHT, OWNER.PLAYWRIGHT);
  assert.strictEqual(p.detachLight, true, "must release LIGHT");
  assert.strictEqual(p.acquireExternal, true);
  assert.strictEqual(p.attachLight, false);
});

test("transition: PLAYWRIGHT -> LIGHT releases the external session and attaches", () => {
  const p = planTransition(OWNER.PLAYWRIGHT, OWNER.LIGHT);
  assert.strictEqual(p.releaseExternal, true);
  assert.strictEqual(p.attachLight, true);
  assert.strictEqual(p.acquireExternal, false);
});

test("transition: leaving PLAYWRIGHT always releases the external session", () => {
  assert.strictEqual(planTransition(OWNER.PLAYWRIGHT, OWNER.NONE).releaseExternal, true);
  assert.strictEqual(planTransition(OWNER.PLAYWRIGHT, OWNER.LIGHT).releaseExternal, true);
});

test("transition: the external session is never claimed outside PLAYWRIGHT", () => {
  for (const from of [OWNER.NONE, OWNER.LIGHT, OWNER.PLAYWRIGHT]) {
    for (const to of [OWNER.NONE, OWNER.LIGHT]) {
      assert.strictEqual(planTransition(from, to).acquireExternal, false, `${from}->${to}`);
    }
  }
});

test("transition: the plan exposes NO debug-port surface", () => {
  // Guards the Stage 7 correction: a port on this process would enumerate the
  // dashboard itself as a drivable CDP target. PLAYWRIGHT drives a separate
  // browser over Playwright's own pipe and needs no port.
  const p = planTransition(OWNER.NONE, OWNER.PLAYWRIGHT);
  assert.ok(!("openPort" in p), "no openPort field");
  assert.ok(!("closePort" in p), "no closePort field");
});

test("transition: unknown owners are rejected", () => {
  assert.strictEqual(planTransition(OWNER.NONE, "wat").invalid, true);
  assert.strictEqual(planTransition("wat", OWNER.LIGHT).invalid, true);
});

// ── canAgentControl (gating) ──

test("gate: agent control requires the general agent-act authorization", () => {
  assert.deepStrictEqual(canAgentControl({ agentActEnabled: false, viewOpen: true }), {
    allowed: false,
    reason: "agent-act-not-authorized",
  });
  assert.strictEqual(canAgentControl({ agentActEnabled: true, viewOpen: true }).allowed, true);
});

test("gate: no view means nothing to control", () => {
  assert.deepStrictEqual(canAgentControl({ agentActEnabled: true, viewOpen: false }), {
    allowed: false,
    reason: "no-browser-view",
  });
});

test("gate: missing state fails closed", () => {
  assert.strictEqual(canAgentControl(undefined).allowed, false);
  assert.strictEqual(canAgentControl({}).allowed, false);
});

// ── mayBootstrapView (the predicate that shipped broken) ──

test("bootstrap: an absent view is the ONE refusal a navigate may tolerate", () => {
  // The bootstrap is about to satisfy this precondition by opening a view.
  assert.strictEqual(
    mayBootstrapView(canAgentControl({ agentActEnabled: true, viewOpen: false })),
    true,
  );
});

test("bootstrap: no grant still refuses, even with no view open", () => {
  // The regression this pins: reading a property the gate never returns made the
  // verdict a constant. Both directions must be asserted — a constant-TRUE test
  // passes the case above while letting an unauthorized bootstrap through here.
  const verdict = canAgentControl({ agentActEnabled: false, viewOpen: false });
  assert.strictEqual(verdict.reason, "agent-act-not-authorized");
  assert.strictEqual(mayBootstrapView(verdict), false);
});

test("bootstrap: an allowed verdict passes, and a malformed one fails closed", () => {
  assert.strictEqual(
    mayBootstrapView(canAgentControl({ agentActEnabled: true, viewOpen: true })),
    true,
  );
  // A shape with neither `allowed` nor a recognised reason must not slip through.
  assert.strictEqual(mayBootstrapView(undefined), false);
  assert.strictEqual(mayBootstrapView({}), false);
  assert.strictEqual(mayBootstrapView({ agentActEnabled: true }), false);
});

// ── consent seeding removed: Browser Mode is the agent's authorization ──

test("gate: the agent path authorizes on Browser Mode alone — no per-session grant, view precondition still applies", () => {
  // The "built-in browser is the default" change: the agent command channel's
  // dispatch (main.js) now builds the gate with `agentActEnabled: true`
  // unconditionally, because Browser Mode (the Settings toggle) is the agent's
  // authorization to drive the built-in browser — security.py (~line 4236)
  // documents it as keystone-level, "Presence alone is the authorization", and
  // the browser_* tools only exist while it is on. Gating the built-in view
  // behind a SECOND per-session grant gated a strictly weaker capability, so it
  // was removed. This pins that contract at the gate primitive: with the grant
  // modeled as always-on, an op is authorized with NO per-session consent state,
  // yet the view precondition still gates it.
  const agentGate = (viewOpen) => canAgentControl({ agentActEnabled: true, viewOpen });

  // Authorized with a live view and no per-session grant anywhere.
  assert.strictEqual(agentGate(true).allowed, true);

  // The view precondition still applies — a closed view refuses with no-browser-view,
  assert.deepStrictEqual(agentGate(false), { allowed: false, reason: "no-browser-view" });
  // ...and that is the ONE refusal a navigate may satisfy by bootstrapping a view.
  assert.strictEqual(mayBootstrapView(agentGate(false)), true);
});

test("loopback: local dev targets are exempt from the grant, real sites are not", () => {
  // The grant protects an authenticated identity; a dev server has none, and
  // cookies are host-scoped so navigating to localhost sends nobody's session.
  for (const u of [
    "http://localhost:5173/",
    "http://127.0.0.1:8080/x",
    "https://kirocrew.localhost/",
    "http://[::1]:3000/",
  ]) {
    assert.strictEqual(isLoopbackUrl(u), true, u);
    assert.strictEqual(
      canAgentControl({ agentActEnabled: false, viewOpen: true, loopback: isLoopbackUrl(u) }).allowed,
      true,
      u,
    );
  }

  // Anything that is not provably loopback stays gated — including lookalikes.
  for (const u of [
    "https://www.amazon.com/",
    "http://localhost.evil.com/",
    "http://notlocalhost/",
    "file:///etc/passwd",
    "",
    "not a url",
  ]) {
    assert.strictEqual(isLoopbackUrl(u), false, u);
    assert.strictEqual(
      canAgentControl({ agentActEnabled: false, viewOpen: true, loopback: isLoopbackUrl(u) }).reason,
      "agent-act-not-authorized",
      u,
    );
  }
});

test("loopback: the exemption does not lift the view precondition", () => {
  // Exempt from the GRANT is not exempt from needing a page to act on.
  assert.deepStrictEqual(
    canAgentControl({ agentActEnabled: false, viewOpen: false, loopback: true }),
    { allowed: false, reason: "no-browser-view" },
  );
  // ...but a bootstrap may still proceed, since that is the refusal it satisfies.
  assert.strictEqual(
    mayBootstrapView(canAgentControl({ agentActEnabled: false, viewOpen: false, loopback: true })),
    true,
  );
});

// ── createControlPlane ──

function fakeWc() {
  const calls = [];
  let isAttached = false;
  return {
    calls,
    attachCount: 0,
    debugger: {
      isAttached: () => isAttached,
      attach(v) {
        if (isAttached) throw new Error("Debugger is already attached to the target");
        isAttached = true;
        calls.push(["attach", v]);
      },
      detach() {
        isAttached = false;
        calls.push(["detach"]);
      },
      async sendCommand(method, params) {
        calls.push(["send", method, params]);
        if (method === "Runtime.evaluate") return { result: { value: 42 } };
        return {};
      },
    },
  };
}

const ALLOW = { allowed: true, reason: null };

test("plane: starts unowned and refuses CDP until LIGHT holds control", async () => {
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  assert.strictEqual(plane.getOwner(), OWNER.NONE);
  await assert.rejects(() => plane.send("Runtime.enable"), /does not hold control/);
});

test("plane: acquiring LIGHT attaches and enables CDP", async () => {
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  const res = await plane.setOwner(OWNER.LIGHT, ALLOW);
  assert.strictEqual(res.changed, true);
  assert.strictEqual(plane.getOwner(), OWNER.LIGHT);
  assert.strictEqual(await plane.evaluate("1+1"), 42);
});

test("plane: an ungated request is refused and leaves the owner untouched", async () => {
  const wc = fakeWc();
  const audits = [];
  const plane = createControlPlane({
    getWebContents: () => wc,
    onAudit: (e, d) => audits.push([e, d]),
  });
  const res = await plane.setOwner(OWNER.LIGHT, {
    allowed: false,
    reason: "agent-act-not-authorized",
  });
  assert.strictEqual(res.changed, false);
  assert.strictEqual(res.refused, "agent-act-not-authorized");
  assert.strictEqual(plane.getOwner(), OWNER.NONE);
  assert.strictEqual(audits.at(-1)[0], "browser-control-refused");
});

test("plane: releasing control needs no gate", async () => {
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  await plane.release();
  assert.strictEqual(plane.getOwner(), OWNER.NONE);
  assert.ok(wc.calls.some((c) => c[0] === "detach"));
});

test("plane: LIGHT -> PLAYWRIGHT detaches before the external session is claimed", async () => {
  const wc = fakeWc();
  const order = [];
  const plane = createControlPlane({
    getWebContents: () => wc,
    acquireExternalSession: async () => {
      order.push("acquireExternal");
      return { kind: "playwright-mcp" };
    },
    releaseExternalSession: async () => order.push("releaseExternal"),
  });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  wc.debugger.detach = ((orig) => function patched() {
    order.push("detach");
    return orig.call(this);
  })(wc.debugger.detach);
  const res = await plane.setOwner(OWNER.PLAYWRIGHT, ALLOW);
  assert.deepStrictEqual(order, ["detach", "acquireExternal"], "release precedes acquire");
  assert.deepStrictEqual(res.session, { kind: "playwright-mcp" });
  // And CDP is refused now that LIGHT no longer owns it.
  await assert.rejects(() => plane.send("Runtime.enable"), /does not hold control/);
});

test("plane: re-acquiring LIGHT is idempotent, not a double-attach throw", async () => {
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  const again = await plane.setOwner(OWNER.LIGHT, ALLOW);
  assert.strictEqual(again.changed, false);
  assert.strictEqual(plane.getOwner(), OWNER.LIGHT);
});

test("plane: a failed acquire records no phantom owner", async () => {
  const plane = createControlPlane({ getWebContents: () => null });
  await assert.rejects(() => plane.setOwner(OWNER.LIGHT, ALLOW), /no browser view/);
  assert.strictEqual(plane.getOwner(), OWNER.NONE);
});

test("plane: a failed PLAYWRIGHT acquire releases whatever it claimed", async () => {
  let released = 0;
  const plane = createControlPlane({
    getWebContents: () => fakeWc(),
    acquireExternalSession: async () => {
      throw new Error("browser launch failed");
    },
    releaseExternalSession: async () => {
      released += 1;
    },
  });
  await assert.rejects(() => plane.setOwner(OWNER.PLAYWRIGHT, ALLOW), /browser launch failed/);
  assert.strictEqual(plane.getOwner(), OWNER.NONE);
  assert.strictEqual(released, 1, "nothing is left claimed after a failed acquire");
});

test("plane: click dispatches view-relative coordinates unchanged", async () => {
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  await plane.click(230, 409);
  const dispatched = wc.calls.filter((c) => c[1] === "Input.dispatchMouseEvent");
  assert.strictEqual(dispatched.length, 3, "moved + pressed + released");
  for (const [, , params] of dispatched) {
    assert.strictEqual(params.x, 230, "no panel offset added");
    assert.strictEqual(params.y, 409);
  }
});

test("plane: CDP navigation refuses non-web URLs (will-navigate cannot see it)", async () => {
  // Regression: Page.navigate does NOT emit will-navigate, so browser-view.js's
  // navigation guard never fires for it. Without normalizing in the control
  // plane, an agent-supplied file:// URL would load a local file and
  // evaluate/snapshot would read it straight back out.
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  for (const bad of ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", ""]) {
    await assert.rejects(() => plane.navigate(bad), /refused navigation/, `${bad} refused`);
  }
  // No Page.navigate reached CDP for any of them.
  assert.strictEqual(wc.calls.filter((c) => c[1] === "Page.navigate").length, 0);
});

test("loopback: a remote gateway URL must never be polled with the local secret", () => {
  // Regression (found by review): the agent command channel authenticates with THIS
  // machine's internal secret, and reporting any session key is what makes it poll.
  // A remote-connected window's backend URL is the REMOTE host, so polling it pushes
  // the local secret through the tunnel to be rejected 403 — forever, since the poller
  // retries, making the remote append one SEL denial per attempt without bound. This
  // models main.js's `listPanelIds` guard: report keys only for a local gateway.
  const ids = (backendUrl, keys) => (isLoopbackUrl(backendUrl) ? keys : []);

  for (const local of ["http://127.0.0.1:5476", "http://localhost:7778/", "http://[::1]:5476"]) {
    assert.deepStrictEqual(ids(local, ["chat-a"]), ["chat-a"], local);
  }
  // A remote host, and anything not provably local, reports nothing — fail closed.
  for (const remote of ["http://dev-box.example.com:7879", "https://gw.internal/", "", undefined]) {
    assert.deepStrictEqual(ids(remote, ["chat-a"]), [], String(remote));
  }
});

test("plane: CDP navigation normalizes and forwards a web URL", async () => {
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  await plane.navigate("example.com");
  const nav = wc.calls.find((c) => c[1] === "Page.navigate");
  assert.deepStrictEqual(nav[2], { url: "https://example.com/" });
});

test("plane: re-taking a held owner STILL re-checks the gate (revocation bypass)", async () => {
  // Regression: the no-op short-circuit used to run BEFORE the gate, so once
  // LIGHT was attached, a caller that used setOwner as its permission check got
  // `{changed:false}` with no `refused` even after authorization was revoked —
  // and kept driving a view that shares the user's logged-in session.
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  assert.strictEqual(plane.getOwner(), OWNER.LIGHT);

  const revoked = { allowed: false, reason: "agent-act-not-authorized" };
  const again = await plane.setOwner(OWNER.LIGHT, revoked);
  assert.strictEqual(again.refused, "agent-act-not-authorized", "must report refusal");
  assert.strictEqual(again.changed, false);
});

test("plane: releasing after revocation stops ops entirely", async () => {
  const wc = fakeWc();
  const plane = createControlPlane({ getWebContents: () => wc });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  // What main.js now does when agent-act is switched off.
  await plane.release();
  assert.strictEqual(plane.getOwner(), OWNER.NONE);
  assert.strictEqual(plane.isAttached(), false);
  await assert.rejects(() => plane.send("Runtime.enable"), /does not hold control/);
  await assert.rejects(() => plane.evaluate("1+1"), /does not hold control/);
});

test("plane: owner transitions are audited", async () => {
  const audits = [];
  const plane = createControlPlane({
    getWebContents: () => fakeWc(),
    onAudit: (e, d) => audits.push([e, d]),
  });
  await plane.setOwner(OWNER.LIGHT, ALLOW);
  assert.deepStrictEqual(audits.at(-1)[0], "browser-control-owner");
  assert.strictEqual(audits.at(-1)[1].from, OWNER.NONE);
  assert.strictEqual(audits.at(-1)[1].to, OWNER.LIGHT);
});

test("plane: a throwing audit sink never breaks a transition", async () => {
  const plane = createControlPlane({
    getWebContents: () => fakeWc(),
    onAudit: () => {
      throw new Error("sink exploded");
    },
  });
  const res = await plane.setOwner(OWNER.LIGHT, ALLOW);
  assert.strictEqual(res.changed, true);
});

// ── reuse across a view rebuild (the "close before a second op" bug) ──

// A WebContents fake that models Chromium's debugger faithfully: `sendCommand`
// THROWS unless the debugger is currently attached to THIS target, and each
// target owns its own attach state. The shared `fakeWc()` above never checks
// attach state on send, so it cannot surface a stale-attachment bug — this one
// can. `id` is only for readability in failures.
function realisticWc(id) {
  let isAttached = false;
  return {
    id,
    debugger: {
      isAttached: () => isAttached,
      attach() {
        if (isAttached) throw new Error("Debugger is already attached to the target");
        isAttached = true;
      },
      detach() {
        isAttached = false;
      },
      async sendCommand(method) {
        if (!isAttached) throw new Error("Debugger is not attached to the target");
        return method === "Runtime.evaluate" ? { result: { value: 7 } } : {};
      },
    },
  };
}

test("plane: reuse after the embedded view is rebuilt re-attaches instead of hitting a detached target", async () => {
  // Reproduces the "must close the browser before a second operation" symptom.
  // The embedded WebContentsView can be torn down and rebuilt under the control
  // plane (panel reopened, session re-keyed, view recreated). The recorded owner
  // stays LIGHT, but the debugger that was attached to the OLD view is gone with
  // it. A second agent op re-requests LIGHT — a planTransition(LIGHT,LIGHT)
  // no-op — so without re-validating the attachment the next CDP send lands on
  // the NEW view's detached debugger and throws. Fully closing the panel is what
  // "fixed" it for users, because that path resets the owner to NONE.
  let current = realisticWc("A");
  const plane = createControlPlane({ getWebContents: () => current });

  await plane.setOwner(OWNER.LIGHT, ALLOW);
  assert.strictEqual(await plane.evaluate("1+1"), 7, "first op drives view A");

  // View A is destroyed; a fresh view B is mounted (new WebContents whose
  // debugger is NOT attached). The control plane still records owner = LIGHT.
  current = realisticWc("B");

  // The next agent op re-requests LIGHT, exactly as main.js dispatch does.
  const again = await plane.setOwner(OWNER.LIGHT, ALLOW);
  assert.strictEqual(again.changed, false, "same logical owner");

  // Must transparently reuse view B — not require the user to close first.
  assert.strictEqual(
    await plane.evaluate("1+1"),
    7,
    "second op must reuse the rebuilt view, not hit a detached target",
  );
});
