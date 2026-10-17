const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  EXTERNAL_URLS,
  classifyNavigation,
  createWindowOpenHandler,
  openExternalSafely,
} = require("../external-scheme");

const ORIGIN = "http://127.0.0.1:6777";
const PANE_ACCESSIBILITY =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility";
const PANE_SCREEN =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";

/* The allowlist is now exact-match on whole URLs, so it is only correct while it
 * stays byte-identical to the pane constants the backend publishes and the panel
 * sends. A drift in either direction silently re-creates the dead button this
 * change fixed, so pin it against the real sources rather than a restated copy. */
describe("pane constants agree across backend, panel and this allowlist", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const repoRoot = path.resolve(__dirname, "..", "..", "..");
  const PANE_RE = /x-apple\.systempreferences:com\.apple\.[^'"`\s]+/g;

  function panesIn(relPath) {
    const text = fs.readFileSync(path.join(repoRoot, relPath), "utf8");
    return [...new Set(text.match(PANE_RE) || [])].sort();
  }

  it("matches src/kiro_crew/computer_use/permissions.py", () => {
    assert.deepEqual(panesIn("src/kiro_crew/computer_use/permissions.py"), [...EXTERNAL_URLS].sort());
  });

  it("matches website/src/pages/settings/ComputerUsePanel.tsx", () => {
    assert.deepEqual(
      panesIn("website/src/pages/settings/ComputerUsePanel.tsx"),
      [...EXTERNAL_URLS].sort(),
    );
  });
});

describe("classifyNavigation", () => {
  it("keeps same-origin web URLs in-app", () => {
    assert.equal(classifyNavigation(`${ORIGIN}/settings`, ORIGIN), "allow");
    assert.equal(classifyNavigation(`${ORIGIN}/`, `${ORIGIN}/some/path`), "allow");
  });

  it("sends cross-origin web URLs to the OS browser", () => {
    assert.equal(classifyNavigation("https://example.com/docs", ORIGIN), "external");
    // A different loopback PORT is a different origin.
    assert.equal(classifyNavigation("http://127.0.0.1:6778/", ORIGIN), "external");
  });

  it("sends the System Settings deep links to the OS", () => {
    assert.equal(classifyNavigation(PANE_ACCESSIBILITY, ORIGIN), "external");
    assert.equal(classifyNavigation(PANE_SCREEN, ORIGIN), "external");
  });

  it("blocks file: — handing a local path to the OS is a disclosure vector", () => {
    assert.equal(classifyNavigation("file:///etc/passwd", ORIGIN), "block");
    assert.equal(classifyNavigation("file:///Users/me/.ssh/id_rsa", ORIGIN), "block");
  });

  it("blocks other non-web schemes rather than defaulting them open", () => {
    for (const url of [
      "javascript:alert(1)",
      "data:text/html,<script>alert(1)</script>",
      "vscode://file/etc/hosts",
      "smb://attacker/share",
      "ftp://example.com/x",
      "chrome://settings",
      "about:blank",
    ]) {
      assert.equal(classifyNavigation(url, ORIGIN), "block", url);
    }
  });

  // Regression guard: `blob:` INHERITS the creating page's origin, and the
  // handler this replaced checked origin BEFORE protocol. WidgetFrame's "Open in
  // new tab" builds a wrapper document with URL.createObjectURL and opens it, so
  // classifying same-origin blobs as anything but `allow` turns that button into
  // a dead click in the packaged app (it needs a real window object).
  it("keeps a same-origin blob: in-app — WidgetFrame's popout depends on it", () => {
    assert.equal(classifyNavigation(`blob:${ORIGIN}/0388b437-dead-beef`, ORIGIN), "allow");
  });

  it("blocks a cross-origin blob: — not ours, and never an OS hand-off", () => {
    assert.equal(classifyNavigation("blob:https://evil.example/abc", ORIGIN), "block");
  });

  it("never hands an origin-inheriting scheme to the OS", () => {
    // A same-origin verdict can only be "allow", so blob: must never be able to
    // reach shell.openExternal by any path.
    for (const origin of [ORIGIN, "http://localhost:3000", "not-an-origin", ""]) {
      assert.notEqual(classifyNavigation(`blob:${ORIGIN}/x`, origin), "external");
    }
  });

  it("fails closed on an unparseable URL", () => {
    assert.equal(classifyNavigation("not a url", ORIGIN), "block");
    assert.equal(classifyNavigation("", ORIGIN), "block");
    assert.equal(classifyNavigation(undefined, ORIGIN), "block");
    assert.equal(classifyNavigation(null, ORIGIN), "block");
  });

  it("never promotes a cross-origin URL to same-origin when appOrigin is unusable", () => {
    // A broken appOrigin must not make an arbitrary URL open IN-APP.
    assert.equal(classifyNavigation("https://evil.example/", "not-an-origin"), "external");
    assert.equal(classifyNavigation("https://evil.example/", ""), "external");
  });

  it("allowlists exactly the two panes the product needs", () => {
    assert.deepEqual(EXTERNAL_URLS, [PANE_ACCESSIBILITY, PANE_SCREEN]);
  });

  /* The allowlist is whole-URL, not scheme-granular. This is reachable, not
   * theoretical: LLM-authored widget/artifact content renders in iframes with
   * `allow-popups-to-escape-sandbox`, and no CSP directive constrains a
   * window.open target's scheme — so a scheme-only rule would let model-generated
   * JS pop ANY System Settings pane next to text telling the user to enable it. */
  it("blocks every other pane in the permitted scheme", () => {
    for (const url of [
      "x-apple.systempreferences:com.apple.preferences.sharing?Services_RemoteLogin",
      "x-apple.systempreferences:com.apple.preferences.configurationprofiles",
      "x-apple.systempreferences:com.apple.preference.security?FDE",
      "x-apple.systempreferences:/Users/victim/Downloads/evil.prefPane",
      "x-apple.systempreferences:anything-at-all",
      "x-apple.systempreferences:",
    ]) {
      assert.equal(classifyNavigation(url, ORIGIN), "block", url);
    }
  });

  // The verdict is computed from `new URL(...)` but the RAW string is what gets
  // forwarded and re-parsed by NSURL/CFURL. Exact raw matching is what stops the
  // validated and forwarded values from diverging.
  it("rejects near-miss variants of an allowlisted pane", () => {
    for (const url of [
      `${PANE_ACCESSIBILITY}&extra=1`,
      `${PANE_ACCESSIBILITY}#frag`,
      ` ${PANE_ACCESSIBILITY}`,
      `\t${PANE_ACCESSIBILITY}`,
      "X-Apple.SystemPreferences:com.apple.preference.security?Privacy_Accessibility",
      "x-apple.systempreferences:com.apple.preference.security?privacy_accessibility",
    ]) {
      assert.equal(classifyNavigation(url, ORIGIN), "block", JSON.stringify(url));
    }
  });

  /* `new URL()` SUCCEEDS for any non-special scheme and reports the literal
   * origin "null", which compares equal to an opaque target origin — so the
   * catch never fires and a foreign document would read as same-origin. */
  it("treats an opaque appOrigin as never-same-origin", () => {
    for (const appOrigin of ["file:///x", "data:text/plain,x", "blob:null/y", "mailto:a@b"]) {
      assert.notEqual(classifyNavigation("blob:null/attacker", appOrigin), "allow");
    }
  });
});

/* Parity with the inline handler this module replaced.
 *
 * That handler governed EVERY window.open() in the dashboard — popouts, widget
 * blob popouts, external links — so the only intended behaviour change is that
 * an allowlisted non-web scheme now reaches the OS. Anything else that differs
 * is a regression in a feature this change was not about, which is exactly how
 * the blob: popout broke once already. */
describe("parity with the previous inline handler", () => {
  // Verbatim transcription of the pre-change handler (main.js, before the fix).
  function legacyVerdict(url, appOrigin) {
    try {
      const u = new URL(url);
      if (u.origin === new URL(appOrigin).origin) return "allow";
      if (u.protocol === "http:" || u.protocol === "https:") return "external";
    } catch {
      /* fall through */
    }
    return "deny";
  }

  // Every URL shape the dashboard's window.open call sites actually produce.
  const SHAPES = [
    `${ORIGIN}/popout/chat/x?sid=slot1`, // chatPopout
    `${ORIGIN}/popout/artifact/slug`, // artifactPopout
    `${ORIGIN}/worlds-popout`, // usePopoutSync
    `${ORIGIN}/apps/detail/thing`, // AppsPage cmd-click
    `${ORIGIN}/api/file-raw?path=/tmp/a.pdf`, // FileRenderers
    `blob:${ORIGIN}/0388b437`, // WidgetFrame popout
    "https://kiro.dev/", // footer link
    "https://github.com/owner/repo", // RegistryManager
    "https://d123.cloudfront.net/index.html", // deployed artifact
    "http://localhost:3000/", // another loopback port
    "http://mypod.localhost:6777/app", // tunnel URL
    "about:blank", // DevFleetPage shim
    "blob:https://evil.example/x",
    "data:text/html,<b>x</b>",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "not a url",
  ];

  it("matches the legacy verdict for every pre-existing URL shape", () => {
    for (const url of SHAPES) {
      const legacy = legacyVerdict(url, ORIGIN);
      const now = classifyNavigation(url, ORIGIN);
      // "deny" and "block" are the same outcome under different names: the
      // handler denies the popup and shells out to nothing.
      const normalized = now === "block" ? "deny" : now;
      assert.equal(normalized, legacy, `${url} — legacy ${legacy}, now ${now}`);
    }
  });

  it("changes exactly one verdict: the System Settings deep link", () => {
    assert.equal(legacyVerdict(PANE_ACCESSIBILITY, ORIGIN), "deny");
    assert.equal(classifyNavigation(PANE_ACCESSIBILITY, ORIGIN), "external");
  });
});

describe("createWindowOpenHandler", () => {
  function harness(overrides = {}) {
    const opened = [];
    const logged = [];
    const handler = createWindowOpenHandler({
      openExternal: (url) => {
        opened.push(url);
      },
      getAppOrigin: () => ORIGIN,
      log: (m) => logged.push(m),
      ...overrides,
    });
    return { handler, opened, logged };
  }

  it("requires its Electron glue", () => {
    assert.throws(() => createWindowOpenHandler(), /openExternal is required/);
    assert.throws(() => createWindowOpenHandler({}), /openExternal is required/);
    assert.throws(
      () => createWindowOpenHandler({ openExternal: () => {} }),
      /getAppOrigin is required/,
    );
  });

  it("allows a same-origin popup without shelling out", () => {
    const { handler, opened } = harness();
    assert.deepEqual(handler({ url: `${ORIGIN}/artifacts/1` }), { action: "allow" });
    assert.deepEqual(opened, []);
  });

  it("hands a System Settings deep link to the OS and denies the popup", () => {
    // The regression under test: this used to fall through to a bare deny, so
    // the grant shortcut was a dead click in the packaged app.
    const { handler, opened } = harness();
    assert.deepEqual(handler({ url: PANE_ACCESSIBILITY }), { action: "deny" });
    assert.deepEqual(opened, [PANE_ACCESSIBILITY]);
  });

  it("hands a cross-origin web URL to the OS and denies the popup", () => {
    const { handler, opened } = harness();
    assert.deepEqual(handler({ url: "https://example.com" }), { action: "deny" });
    assert.deepEqual(opened, ["https://example.com"]);
  });

  it("denies a blocked scheme without shelling out, and says why", () => {
    const { handler, opened, logged } = harness();
    assert.deepEqual(handler({ url: "file:///etc/passwd" }), { action: "deny" });
    assert.deepEqual(opened, []);
    assert.match(logged.join("\n"), /unsupported target/);
  });

  it("denies (and never throws) when the URL is missing or malformed", () => {
    const { handler, opened } = harness();
    assert.deepEqual(handler({}), { action: "deny" });
    assert.deepEqual(handler({ url: "://" }), { action: "deny" });
    assert.deepEqual(handler(undefined), { action: "deny" });
    assert.deepEqual(opened, []);
  });

  it("denies rather than escaping when the log sink throws", () => {
    // A throwing sink (closed stdout / EPIPE) must not propagate out of the
    // handler into Electron's setWindowOpenHandler.
    const { handler } = harness({
      log: () => {
        throw new Error("EPIPE");
      },
    });
    assert.deepEqual(handler({ url: "file:///etc/passwd" }), { action: "deny" });
  });

  it("denies rather than escaping when reading details.url throws", () => {
    const { handler } = harness();
    const hostile = {
      get url() {
        throw new Error("hostile getter");
      },
    };
    assert.deepEqual(handler(hostile), { action: "deny" });
  });

  it("denies when resolving the app origin throws", () => {
    const { handler, opened } = harness({
      getAppOrigin: () => {
        throw new Error("no window");
      },
    });
    assert.deepEqual(handler({ url: PANE_ACCESSIBILITY }), { action: "deny" });
    assert.deepEqual(opened, []);
  });

  it("survives a throwing openExternal", () => {
    const { handler, logged } = harness({
      openExternal: () => {
        throw new Error("LaunchServices unavailable");
      },
    });
    assert.deepEqual(handler({ url: PANE_ACCESSIBILITY }), { action: "deny" });
    assert.match(logged.join("\n"), /openExternal threw: LaunchServices unavailable/);
  });

  it("survives a REJECTING openExternal without an unhandled rejection", async () => {
    // shell.openExternal returns a Promise that rejects when the OS has no
    // handler for the scheme; unhandled, that would crash the main process.
    const logged = [];
    const handler = createWindowOpenHandler({
      openExternal: () => Promise.reject(new Error("no handler")),
      getAppOrigin: () => ORIGIN,
      log: (m) => logged.push(m),
    });
    assert.deepEqual(handler({ url: PANE_ACCESSIBILITY }), { action: "deny" });
    await new Promise((r) => setImmediate(r));
    assert.match(logged.join("\n"), /openExternal rejected: no handler/);
  });
});

describe("openExternalSafely", () => {
  it("tolerates a void-returning openExternal (no .catch on the result)", () => {
    let seen = "";
    assert.doesNotThrow(() =>
      openExternalSafely((url) => {
        seen = url;
      }, PANE_SCREEN, () => {}),
    );
    assert.equal(seen, PANE_SCREEN);
  });

  it("tolerates a missing log sink", () => {
    assert.doesNotThrow(() =>
      openExternalSafely(() => {
        throw new Error("boom");
      }, PANE_SCREEN),
    );
  });

  it("tolerates a THROWING log sink on the sync path", () => {
    assert.doesNotThrow(() =>
      openExternalSafely(
        () => {
          throw new Error("boom");
        },
        PANE_SCREEN,
        () => {
          throw new Error("EPIPE");
        },
      ),
    );
  });

  it("tolerates a THROWING log sink on the async path", async () => {
    // Without the guard this surfaces as an unhandledRejection — the exact
    // failure this helper exists to prevent.
    assert.doesNotThrow(() =>
      openExternalSafely(() => Promise.reject(new Error("no handler")), PANE_SCREEN, () => {
        throw new Error("EPIPE");
      }),
    );
    await new Promise((r) => setImmediate(r));
  });
});
