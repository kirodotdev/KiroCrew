"use strict";
//
// token-prompt.test.js — the SHIPPED recovery wording on the "Token Required"
// page (token-prompt.html).
//
// #16258: a packaged AppImage adopting a local gateway it did not spawn is
// correctly refused a silent token mint, and the page told the user to run the
// on-PATH `kirocrew token` — a command an AppImage installs nowhere on PATH.
// The shell now resolves the bundled launcher's absolute path
// (appImageBundledCliPath, tested in gateway-auth-hint.test.js) and passes it
// to this page as `?cli=`; the page names it in the LOCAL recovery command.
//
// These tests run the page's OWN inline `explain()` against a minimal DOM shim,
// so they cover the exact code that ships — not a module copy of the wording.
// The IIFE is extracted from the HTML and evaluated in a vm sandbox; the shim
// records what `#why` ends up containing (plain text, with <code> command
// spans flattened to their text so an assertion can read the whole sentence).

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const HTML = fs.readFileSync(path.join(__dirname, "..", "token-prompt.html"), "utf8");

// Pull the `(function explain() { ... })();` IIFE out of the page's <script>.
const EXPLAIN = (() => {
  const start = HTML.indexOf("(function explain()");
  assert.ok(start !== -1, "explain() IIFE not found in token-prompt.html");
  const end = HTML.indexOf("})();", start);
  assert.ok(end !== -1, "end of explain() IIFE not found");
  return HTML.slice(start, end + "})();".length);
})();

// A minimal document/window shim. `#why` is a node that collects appended text
// and <code> children; reading it back concatenates their text, which is what a
// user sees as one sentence.
function renderWith(search) {
  const makeNode = () => {
    const node = {
      children: [],
      _text: "",
      set textContent(v) { this._text = v; this.children = []; },
      get textContent() {
        if (this.children.length === 0) return this._text;
        return this.children.map((c) => c.textContent).join("");
      },
      appendChild(c) { this.children.push(c); return c; },
    };
    return node;
  };
  const why = makeNode();
  const sandbox = {
    window: { location: { search } },
    URLSearchParams,
    document: {
      getElementById(id) {
        assert.equal(id, "why", "the page only addresses #why");
        return why;
      },
      createTextNode(t) { return { textContent: String(t) }; },
      createElement(tag) {
        assert.equal(tag, "code", "the command renders in a <code> span");
        return makeNode();
      },
    },
  };
  vm.runInNewContext(EXPLAIN, sandbox);
  return why.textContent;
}

const BUNDLED = "/tmp/.mount_Kiro/resources/backend-dist/kirocrew-backend-x64/bin/kirocrew";

// ── Local: our own gateway, token mint failed ──────────────────────────────

test("local on a normal install names the on-PATH command", () => {
  const msg = renderWith("?kind=local&port=5476");
  assert.match(msg, /Run kirocrew token on THIS machine and paste the URL below\./);
  assert.ok(!msg.includes("/bin/kirocrew"), "no bundled path on a normal install");
});

test("local on a packaged AppImage names the bundled launcher", () => {
  // The reported scenario: an AppImage adopts a local gateway, the silent mint
  // is refused, and the page must name a command the user can actually run.
  const msg = renderWith("?kind=local&port=5476&cli=" + encodeURIComponent(BUNDLED));
  assert.ok(msg.includes("Run " + BUNDLED + " token on THIS machine"), msg);
  assert.ok(!/Run kirocrew token/.test(msg), "does not fall back to the bare on-PATH command");
});

test("local quotes a bundled path containing spaces", () => {
  const spaced = "/tmp/.mount A/bin/kirocrew";
  const msg = renderWith("?kind=local&cli=" + encodeURIComponent(spaced));
  assert.ok(msg.includes('Run "' + spaced + '" token on THIS machine'), msg);
});

// ── Foreign: a gateway we did not start (tunnel / external) ─────────────────

test("foreign always names the on-PATH command, even with a bundled path", () => {
  // A foreign gateway's token must be minted on the OTHER machine, where this
  // app's bundled CLI cannot read the secret — so the command stays on-PATH.
  const withCli = renderWith("?kind=foreign&host=dev-host&cli=" + encodeURIComponent(BUNDLED));
  const noCli = renderWith("?kind=foreign&host=dev-host");
  assert.equal(withCli, noCli);
  assert.match(withCli, /run kirocrew token on dev-host, then paste the URL below\./);
  assert.ok(!withCli.includes(BUNDLED), "a foreign gateway never names our bundled launcher");
});

// ── Unknown owner: name both paths without asserting one ────────────────────

test("unknown on a normal install is byte-for-byte the original hedged text", () => {
  const msg = renderWith("?kind=unknown&port=5476");
  assert.equal(
    msg,
    "The gateway on port 5476 would not issue a dashboard token, and this app "
      + "could not determine which machine it is running on. If you reached it "
      + "through an SSH tunnel, run kirocrew token on the machine running that "
      + "gateway; otherwise run it on this machine. Then paste the URL below.",
  );
});

test("unknown on a packaged AppImage names the bundled launcher for the local branch", () => {
  const msg = renderWith("?kind=unknown&port=5476&cli=" + encodeURIComponent(BUNDLED));
  assert.ok(msg.includes("run kirocrew token on the machine running that gateway"));
  assert.ok(msg.includes("otherwise run " + BUNDLED + " token on this machine"), msg);
});

test("a missing kind falls through to the hedged (unknown) branch", () => {
  const msg = renderWith("?port=5476");
  assert.match(msg, /could not determine which machine it is running on/);
  assert.match(msg, /otherwise run it on this machine/);
});
