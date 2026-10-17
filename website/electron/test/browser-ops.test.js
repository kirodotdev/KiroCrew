const { test } = require("node:test");
const assert = require("node:assert");
const {
  WIRE_OPS,
  isValidRef,
  clampInt,
  rectCenter,
  formatOutline,
  keyDefinition,
  describeException,
  remoteArgsToText,
  normalizeConsoleEvent,
  WALKER_SOURCE,
  resolveExpression,
  selectOptionExpression,
  textProbeExpression,
  createBrowserOps,
} = require("../browser-ops");

// ── isValidRef ──

test("ref: only e<N> with a positive int is valid", () => {
  for (const good of ["e1", "e2", "e42", "e1000"]) assert.strictEqual(isValidRef(good), true, good);
  for (const bad of ["e0", "e01", "e", "x1", "1", "", null, undefined, 5, "e1 ", " e1"]) {
    assert.strictEqual(isValidRef(bad), false, JSON.stringify(bad));
  }
});

// ── clampInt ──

test("clampInt: clamps, truncates, and falls back", () => {
  assert.strictEqual(clampInt(5, 0, 10, 3), 5);
  assert.strictEqual(clampInt(50, 0, 10, 3), 10);
  assert.strictEqual(clampInt(-5, 0, 10, 3), 0);
  assert.strictEqual(clampInt(4.9, 0, 10, 3), 4);
  assert.strictEqual(clampInt(undefined, 0, 10, 3), 3);
  assert.strictEqual(clampInt(NaN, 0, 10, 3), 3);
  assert.strictEqual(clampInt("nope", 0, 10, 3), 3);
});

// ── rectCenter ──

test("rectCenter: center in CSS px from x/y or left/top", () => {
  assert.deepStrictEqual(rectCenter({ x: 10, y: 20, width: 100, height: 40 }), { x: 60, y: 40 });
  assert.deepStrictEqual(rectCenter({ left: 0, top: 0, width: 200, height: 200 }), { x: 100, y: 100 });
});

test("rectCenter: null for zero-area or malformed rects", () => {
  assert.strictEqual(rectCenter({ x: 0, y: 0, width: 0, height: 0 }), null);
  assert.strictEqual(rectCenter({ x: NaN, y: 0, width: 10, height: 10 }), null);
  assert.strictEqual(rectCenter(null), null);
  assert.strictEqual(rectCenter("nope"), null);
});

// ── formatOutline ──

test("outline: indentation, ref, and role/name shape", () => {
  const text = formatOutline([
    { depth: 0, role: "heading", name: "Welcome" },
    { depth: 1, role: "link", name: "Docs", ref: "e1" },
    { depth: 1, role: "button", name: "Sign in", ref: "e2" },
  ]);
  assert.strictEqual(
    text,
    [
      '- heading "Welcome"',
      '  - link "Docs" [ref=e1]',
      '  - button "Sign in" [ref=e2]',
    ].join("\n"),
  );
});

test("outline: state suffixes for checked/disabled/expanded/collapsed", () => {
  assert.strictEqual(
    formatOutline([{ depth: 0, role: "checkbox", name: "Agree", ref: "e1", state: { checked: true, disabled: true } }]),
    '- checkbox "Agree" [ref=e1] [checked] [disabled]',
  );
  assert.strictEqual(
    formatOutline([{ depth: 0, role: "button", name: "Menu", ref: "e1", state: { expanded: true } }]),
    '- button "Menu" [ref=e1] [expanded]',
  );
  assert.strictEqual(
    formatOutline([{ depth: 0, role: "button", name: "Menu", ref: "e1", state: { expanded: false } }]),
    '- button "Menu" [ref=e1] [collapsed]',
  );
});

test("outline: empty / non-array input yields empty string", () => {
  assert.strictEqual(formatOutline([]), "");
  assert.strictEqual(formatOutline(null), "");
  assert.strictEqual(formatOutline(undefined), "");
});

// ── keyDefinition ──

test("key: named keys map to CDP fields", () => {
  assert.deepStrictEqual(keyDefinition("Enter"), { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  assert.deepStrictEqual(keyDefinition("Escape"), { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
  assert.strictEqual(keyDefinition("ArrowDown").windowsVirtualKeyCode, 40);
});

test("key: a single character maps to itself; unknown/multichar is null", () => {
  const a = keyDefinition("a");
  assert.strictEqual(a.key, "a");
  assert.strictEqual(a.code, "KeyA");
  assert.strictEqual(keyDefinition("abc"), null);
  assert.strictEqual(keyDefinition(""), null);
  assert.strictEqual(keyDefinition(null), null);
});

// ── describeException ──

test("describeException: prefers description, then value, then text", () => {
  assert.strictEqual(describeException({ exception: { description: "TypeError: x" } }), "TypeError: x");
  assert.strictEqual(describeException({ exception: { value: "boom" } }), "boom");
  assert.strictEqual(describeException({ text: "Uncaught" }), "Uncaught");
  assert.strictEqual(describeException(null), "unknown page exception");
});

// ── console event normalisation ──

test("console: normalises consoleAPICalled and Log.entryAdded", () => {
  assert.deepStrictEqual(
    normalizeConsoleEvent("Runtime.consoleAPICalled", { type: "error", timestamp: 5, args: [{ value: "boom" }, { value: 42 }] }),
    { source: "console", level: "error", text: "boom 42", ts: 5 },
  );
  assert.deepStrictEqual(
    normalizeConsoleEvent("Log.entryAdded", { entry: { level: "warning", text: "csp", timestamp: 9, url: "https://x" } }),
    { source: "log", level: "warning", text: "csp", ts: 9, url: "https://x" },
  );
  assert.strictEqual(normalizeConsoleEvent("Page.loadEventFired", {}), null);
});

test("remoteArgsToText: value / description / unserializable", () => {
  assert.strictEqual(
    remoteArgsToText([{ value: "a" }, { description: "Object" }, { unserializableValue: "Infinity" }]),
    "a Object Infinity",
  );
  assert.strictEqual(remoteArgsToText(null), "");
});

// ── expression builders embed the ref safely ──

test("expressions: the ref is JSON-embedded, not interpolated raw", () => {
  const r = resolveExpression('e1"]; alert(1); //');
  assert.ok(r.includes(JSON.stringify('e1"]; alert(1); //')), "ref is JSON-quoted");
  assert.ok(textProbeExpression("he\"llo").includes(JSON.stringify("he\"llo")));
  assert.ok(selectOptionExpression("e1", ["a", "b"]).includes(JSON.stringify(["a", "b"])));
});

// ── createBrowserOps: a stub CDP so ops are testable with no Electron ──

/**
 * Build a fake `sendCommand`. `onEvaluate(expr, params)` decides the result of
 * a Runtime.evaluate by inspecting the injected expression; other methods get
 * canned results. Every call is recorded.
 */
function makeCdp({ onEvaluate, history, screenshotData = "PNGDATA==" } = {}) {
  const calls = [];
  async function sendCommand(method, params) {
    calls.push({ method, params });
    if (method === "Runtime.evaluate") {
      return onEvaluate ? onEvaluate(params.expression, params) : { result: { value: undefined } };
    }
    if (method === "Page.captureScreenshot") return { data: screenshotData };
    if (method === "Page.getNavigationHistory") {
      return history || { currentIndex: 1, entries: [{ id: 11, url: "https://a/" }, { id: 22, url: "https://b/" }] };
    }
    return {};
  }
  return { calls, sendCommand };
}

const sent = (cdp, method) => cdp.calls.filter((c) => c.method === method);

test("run: unknown op throws the byte-compatible message", async () => {
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  await assert.rejects(() => ops.run("frobnicate", {}), /unsupported browser control op: frobnicate/);
});

// `navigate` waits for the new document to commit, so a stub must answer the
// commit probe. Returning an empty `stale` means "the stamp is gone" = new doc.
const committedProbe = (expression) => {
  if (/__kcNavToken =/.test(expression)) return { result: { value: 1 } };
  if (/readyState/.test(expression)) return { result: { value: { s: "complete", stale: "" } } };
  return { result: { value: undefined } };
};

test("navigate: normalises and forwards a web URL over CDP", async () => {
  const cdp = makeCdp({ onEvaluate: committedProbe });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("navigate", { url: "example.com" });
  assert.deepStrictEqual(res, { ok: true, url: "https://example.com/" });
  assert.deepStrictEqual(sent(cdp, "Page.navigate")[0].params, { url: "https://example.com/" });
});

test("navigate: waits for the new document instead of returning on dispatch", async () => {
  // The old document still carries the stamp on the first polls, so a correct
  // implementation must keep waiting rather than report success immediately --
  // otherwise the next op reads the PREVIOUS page and claims it succeeded.
  // The stub must echo back the REAL token navigate wrote, since that identity
  // is what distinguishes "old document" from "new document".
  let stamped = "";
  let polls = 0;
  const cdp = makeCdp({
    onEvaluate: (expression) => {
      const m = expression.match(/__kcNavToken = "([^"]+)"/);
      if (m) {
        stamped = m[1];
        return { result: { value: 1 } };
      }
      if (/readyState/.test(expression)) {
        polls += 1;
        return polls < 3
          ? { result: { value: { s: "complete", stale: stamped } } } // still the old doc
          : { result: { value: { s: "complete", stale: "" } } }; // committed
      }
      return { result: { value: undefined } };
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand, sleep: async () => {}, now: () => 0 });
  const res = await ops.run("navigate", { url: "https://example.com/" });
  assert.strictEqual(res.ok, true);
  assert.ok(polls >= 3, `expected repeated polling until commit, saw ${polls}`);
});

test("navigate: a document that never commits is a coded timeout, not a hang", async () => {
  let stamped = "";
  const cdp = makeCdp({
    onEvaluate: (expression) => {
      const m = expression.match(/__kcNavToken = "([^"]+)"/);
      if (m) {
        stamped = m[1];
        return { result: { value: 1 } };
      }
      // Always the stamped (old) document -> never commits.
      if (/readyState/.test(expression)) return { result: { value: { s: "complete", stale: stamped } } };
      return { result: { value: undefined } };
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand, sleep: async () => {}, now: () => 0 });
  const res = await ops.run("navigate", { url: "https://example.com/" });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.code, "nav_timeout");
});

test("a CDP command that never settles times out instead of wedging the call", async () => {
  // Reachable in production: Page.captureScreenshot does not settle against a
  // view with no live compositor surface, which is exactly the state the panel
  // puts the view in (setInactive) when the user switches side-panel tabs.
  const ops = createBrowserOps({
    sendCommand: () => new Promise(() => {}), // never settles
    commandTimeoutMs: 20,
  });
  await assert.rejects(() => ops.run("screenshot", {}), /timed out after 20ms/);
});

test("navigate: refuses a non-web URL with a coded error, no CDP navigate", async () => {
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("navigate", { url: "file:///etc/passwd" });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.code, "bad_url");
  assert.strictEqual(sent(cdp, "Page.navigate").length, 0);
});

test("snapshot: formats the walker's node list into outline text", async () => {
  const cdp = makeCdp({
    onEvaluate: () => ({
      result: {
        value: {
          url: "https://x/",
          title: "X",
          nodes: [{ depth: 0, role: "button", name: "Go", ref: "e1", state: {} }],
        },
      },
    }),
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("snapshot", {});
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.url, "https://x/");
  assert.strictEqual(res.snapshot, '- button "Go" [ref=e1]');
});

test("snapshot: a thrown walker surfaces a coded error", async () => {
  const cdp = makeCdp({ onEvaluate: () => ({ exceptionDetails: { text: "boom" } }) });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("snapshot", {});
  assert.deepStrictEqual(res, { ok: false, code: "snapshot_error", error: "boom" });
});

test("click: resolves the ref and dispatches VIEW-relative trusted input", async () => {
  const cdp = makeCdp({
    onEvaluate: (expr) => {
      if (expr.includes("scrollIntoView")) return { result: { value: { ok: true, x: 230, y: 409, tag: "button" } } };
      return { result: { value: undefined } };
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("click", { ref: "e5" });
  assert.deepStrictEqual(res, { ok: true, ref: "e5", x: 230, y: 409, button: "left", clickCount: 1 });
  const mouse = sent(cdp, "Input.dispatchMouseEvent");
  assert.deepStrictEqual(mouse.map((c) => c.params.type), ["mouseMoved", "mousePressed", "mouseReleased"]);
  for (const c of mouse) {
    assert.strictEqual(c.params.x, 230, "no panel offset added");
    assert.strictEqual(c.params.y, 409);
  }
});

test("click: honours button / doubleClick / modifiers instead of dropping them", async () => {
  // Dropping these would silently downgrade a right-click to a left-click, so a
  // destructive control's primary handler would fire instead of its context
  // menu -- the wrong action, reported as success.
  const cdp = makeCdp({
    onEvaluate: (expr) =>
      expr.includes("scrollIntoView")
        ? { result: { value: { ok: true, x: 10, y: 20, tag: "button" } } }
        : { result: { value: undefined } },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("click", {
    ref: "e1",
    button: "right",
    doubleClick: true,
    modifiers: ["Shift", "Meta"],
  });
  assert.strictEqual(res.button, "right");
  assert.strictEqual(res.clickCount, 2);
  const press = sent(cdp, "Input.dispatchMouseEvent").find((c) => c.params.type === "mousePressed");
  assert.strictEqual(press.params.button, "right");
  assert.strictEqual(press.params.clickCount, 2);
  assert.strictEqual(press.params.modifiers, 8 | 4, "Shift(8)|Meta(4) as a CDP bitmask");
});

test("click: an unknown button is coerced to left, never forwarded blindly", async () => {
  const cdp = makeCdp({
    onEvaluate: (expr) =>
      expr.includes("scrollIntoView")
        ? { result: { value: { ok: true, x: 1, y: 2, tag: "button" } } }
        : { result: { value: undefined } },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("click", { ref: "e1", button: "sideways" });
  assert.strictEqual(res.button, "left");
  const press = sent(cdp, "Input.dispatchMouseEvent").find((c) => c.params.type === "mousePressed");
  assert.strictEqual(press.params.button, "left");
});

test("click: a stale ref returns ref_stale and never dispatches input", async () => {
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: { ok: false } } }) });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("click", { ref: "e9" });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.code, "ref_stale");
  assert.strictEqual(sent(cdp, "Input.dispatchMouseEvent").length, 0);
});

test("click: a malformed ref is rejected as bad_ref before any CDP call", async () => {
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("click", { ref: "notaref" });
  assert.strictEqual(res.code, "bad_ref");
  assert.strictEqual(cdp.calls.length, 0);
});

test("hover: dispatches a single mouseMoved at the resolved center", async () => {
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: { ok: true, x: 12, y: 34 } } }) });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("hover", { ref: "e1" });
  assert.deepStrictEqual(res, { ok: true, ref: "e1", x: 12, y: 34 });
  const mouse = sent(cdp, "Input.dispatchMouseEvent");
  assert.strictEqual(mouse.length, 1);
  assert.strictEqual(mouse[0].params.type, "mouseMoved");
});

test("type: focuses then inserts text; submit appends an Enter key press", async () => {
  const cdp = makeCdp({
    onEvaluate: (expr) => {
      if (expr.includes("scrollIntoView")) return { result: { value: { ok: true, x: 1, y: 2 } } };
      return { result: { value: { ok: true, focused: true } } }; // focus landed
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("type", { ref: "e1", text: "hello", submit: true });
  assert.deepStrictEqual(res, { ok: true, ref: "e1", submitted: true });
  assert.deepStrictEqual(sent(cdp, "Input.insertText")[0].params, { text: "hello" });
  const keys = sent(cdp, "Input.dispatchKeyEvent");
  assert.deepStrictEqual(keys.map((c) => c.params.type), ["rawKeyDown", "keyUp"]);
  assert.strictEqual(keys[0].params.key, "Enter");
});

test("type: refuses when focus did NOT land on the target", async () => {
  // Input.insertText goes to whatever holds focus. A disabled/readonly target
  // leaves focus elsewhere, so typing would silently write into a DIFFERENT
  // field (possibly a credential box) and still report success.
  const cdp = makeCdp({
    onEvaluate: (expr) => {
      if (expr.includes("scrollIntoView")) return { result: { value: { ok: true, x: 1, y: 2 } } };
      return { result: { value: { ok: true, focused: false, disabled: true, activeTag: "body" } } };
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("type", { ref: "e1", text: "secret" });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.code, "not_focusable");
  assert.strictEqual(sent(cdp, "Input.insertText").length, 0, "must not type anywhere");
});

test("type: a stale ref returns ref_stale and never inserts text", async () => {
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: { ok: false } } }) });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("type", { ref: "e1", text: "x" });
  assert.strictEqual(res.code, "ref_stale");
  assert.strictEqual(sent(cdp, "Input.insertText").length, 0);
});

test("press_key: dispatches rawKeyDown + keyUp; unknown key is coded", async () => {
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("press_key", { key: "Enter" });
  assert.deepStrictEqual(res, { ok: true, key: "Enter" });
  assert.deepStrictEqual(sent(cdp, "Input.dispatchKeyEvent").map((c) => c.params.type), ["rawKeyDown", "keyUp"]);

  const bad = await ops.run("press_key", { key: "Nope" });
  assert.strictEqual(bad.code, "bad_key");
});

test("select_option: sets values and returns the selected list", async () => {
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: { ok: true, selected: ["b"] } } }) });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("select_option", { ref: "e1", values: ["b"] });
  assert.deepStrictEqual(res, { ok: true, ref: "e1", selected: ["b"] });
});

test("select_option: a non-select element is a coded error", async () => {
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: { ok: false, code: "not_select" } } }) });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("select_option", { ref: "e1", values: ["b"] });
  assert.strictEqual(res.code, "not_select");
});

test("screenshot: returns base64 + format and never writes a file", async () => {
  const cdp = makeCdp({ screenshotData: "ABC123==" });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const png = await ops.run("screenshot", {});
  // `mimeType` is part of the contract: the caller's save-to-file path keys on
  // it (shared with the Playwright direction), so a jpeg lacking it would be
  // written as image/png.
  assert.deepStrictEqual(png, { ok: true, format: "png", mimeType: "image/png", data: "ABC123==" });
  const jpeg = await ops.run("screenshot", { format: "jpeg", quality: 50 });
  assert.strictEqual(jpeg.format, "jpeg");
  assert.strictEqual(jpeg.mimeType, "image/jpeg");
  assert.strictEqual(sent(cdp, "Page.captureScreenshot").at(-1).params.quality, 50);
});

test("evaluate: returns the value; a page exception is a coded error", async () => {
  const okCdp = makeCdp({ onEvaluate: () => ({ result: { value: 7 } }) });
  const ok = createBrowserOps({ sendCommand: okCdp.sendCommand });
  assert.deepStrictEqual(await ok.run("evaluate", { expression: "3+4" }), { ok: true, value: 7 });

  const badCdp = makeCdp({ onEvaluate: () => ({ exceptionDetails: { exception: { description: "ReferenceError: y" } } }) });
  const bad = createBrowserOps({ sendCommand: badCdp.sendCommand });
  const res = await bad.run("evaluate", { expression: "y" });
  assert.deepStrictEqual(res, { ok: false, code: "evaluate_error", error: "ReferenceError: y" });
});

test("back: navigates to the previous history entry AND waits for it to commit", async () => {
  let stamped = "";
  let polls = 0;
  const cdp = makeCdp({
    onEvaluate: (expr) => {
      const m = expr.match(/__kcNavToken = "([^"]+)"/);
      if (m) {
        stamped = m[1];
        return { result: { value: 1 } };
      }
      if (/readyState/.test(expr)) {
        polls += 1;
        // Still the old document on the first poll, committed afterwards.
        return polls < 2
          ? { result: { value: { s: "complete", stale: stamped, href: "https://b/" } } }
          : { result: { value: { s: "complete", stale: "", href: "https://a/" } } };
      }
      return { result: { value: undefined } };
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand, sleep: async () => {}, now: () => 0 });
  const res = await ops.run("back", {});
  assert.deepStrictEqual(res, { ok: true, url: "https://a/" });
  assert.deepStrictEqual(sent(cdp, "Page.navigateToHistoryEntry")[0].params, { entryId: 11 });
  assert.ok(polls >= 2, `back must wait for the destination to commit, saw ${polls} polls`);
});

test("back: a destination that never commits is a coded timeout, not a hang", async () => {
  let stamped = "";
  const cdp = makeCdp({
    onEvaluate: (expr) => {
      const m = expr.match(/__kcNavToken = "([^"]+)"/);
      if (m) {
        stamped = m[1];
        return { result: { value: 1 } };
      }
      if (/readyState/.test(expr)) {
        return { result: { value: { s: "complete", stale: stamped, href: "https://b/" } } };
      }
      return { result: { value: undefined } };
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand, sleep: async () => {}, now: () => 0 });
  const res = await ops.run("back", {});
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.code, "nav_timeout");
});

test("back: at the first entry there is no previous page", async () => {
  const cdp = makeCdp({ history: { currentIndex: 0, entries: [{ id: 1, url: "https://only/" }] } });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("back", {});
  assert.strictEqual(res.code, "no_history");
  assert.strictEqual(sent(cdp, "Page.navigateToHistoryEntry").length, 0);
});

test("wait_for: resolves when the text appears (deterministic clock)", async () => {
  let present = false;
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: present } }) });
  const ops = createBrowserOps({
    sendCommand: cdp.sendCommand,
    now: () => 0,
    sleep: async () => { present = true; }, // second poll sees the text
  });
  const res = await ops.run("wait_for", { text: "Loaded", timeout: 1000 });
  assert.deepStrictEqual(res, { ok: true, matched: "present", text: "Loaded" });
});

test("wait_for: times out with a coded error when the condition never holds", async () => {
  let t = 0;
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: false } }) });
  const ops = createBrowserOps({
    sendCommand: cdp.sendCommand,
    now: () => { t += 500; return t; }, // clock races past the timeout
    sleep: async () => {},
  });
  const res = await ops.run("wait_for", { text: "Never", timeout: 300 });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.code, "wait_timeout");
});

test("wait_for: a fixed delay is SECONDS per the tool contract, not milliseconds", async () => {
  // Playwright's `time` is seconds. Consuming it as ms made wait_for({time: 3})
  // sleep 3ms and report success -- a silent 1000x under-wait.
  let slept = 0;
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand, sleep: async (ms) => { slept = ms; } });
  const res = await ops.run("wait_for", { time: 3 });
  assert.deepStrictEqual(res, { ok: true, waited: 3000 });
  assert.strictEqual(slept, 3000);
});

test("wait_for: a huge delay is still clamped to the ceiling", async () => {
  let slept = 0;
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand, sleep: async (ms) => { slept = ms; } });
  const res = await ops.run("wait_for", { time: 9999 });
  assert.strictEqual(res.waited, 30000);
  assert.strictEqual(slept, 30000);
});

test("evaluate: invokes the function source rather than returning the function", async () => {
  // Playwright sends a FUNCTION SOURCE ("() => document.title"). Evaluating that
  // text directly yields the function object (undefined under returnByValue) and
  // reports success -- so it must be called.
  const seen = [];
  const cdp = makeCdp({
    onEvaluate: (expr) => {
      seen.push(expr);
      return { result: { value: "TITLE" } };
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("evaluate", { expression: "() => document.title" });
  assert.deepStrictEqual(res, { ok: true, value: "TITLE" });
  assert.ok(
    seen.some((e) => /\(\(\(\) => document\.title\)\)\(\)/.test(e) || /\)\(\)$/.test(e)),
    `expected the source to be invoked, saw: ${seen.join(" | ")}`
  );
});

test("evaluate: the element-scoped form passes the resolved ref as the argument", async () => {
  // The ref was previously accepted and silently dropped, so an element-scoped
  // evaluate ran against the page instead of the element and reported success.
  const seen = [];
  const cdp = makeCdp({
    onEvaluate: (expr) => {
      seen.push(expr);
      if (expr.includes("scrollIntoView")) {
        return { result: { value: { ok: true, x: 5, y: 6, tag: "input" } } };
      }
      return { result: { value: "VALUE" } };
    },
  });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("evaluate", { expression: "el => el.value", ref: "e7" });
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.ref, "e7");
  assert.ok(
    seen.some((e) => e.includes("__kcRefs.get(\"e7\")") && !e.includes("scrollIntoView")),
    `expected the element to be passed in, saw: ${seen.join(" | ")}`
  );
});

test("evaluate: an empty function source is a coded error, not a silent success", async () => {
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("evaluate", { expression: "   " });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.code, "bad_expression");
});

test("wait_for: with no condition returns a coded bad_wait", async () => {
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("wait_for", {});
  assert.strictEqual(res.code, "bad_wait");
});

test("console: buffers subscribed CDP events and returns them", async () => {
  let emit;
  const cdp = makeCdp();
  const ops = createBrowserOps({
    sendCommand: cdp.sendCommand,
    subscribe: (handler) => { emit = handler; },
  });
  emit("Runtime.consoleAPICalled", { type: "log", timestamp: 1, args: [{ value: "hi" }] });
  emit("Log.entryAdded", { entry: { level: "error", text: "csp", timestamp: 2 } });
  emit("Page.somethingElse", {}); // ignored
  const res = await ops.run("console", {});
  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.wired, true);
  assert.deepStrictEqual(res.messages.map((m) => m.text), ["hi", "csp"]);
  // console op enables the domains it needs.
  assert.ok(sent(cdp, "Runtime.enable").length >= 1);
  assert.ok(sent(cdp, "Log.enable").length >= 1);
});

test("console: limit returns only the most recent entries", async () => {
  let emit;
  const cdp = makeCdp();
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand, subscribe: (h) => { emit = h; } });
  for (let i = 0; i < 5; i++) emit("Runtime.consoleAPICalled", { type: "log", args: [{ value: `m${i}` }] });
  const res = await ops.run("console", { limit: 2 });
  assert.deepStrictEqual(res.messages.map((m) => m.text), ["m3", "m4"]);
});

// ── transport-vs-answered failure split (fallback invariant) ──

test("transport failure propagates (throws) — only that may fall back", async () => {
  const cdp = {
    calls: [],
    sendCommand: async (method) => {
      if (method === "Page.navigate") throw new Error("debugger detached");
      return {};
    },
  };
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  // A CDP transport error is NOT swallowed into ok:false — it throws, so the
  // command channel reports transport failure (the only fallback-eligible case).
  await assert.rejects(() => ops.run("navigate", { url: "https://x/" }), /debugger detached/);
});

test("an answered error is a returned ok:false with a code — never a throw", async () => {
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: { ok: false } } }) });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand });
  const res = await ops.run("click", { ref: "e1" });
  assert.strictEqual(res.ok, false);
  assert.ok(typeof res.code === "string" && res.code.length > 0, "answered errors carry a code");
});

// ── contract vocabulary ──

test("WIRE_OPS is exactly the agreed vocabulary", () => {
  assert.deepStrictEqual(
    [...WIRE_OPS].sort(),
    ["back", "click", "console", "evaluate", "hover", "navigate", "press_key", "screenshot", "select_option", "snapshot", "type", "wait_for"].sort(),
  );
});

test("every WIRE_OP has a handler (run does not throw 'unsupported')", async () => {
  // A stub that answers every evaluate/screenshot/history minimally so each op
  // can run to completion without a real page.
  const cdp = makeCdp({ onEvaluate: () => ({ result: { value: { ok: true, x: 0, y: 0, nodes: [], selected: [] } } }) });
  const ops = createBrowserOps({ sendCommand: cdp.sendCommand, sleep: async () => {}, now: () => 0 });
  for (const op of WIRE_OPS) {
    const args = { url: "https://x/", ref: "e1", key: "Enter", expression: "1", values: [], time: 1 };
    await assert.doesNotReject(async () => {
      await ops.run(op, args);
    }, `op ${op} should be handled`);
  }
});

test("WALKER_SOURCE is a self-contained IIFE string", () => {
  assert.ok(typeof WALKER_SOURCE === "string" && WALKER_SOURCE.startsWith("(()"));
  assert.ok(WALKER_SOURCE.includes("__kcRefs"));
});
