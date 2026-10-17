"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  CONSOLE_REPEAT_LIMIT,
  MESSAGE_MAX_LENGTH,
  PANE_LOG_PREFIX,
  PANE_REPEAT_LIMIT,
  REPEAT_KEYS_MAX,
  UNTRUSTED_LOG_BUDGET,
  attachFrameLoadLogging,
  formatFrameFailLoad,
  formatFrameNavigate,
  formatFrameStartNavigation,
  isTrustedFrameMessage,
  normalizeConsoleMessage,
  normalizeTrustedOrigin,
  safeUrl,
  sanitizeLogText,
} = require("../frame-load-log");

/** A webContents stand-in that records handlers and can replay events. */
function fakeContents() {
  const handlers = new Map();
  return {
    handlers,
    on(event, handler) {
      handlers.set(event, handler);
      return this;
    },
    emit(event, ...args) {
      const handler = handlers.get(event);
      if (!handler) throw new Error(`no handler for ${event}`);
      handler(...args);
    },
  };
}

/** The URL the main window is loaded with, and so the one trusted origin. */
const DASHBOARD_ORIGIN = "http://localhost:5476";

/** The dashboard's own document: top of the frame tree AND on that origin. */
const TOP_FRAME = { parent: null, origin: DASHBOARD_ORIGIN };

/** A crew pane: a child frame on a different (forwarded) origin. */
function paneFrame(origin = "http://localhost:7778") {
  return { parent: TOP_FRAME, origin };
}

/**
 * A remote document that IS the top frame — what a pane gets by navigating the
 * top-level window (a `target="_top"` link the user clicks). Position without
 * identity, which is exactly the case `parent === null` alone cannot refuse.
 */
function hijackedTopFrame(origin = "http://localhost:7778") {
  return { parent: null, origin };
}

/** A `console-message` emission in the Electron >= 35 details shape. */
function consoleDetails(message, { level = "info", frame = TOP_FRAME, sourceId = "app.js", lineNumber = 1 } = {}) {
  return { level, message, lineNumber, sourceId, frame };
}

function attachedLog(trustedOrigin = DASHBOARD_ORIGIN) {
  const lines = [];
  const contents = fakeContents();
  const attached = attachFrameLoadLogging(contents, (line) => lines.push(line), trustedOrigin);
  return { lines, contents, attached };
}

describe("frame-load-log URL redaction", () => {
  it("never writes a session token to the log", () => {
    const url = "http://localhost:7778/?token=supersecretvalue";
    assert.equal(safeUrl(url), "http://localhost:7778/?token=<redacted>");
    for (const line of [
      formatFrameNavigate({ url, httpResponseCode: 200, isMainFrame: false }),
      formatFrameFailLoad({ errorCode: -102, errorDescription: "ERR_CONNECTION_REFUSED", url }),
    ]) {
      assert.doesNotMatch(line, /supersecretvalue/, "the token must not reach the log");
      assert.match(line, /token=<redacted>/, "but its presence must still be recorded");
    }
  });

  it("keeps the port and path, which are the diagnostic content", () => {
    const line = formatFrameNavigate({
      url: "http://localhost:7778/chat/abc?token=x",
      httpResponseCode: 200,
      isMainFrame: false,
    });
    assert.match(line, /http:\/\/localhost:7778\/chat\/abc/);
  });

  it("marks a non-token query without dropping the fact there was one", () => {
    assert.equal(safeUrl("http://localhost:5476/?foo=1"), "http://localhost:5476/?<query>");
  });

  it("passes through a URL with no query untouched", () => {
    assert.equal(safeUrl("http://localhost:5476/"), "http://localhost:5476/");
  });
});

describe("frame-load-log formatting", () => {
  it("distinguishes a crew pane from the dashboard itself", () => {
    assert.match(
      formatFrameNavigate({ url: "http://localhost:7778/", httpResponseCode: 200, isMainFrame: false }),
      /subframe/,
    );
    assert.match(
      formatFrameNavigate({ url: "http://localhost:5476/", httpResponseCode: 200, isMainFrame: true }),
      /main/,
    );
  });

  it("records the HTTP status, so a refused token is visible as 403", () => {
    assert.match(
      formatFrameNavigate({ url: "http://localhost:7778/", httpResponseCode: 403, isMainFrame: false }),
      /status=403/,
    );
  });

  it("records the net error code and description on a failed load", () => {
    const line = formatFrameFailLoad({
      errorCode: -102,
      errorDescription: "ERR_CONNECTION_REFUSED",
      url: "http://localhost:7778/",
      isMainFrame: false,
    });
    assert.match(line, /FAILED/);
    assert.match(line, /code=-102/);
    assert.match(line, /ERR_CONNECTION_REFUSED/);
  });

  it("names a missing description rather than logging an empty gap", () => {
    assert.match(formatFrameFailLoad({ errorCode: -2, url: "http://x/" }), /unknown/);
  });

  it("does not print a bare '?' status as a number", () => {
    assert.match(formatFrameNavigate({ url: "http://x/" }), /status=\?/);
  });

  it("announces a navigation that merely started, before any commit", () => {
    const line = formatFrameStartNavigation({
      url: "http://localhost:7778/?token=s",
      isMainFrame: false,
    });
    assert.match(line, /frame navigation STARTED \(subframe\)/);
    assert.match(line, /token=<redacted>/);
    assert.doesNotMatch(line, /\bstatus=/, "nothing has committed yet, so there is no status");
  });

  it("stays silent for a same-document navigation, which carries no load info", () => {
    assert.equal(
      formatFrameStartNavigation({ url: "http://localhost:5476/chat", isInPlace: true }),
      "",
      "the dashboard is a router-driven SPA and would otherwise flood the log",
    );
  });
});

describe("frame-load-log console normalization", () => {
  it("reads the Electron >= 35 details-object shape", () => {
    const entry = normalizeConsoleMessage([
      { level: "error", message: "boom", lineNumber: 42, sourceId: "app.js", frame: TOP_FRAME },
    ]);
    assert.deepEqual(entry, {
      severity: "error",
      message: "boom",
      sourceId: "app.js",
      line: 42,
      frame: TOP_FRAME,
    });
  });

  it("reads the legacy positional shape the rest of the app still uses", () => {
    const entry = normalizeConsoleMessage([{}, 3, "boom", 42, "app.js"]);
    assert.deepEqual(entry, {
      severity: "error",
      message: "boom",
      sourceId: "app.js",
      line: 42,
      frame: null,
    });
  });

  it("carries the emitting frame through, since the text alone proves nothing", () => {
    const pane = paneFrame();
    assert.equal(normalizeConsoleMessage([consoleDetails("hi", { frame: pane })]).frame, pane);
    // Electron 43 delivers the details object AND the deprecated positionals; the
    // frame must survive whichever branch reads the payload.
    assert.equal(normalizeConsoleMessage([{ frame: pane }, 3, "boom", 1, "a.js"]).frame, pane);
  });

  it("maps every numeric level onto the shared vocabulary", () => {
    const levels = [0, 1, 2, 3].map((n) => normalizeConsoleMessage([{}, n, "m"]).severity);
    assert.deepEqual(levels, ["debug", "info", "warning", "error"]);
  });

  it("returns null for an unrecognized emission instead of inventing a message", () => {
    assert.equal(normalizeConsoleMessage([]), null);
    assert.equal(normalizeConsoleMessage([{}, 3]), null);
    assert.equal(normalizeConsoleMessage(undefined), null);
  });
});

describe("frame-load-log attachment", () => {
  it("logs a committed subframe navigation", () => {
    const { lines, contents } = attachedLog();
    contents.emit("did-frame-navigate", {}, "http://localhost:7778/?token=s", 200, "OK", false);
    assert.equal(lines.length, 1);
    assert.match(lines[0], /frame navigated \(subframe\) status=200 http:\/\/localhost:7778\/\?token=<redacted>/);
  });

  it("logs a failed load with its net error", () => {
    const { lines, contents } = attachedLog();
    contents.emit("did-fail-load", {}, -102, "ERR_CONNECTION_REFUSED", "http://localhost:7778/", false);
    assert.equal(lines.length, 1);
    assert.match(lines[0], /frame load FAILED \(subframe\) code=-102 ERR_CONNECTION_REFUSED/);
  });

  it("forwards console errors", () => {
    const { lines, contents } = attachedLog();
    contents.emit("console-message", {}, 3, "Refused to frame 'http://localhost:7778/'", 1, "app.js");
    assert.equal(lines.length, 1);
    assert.match(lines[0], /renderer console \[error\]: Refused to frame/);
  });

  it("drops warnings, which are dominated by per-paint noise", () => {
    const { lines, contents } = attachedLog();
    contents.emit("console-message", {}, 2, "Rendering was performed in a subtree hidden by content-visibility.", 1, "a.js");
    contents.emit("console-message", {}, 1, "just info", 1, "a.js");
    assert.deepEqual(lines, [], "only errors belong in the launch log");
  });

  it("caps identical repeats so one loud error cannot bury the load lines", () => {
    const { lines, contents } = attachedLog();
    for (let i = 0; i < 50; i += 1) {
      contents.emit("console-message", {}, 3, "same error", 1, "a.js");
    }
    assert.equal(lines.length, CONSOLE_REPEAT_LIMIT);
    assert.match(lines[CONSOLE_REPEAT_LIMIT - 1], /further repeats suppressed/);
    assert.doesNotMatch(lines[0], /further repeats suppressed/);
  });

  it("counts repeats per message, so a second distinct error still lands", () => {
    const { lines, contents } = attachedLog();
    for (let i = 0; i < 10; i += 1) contents.emit("console-message", {}, 3, "first", 1, "a.js");
    contents.emit("console-message", {}, 3, "second", 1, "a.js");
    assert.match(lines[lines.length - 1], /second/);
  });

  it("logs a started navigation and skips the SPA's in-place ones", () => {
    const { lines, contents } = attachedLog();
    contents.emit("did-start-navigation", {}, "http://localhost:7778/?token=s", false, false);
    contents.emit("did-start-navigation", {}, "http://localhost:5476/chat", true, true);
    assert.equal(lines.length, 1, "only the real navigation belongs in the log");
    assert.match(lines[0], /frame navigation STARTED \(subframe\)/);
  });

  it("keeps the dashboard's own [pane] journal even though it is INFO level", () => {
    const { lines, contents } = attachedLog();
    contents.emit(
      "console-message",
      consoleDetails(`${PANE_LOG_PREFIX} load-timeout id=nobita frame=about:blank`),
    );
    assert.equal(lines.length, 1, "the renderer half of the diagnosis must survive the filter");
    assert.match(lines[0], /load-timeout id=nobita frame=about:blank/);
  });

  it("gives the journal a higher repeat cap, because a re-mint loop IS the finding", () => {
    const { lines, contents } = attachedLog();
    for (let i = 0; i < PANE_REPEAT_LIMIT + 30; i += 1) {
      contents.emit("console-message", consoleDetails(`${PANE_LOG_PREFIX} auth-expired id=nobita`));
    }
    assert.equal(lines.length, PANE_REPEAT_LIMIT);
    assert.ok(PANE_REPEAT_LIMIT > CONSOLE_REPEAT_LIMIT, "a loop needs more than three lines to be visible");
    assert.match(lines[PANE_REPEAT_LIMIT - 1], /further repeats suppressed/);
  });

  it("still drops a non-prefixed info line, so pane logs are not a blanket opening", () => {
    const { lines, contents } = attachedLog();
    contents.emit("console-message", consoleDetails("pane something not ours"));
    assert.deepEqual(lines, []);
  });
});

describe("frame-load-log console trust boundary", () => {
  it("requires BOTH the top position and the dashboard's own origin", () => {
    assert.equal(isTrustedFrameMessage(TOP_FRAME, DASHBOARD_ORIGIN), true);
    assert.equal(isTrustedFrameMessage(paneFrame(), DASHBOARD_ORIGIN), false, "a nested frame is not the top one");
    assert.equal(
      isTrustedFrameMessage(hijackedTopFrame(), DASHBOARD_ORIGIN),
      false,
      "being the top frame is a position, not an identity",
    );
    // Fails closed on anything it cannot verify, including a frame that has been
    // destroyed (Electron throws on property access once that happens).
    assert.equal(isTrustedFrameMessage(null, DASHBOARD_ORIGIN), false);
    assert.equal(isTrustedFrameMessage({}, DASHBOARD_ORIGIN), false, "an absent parent is not a null parent");
    assert.equal(
      isTrustedFrameMessage({ parent: null }, DASHBOARD_ORIGIN),
      false,
      "an absent origin cannot equal the dashboard's",
    );
    assert.equal(
      isTrustedFrameMessage(TOP_FRAME, ""),
      false,
      "with no configured origin there is nothing to compare against",
    );
    assert.equal(
      isTrustedFrameMessage(
        {
          get parent() {
            throw new Error("Render frame was disposed before WebFrameMain could be accessed");
          },
        },
        DASHBOARD_ORIGIN,
      ),
      false,
    );
    assert.equal(
      isTrustedFrameMessage(
        {
          parent: null,
          get origin() {
            throw new Error("Render frame was disposed before WebFrameMain could be accessed");
          },
        },
        DASHBOARD_ORIGIN,
      ),
      false,
      "a throwing origin is unverifiable, not a match",
    );
  });

  it("reduces the loaded URL to the origin Chromium reports, not a prefix of it", () => {
    assert.equal(normalizeTrustedOrigin("http://localhost:5476/?token=secret"), DASHBOARD_ORIGIN);
    assert.equal(normalizeTrustedOrigin(DASHBOARD_ORIGIN), DASHBOARD_ORIGIN);
    // An unparseable or absent value yields "", which disables the allowlist.
    assert.equal(normalizeTrustedOrigin("not a url"), "");
    assert.equal(normalizeTrustedOrigin(undefined), "");
    // A same-prefix impostor must not pass: the comparison is equality, not startsWith.
    assert.equal(
      isTrustedFrameMessage({ parent: null, origin: "http://localhost:54760" }, DASHBOARD_ORIGIN),
      false,
    );
  });

  it("refuses a [pane] line from a remote document that navigated the top window", () => {
    const { lines, contents } = attachedLog();
    contents.emit(
      "console-message",
      consoleDetails(`${PANE_LOG_PREFIX} pane-ready id=nobita status=200`, { frame: hijackedTopFrame() }),
    );
    assert.deepEqual(
      lines,
      [],
      "a target=_top navigation must not hand a pane the dashboard's own journal",
    );
  });

  it("disables the journal entirely when no trusted origin was configured", () => {
    const { lines, contents } = attachedLog("");
    contents.emit("console-message", consoleDetails(`${PANE_LOG_PREFIX} pane-ready id=nobita`));
    assert.deepEqual(lines, [], "the INFO-level journal is the cost of failing closed");
    // Errors still flow — they just lose the trusted attribution.
    contents.emit("console-message", consoleDetails("boom", { level: "error" }));
    assert.equal(lines.length, 1);
    assert.match(lines[0], /untrusted frame http:\/\/localhost:5476/);
  });

  it("refuses a crew pane's forged [pane] journal line", () => {
    const { lines, contents } = attachedLog();
    contents.emit(
      "console-message",
      consoleDetails(`${PANE_LOG_PREFIX} pane-ready id=nobita status=200`, { frame: paneFrame() }),
    );
    assert.deepEqual(
      lines,
      [],
      "a compromised pane must not be able to write the record that says its pane loaded",
    );
  });

  it("drops the pane journal when no frame can be verified at all", () => {
    const { lines, contents } = attachedLog();
    contents.emit("console-message", {}, 1, `${PANE_LOG_PREFIX} load-timeout id=nobita`, 1, "app.js");
    assert.deepEqual(lines, [], "unverifiable is treated as untrusted, not as the dashboard");
  });

  it("still journals a pane's console ERROR, but attributes it to that origin", () => {
    const { lines, contents } = attachedLog();
    contents.emit(
      "console-message",
      consoleDetails("Refused to connect to the gateway", { level: "error", frame: paneFrame() }),
    );
    assert.equal(lines.length, 1, "a pane's own error is the diagnosis and must survive");
    assert.match(lines[0], /untrusted frame http:\/\/localhost:7778/);
  });

  it("does not let a pane forge whole log entries with a newline", () => {
    const { lines, contents } = attachedLog();
    contents.emit(
      "console-message",
      consoleDetails("boom\nframe navigated (subframe) status=200 http://localhost:7778/", {
        level: "error",
        frame: paneFrame(),
      }),
    );
    assert.equal(lines.length, 1);
    assert.doesNotMatch(lines[0], /\n/, "gateway-launch.log is read by tailing it, one record per line");
    assert.match(lines[0], /boom\\nframe navigated/, "the text is kept, only its framing is escaped");
  });

  it("neutralizes control characters that would rewrite a terminal reader's view", () => {
    assert.equal(sanitizeLogText("a\r\nb"), "a\\r\\nb");
    assert.equal(sanitizeLogText("esc\u001b[2Jgone"), "esc?[2Jgone");
    assert.equal(sanitizeLogText(null), "");
  });

  it("truncates one enormous message rather than losing the lines around it", () => {
    const line = sanitizeLogText("x".repeat(MESSAGE_MAX_LENGTH + 40));
    assert.ok(line.startsWith("x".repeat(MESSAGE_MAX_LENGTH)));
    assert.match(line, /\[40 more chars truncated\]/);
  });

  it("bounds the repeat counter, which a pane keys with text of its choosing", () => {
    const { lines, contents } = attachedLog();
    for (let i = 0; i < REPEAT_KEYS_MAX + 50; i += 1) {
      contents.emit("console-message", consoleDetails(`distinct error ${i}`, { level: "error" }));
    }
    // Trusted text is the dashboard's own, so the map is capped rather than the log.
    assert.equal(lines.length, REPEAT_KEYS_MAX + 50, "every distinct error is still logged once");
    contents.emit("console-message", consoleDetails("distinct error 0", { level: "error" }));
    assert.equal(lines.length, REPEAT_KEYS_MAX + 51, "the oldest keys were forgotten, not remembered forever");
  });

  it("caps what an untrusted frame can write no matter how it varies the text", () => {
    const { lines, contents } = attachedLog();
    const pane = paneFrame();
    for (let i = 0; i < UNTRUSTED_LOG_BUDGET + 50; i += 1) {
      contents.emit("console-message", consoleDetails(`distinct error ${i}`, { level: "error", frame: pane }));
    }
    assert.equal(
      lines.length,
      UNTRUSTED_LOG_BUDGET,
      "clearing the repeat map on overflow restarts per-message counting, so the bound cannot be per-message",
    );
    assert.match(
      lines[UNTRUSTED_LOG_BUDGET - 1],
      new RegExp(`untrusted-frame log budget of ${UNTRUSTED_LOG_BUDGET} reached`),
      "the last line must say why the log goes quiet, or the silence reads as recovery",
    );
    assert.doesNotMatch(lines[0], /log budget/, "only the final line carries the notice");
  });

  it("charges the budget only for lines it actually writes", () => {
    const { lines, contents } = attachedLog();
    const pane = paneFrame();
    // Repeats past CONSOLE_REPEAT_LIMIT are dropped, so they must not consume budget.
    for (let i = 0; i < 400; i += 1) {
      contents.emit("console-message", consoleDetails("same error", { level: "error", frame: pane }));
    }
    assert.equal(lines.length, CONSOLE_REPEAT_LIMIT);
    for (let i = 0; i < UNTRUSTED_LOG_BUDGET; i += 1) {
      contents.emit("console-message", consoleDetails(`later ${i}`, { level: "error", frame: pane }));
    }
    assert.equal(lines.length, UNTRUSTED_LOG_BUDGET, "the budget counts written lines, not emissions");
  });

  it("does not let pane volume throttle the dashboard's own errors", () => {
    const { lines, contents } = attachedLog();
    const pane = paneFrame();
    for (let i = 0; i < UNTRUSTED_LOG_BUDGET + 50; i += 1) {
      contents.emit("console-message", consoleDetails(`pane error ${i}`, { level: "error", frame: pane }));
    }
    contents.emit("console-message", consoleDetails("the dashboard's own failure", { level: "error" }));
    assert.match(
      lines[lines.length - 1],
      /the dashboard's own failure/,
      "the budget is the untrusted frames', so a spent one must not silence the diagnosis",
    );
    assert.doesNotMatch(lines[lines.length - 1], /untrusted frame/);
  });

  it("bounds a subframe's navigation flood, not just its console output", () => {
    const { lines, contents } = attachedLog();
    // A pane loops its own frame's loads — each did-fail-load is another line, and
    // this path used to call log() unconditionally, outside the budget.
    for (let i = 0; i < UNTRUSTED_LOG_BUDGET + 50; i += 1) {
      contents.emit("did-fail-load", {}, -102, "ERR_CONNECTION_REFUSED", `http://localhost:7778/try${i}`, false);
    }
    assert.equal(lines.length, UNTRUSTED_LOG_BUDGET, "subframe navigations are charged against the same budget");
    assert.match(lines[UNTRUSTED_LOG_BUDGET - 1], /untrusted-frame log budget of 100 reached/);
  });

  it("charges console and navigation to ONE aggregate budget, not one each", () => {
    const { lines, contents } = attachedLog();
    const pane = paneFrame();
    for (let i = 0; i < 70; i += 1) {
      contents.emit("did-fail-load", {}, -102, "ERR_CONNECTION_REFUSED", `http://localhost:7778/n${i}`, false);
    }
    for (let i = 0; i < 70; i += 1) {
      contents.emit("console-message", consoleDetails(`pane error ${i}`, { level: "error", frame: pane }));
    }
    assert.equal(
      lines.length,
      UNTRUSTED_LOG_BUDGET,
      "a pane cannot get 2x the budget by spreading its lines across two paths",
    );
  });

  it("does not budget the dashboard's own top-frame navigations", () => {
    const { lines, contents } = attachedLog();
    // The main frame is the dashboard itself: a handful of real navigations over the
    // app's life, and the ones the log most needs. isMainFrame=true is trusted.
    for (let i = 0; i < UNTRUSTED_LOG_BUDGET + 50; i += 1) {
      contents.emit("did-frame-navigate", {}, `http://localhost:5476/r${i}`, 200, "OK", true);
    }
    assert.equal(lines.length, UNTRUSTED_LOG_BUDGET + 50, "top-frame navigations are the dashboard's own and unbudgeted");
    assert.doesNotMatch(lines[lines.length - 1], /log budget/);
  });

  it("keeps the two repeat maps apart, so pane text cannot evict trusted counters", () => {
    const { lines, contents } = attachedLog();
    const pane = paneFrame();
    contents.emit("console-message", consoleDetails("shared text", { level: "error" }));
    // Overflow the untrusted map: its reset must not restart the trusted count.
    for (let i = 0; i < REPEAT_KEYS_MAX + 5; i += 1) {
      contents.emit("console-message", consoleDetails(`pane ${i}`, { level: "error", frame: pane }));
    }
    const before = lines.length;
    for (let i = 0; i < 10; i += 1) {
      contents.emit("console-message", consoleDetails("shared text", { level: "error" }));
    }
    assert.equal(
      lines.length - before,
      CONSOLE_REPEAT_LIMIT - 1,
      "the trusted counter kept its first hit and still stopped at the cap",
    );
  });

  it("is inert rather than throwing when there is nothing to attach to", () => {
    assert.equal(attachFrameLoadLogging(null, () => {}), false);
    assert.equal(attachFrameLoadLogging({}, () => {}), false);
    assert.equal(attachFrameLoadLogging(fakeContents(), null), false);
  });
});
