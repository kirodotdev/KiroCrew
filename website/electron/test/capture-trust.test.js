const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  registerCaptureSurface,
  registeredOrigin,
  createCaptureTrust,
} = require("../capture-trust");

const DASH = "http://localhost:5476";
const PANE = "http://localhost:7778";

/**
 * A webContents whose main frame is `mainFrame`, plus the frames that resolve
 * back to it. Mirrors the shape Electron hands the handler: a request carries a
 * WebFrameMain, and `webContents.fromFrame` maps it to its owner.
 */
function surface(url) {
  const wc = { id: Math.random() };
  const main = { parent: null, url, wc };
  wc.mainFrame = main;
  return { wc, main };
}

/** Trust predicate over a fixed frame->webContents mapping. */
function trustOver(...surfaces) {
  const owners = new Map();
  for (const s of surfaces) {
    owners.set(s.main, s.wc);
    for (const sub of s.subframes || []) owners.set(sub, s.wc);
  }
  return createCaptureTrust({ fromFrame: (frame) => owners.get(frame) });
}

describe("registerCaptureSurface", () => {
  it("keeps the origin, so in-app navigation within it stays registered", () => {
    const { wc } = surface(`${DASH}/chat`);
    assert.equal(registerCaptureSurface(wc, `${DASH}/chat?token=secret`), true);
    assert.equal(registeredOrigin(wc), DASH);
  });

  it("refuses a url it cannot turn into a comparable origin", () => {
    const { wc } = surface(`${DASH}/chat`);
    // An unparseable or opaque origin has nothing a later request can be
    // compared against, and registering "null" would make two unrelated opaque
    // documents compare equal to each other.
    assert.equal(registerCaptureSurface(wc, "not a url"), false);
    assert.equal(registerCaptureSurface(wc, "data:text/html,<p>x"), false);
    assert.equal(registerCaptureSurface(wc, ""), false);
    assert.equal(registerCaptureSurface(wc, undefined), false);
    assert.equal(registerCaptureSurface(null, DASH), false);
    assert.equal(registeredOrigin(wc), undefined);
  });
});

describe("createCaptureTrust", () => {
  it("requires its runtime glue rather than defaulting to a guess", () => {
    assert.throws(() => createCaptureTrust({}), /fromFrame is required/);
    assert.throws(() => createCaptureTrust(), /fromFrame is required/);
  });

  it("authorizes a registered surface's own main frame", () => {
    const dash = surface(`${DASH}/chat`);
    registerCaptureSurface(dash.wc, DASH);
    assert.equal(trustOver(dash)({ frame: dash.main }), true);
  });

  it("authorizes it across in-app navigation within the same origin", () => {
    const dash = surface(`${DASH}/chat`);
    registerCaptureSurface(dash.wc, DASH);
    const trust = trustOver(dash);
    dash.main.url = `${DASH}/settings/appearance?tab=2`;
    assert.equal(trust({ frame: dash.main }), true);
  });

  it("refuses an embedded pane's subframe (leg 2: not the main frame)", () => {
    // The instances pane: the same SPA on a tunnel port, nested in the
    // dashboard's document, and delegated `display-capture` by the parent.
    const dash = surface(`${DASH}/chat`);
    dash.subframes = [{ parent: dash.main, url: `${PANE}/?token=x`, wc: dash.wc }];
    registerCaptureSurface(dash.wc, DASH);
    assert.equal(trustOver(dash)({ frame: dash.subframes[0] }), false);
  });

  it("refuses a same-origin subframe, which only the main-frame leg can", () => {
    // An iframe on the dashboard's OWN origin -- a rendered widget, an artifact
    // preview, a pane pointed at this same gateway. Its origin matches the
    // registered one exactly, so leg 3 cannot help and leg 2 is the whole
    // defence. Delegating `display-capture` reaches these frames too.
    const dash = surface(`${DASH}/chat`);
    dash.subframes = [{ parent: dash.main, url: `${DASH}/widget/preview`, wc: dash.wc }];
    registerCaptureSurface(dash.wc, DASH);
    assert.equal(trustOver(dash)({ frame: dash.subframes[0] }), false);
  });

  it("refuses a pane that promoted itself to the top frame (leg 3: origin moved)", () => {
    // `target="_top"` from inside a pane replaces the dashboard's top document
    // with one the pane's origin controls. It IS the main frame now, and its
    // host is the same loopback host, so only the PORT separates it.
    const dash = surface(`${DASH}/chat`);
    registerCaptureSurface(dash.wc, DASH);
    const trust = trustOver(dash);
    assert.equal(trust({ frame: dash.main }), true);
    dash.main.url = `${PANE}/hostile`;
    assert.equal(trust({ frame: dash.main }), false);
  });

  it("still authorizes a promotion to the app's own origin", () => {
    // Not an escalation: that origin is the local gateway serving this app's
    // own token-authed pages, so the document is the app's either way.
    const dash = surface(`${DASH}/chat`);
    registerCaptureSurface(dash.wc, DASH);
    const trust = trustOver(dash);
    dash.main.url = `${DASH}/some/other/page`;
    assert.equal(trust({ frame: dash.main }), true);
  });

  it("refuses an unregistered webContents (leg 1)", () => {
    const other = surface("https://example.com/page");
    assert.equal(trustOver(other)({ frame: other.main }), false);
  });

  it("keeps sibling surfaces on their own origins", () => {
    // A secondary window pointed at a remote gateway is a legitimate capture
    // surface on a DIFFERENT loopback port. Each is bound to its own origin, so
    // one window's port never authorizes another window's document.
    const dash = surface(`${DASH}/chat`);
    const remote = surface(`${PANE}/chat`);
    registerCaptureSurface(dash.wc, DASH);
    registerCaptureSurface(remote.wc, PANE);
    const trust = trustOver(dash, remote);
    assert.equal(trust({ frame: dash.main }), true);
    assert.equal(trust({ frame: remote.main }), true);
    dash.main.url = `${PANE}/hostile`;
    assert.equal(trust({ frame: dash.main }), false);
  });

  it("refuses when the frame resolves to no webContents", () => {
    const dash = surface(`${DASH}/chat`);
    registerCaptureSurface(dash.wc, DASH);
    // Electron returns undefined once the frame's contents are gone.
    const trust = createCaptureTrust({ fromFrame: () => undefined });
    assert.equal(trust({ frame: dash.main }), false);
  });

  it("refuses an absent, null or non-object frame", () => {
    const dash = surface(`${DASH}/chat`);
    registerCaptureSurface(dash.wc, DASH);
    const trust = trustOver(dash);
    assert.equal(trust(undefined), false);
    assert.equal(trust({}), false);
    assert.equal(trust({ frame: null }), false);
    assert.equal(trust({ frame: "top" }), false);
  });

  it("refuses when any read throws, rather than letting it escape", () => {
    // A destroyed WebFrameMain or webContents throws on property access, and a
    // throw from here would land inside Chromium's request handling.
    const dash = surface(`${DASH}/chat`);
    registerCaptureSurface(dash.wc, DASH);

    const throwingRequest = {
      get frame() {
        throw new Error("frame destroyed");
      },
    };
    assert.equal(trustOver(dash)(throwingRequest), false);

    assert.equal(
      createCaptureTrust({
        fromFrame: () => {
          throw new Error("contents destroyed");
        },
      })({ frame: dash.main }),
      false,
    );

    const badMain = surface(`${DASH}/chat`);
    registerCaptureSurface(badMain.wc, DASH);
    Object.defineProperty(badMain.wc, "mainFrame", {
      get() {
        throw new Error("contents destroyed");
      },
    });
    assert.equal(trustOver(badMain)({ frame: badMain.main }), false);

    const badUrl = surface(`${DASH}/chat`);
    registerCaptureSurface(badUrl.wc, DASH);
    Object.defineProperty(badUrl.main, "url", {
      get() {
        throw new Error("frame destroyed");
      },
    });
    assert.equal(trustOver(badUrl)({ frame: badUrl.main }), false);
  });

  it("refuses an unusable frame url", () => {
    const dash = surface(`${DASH}/chat`);
    registerCaptureSurface(dash.wc, DASH);
    const trust = trustOver(dash);
    for (const url of ["", "not a url", undefined, null, 7]) {
      dash.main.url = url;
      assert.equal(trust({ frame: dash.main }), false);
    }
  });
});
