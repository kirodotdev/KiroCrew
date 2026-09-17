"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createWindowLifecycle } = require("../window-lifecycle");

function cancelableEvent() {
  return {
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
}

async function harness(t, { main = false, panels = [] } = {}) {
  const calls = [];
  const dialogs = [];
  let quitting = false;
  const fetch = t.mock.method(globalThis, "fetch", async () => {
    throw new Error("window close tests must not contact a gateway");
  });
  t.after(() => assert.equal(fetch.mock.callCount(), 0));

  class Contents extends EventEmitter {
    destroyed = false;
    crashed = false;
    requests = [];
    session = { webRequest: { onBeforeSendHeaders() {} } };

    constructor(name = "dashboard") {
      super();
      this.name = name;
      this.value = `draft in ${name}`;
    }
    isDestroyed() { return this.destroyed; }
    getZoomFactor() { return 1; }
    setWindowOpenHandler() {}
    loadURL() { return Promise.resolve(); }
    close(options) {
      assert.equal(this.destroyed, false, "must not close destroyed contents");
      this.requests.push(options);
      calls.push(`close-${this.name}`);
      // Native close may accept immediately or time out before a veto arrives.
      // Either outcome destroys this page, so consent must precede the call.
      this.destroy();
    }
    destroy() {
      assert.equal(this.destroyed, false, "must not destroy contents twice");
      this.destroyed = true;
      this.value = undefined;
      calls.push(`${this.name}-destroyed`);
      this.emit("destroyed");
    }
    crash() {
      this.crashed = true;
      this.emit("render-process-gone", {}, { reason: "crashed" });
    }
  }

  class Window extends EventEmitter {
    destroyed = false;
    hidden = false;
    contentView = { addChildView() {}, removeChildView() {} };

    isDestroyed() { return this.destroyed; }
    isFullScreen() { return false; }
    getContentBounds() { return { width: 1280, height: 860 }; }
    getNormalBounds() { return { x: 0, y: 0, width: 1280, height: 860 }; }
    setWindowButtonPosition() {}
    setTitle() {}
    hide() { this.hidden = true; }
    close() {
      const event = cancelableEvent();
      this.emit("close", event);
      if (!event.defaultPrevented) this.destroy();
    }
    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      calls.push("window-destroyed");
      this.emit("closed");
    }
  }

  const lifecycle = createWindowLifecycle({
    electron: {
      BaseWindow: Window,
      WebContentsView: class {
        constructor() {
          this.webContents = new Contents();
          this.webContents.once("destroyed", () => { this.webContents = undefined; });
        }
        setBackgroundColor() {}
        setBounds() {}
      },
      screen: { getAllDisplays: () => [] },
      dialog: {
        showMessageBox(parent, options) {
          assert.equal(parent, win, "the confirmation must belong to the closing window");
          assert.equal(parent.isDestroyed(), false);
          return new Promise((resolve, reject) => {
            dialogs.push({
              options,
              answer(label) {
                const response = options.buttons.indexOf(label);
                assert.notEqual(response, -1, `missing dialog action: ${label}`);
                resolve({ response });
              },
              reject,
            });
          });
        },
      },
    },
    store: { get: () => null, set() {} },
    backendUrl: "https://gateway.example.test:5476",
    port: 5476,
    fetchLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit() {},
    connectWindow: async () => {},
    isQuitting: () => quitting,
    // Pin the affected platform, independently of the test host.
    platform: "darwin",
  });
  const win = main
    ? lifecycle.createMainWindow()
    : lifecycle.createConnectionWindow("https://gateway.example.test:5476", 5476);
  const wc = win.webContents;
  // A remote gateway keeps setup's idle channel from sending a host heartbeat.
  // Stop its idle loop before replacing it with a controllable teardown.
  await win._mcAgentChannel.stop();
  let running = true;
  win._mcAgentChannel = {
    stop() {
      calls.push("stop-channel");
      running = false;
      return Promise.resolve();
    },
    start() {
      calls.push("start-channel");
      running = true;
    },
    isRunning: () => running,
  };
  const pages = panels.map(({ id }) => {
    const page = new Contents(id);
    page.owner = "agent";
    win._mcBrowserPanels.set(id, {
      control: {
        release() {
          calls.push(`release-${id}`);
          page.owner = null;
        },
      },
      manager: {
        getWebContents: () => page,
        refreshBounds() {},
        close() { if (!page.isDestroyed()) page.close(); },
      },
    });
    return page;
  });
  t.after(() => {
    quitting = true;
    win.destroy();
  });
  return { win, wc, pages, calls, dialogs, quit: () => { quitting = true; } };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const closeEvents = ["will-prevent-unload", "destroyed", "render-process-gone"];
const listenerCounts = (wc) => closeEvents.map((name) => wc.listenerCount(name));

function assertStayDefault(dialog) {
  const { options } = dialog;
  assert.equal(options.buttons[options.defaultId], "Stay", "Enter must keep the window");
  assert.equal(options.buttons[options.cancelId], "Stay", "Escape must keep the window");
  assert.equal(options.noLink, true);
}

it("asks before closing any page and keeps clean and dirty pages intact on Stay", async (t) => {
  const { win, wc, pages, calls, dialogs } = await harness(t, {
    panels: [{ id: "clean-page" }, { id: "dirty-page", dirty: true }],
  });
  const values = [wc, ...pages].map((page) => page.value);
  win.close();

  assert.equal(dialogs.length, 1, "embedded pages require confirmation before any close");
  assertStayDefault(dialogs[0]);
  assert.match(dialogs[0].options.detail, /unsaved changes.*dashboard.*browser pages.*lost/i);
  assert.deepEqual(calls, [], "even a clean dashboard must remain alive pending consent");
  dialogs[0].answer("Stay");
  await flush();

  assert.equal(win.isDestroyed(), false);
  assert.equal(win._mcAgentChannel.isRunning(), true);
  assert.equal(win._mcBrowserPanels.size, 2);
  assert.deepEqual([wc, ...pages].map((page) => page.value), values);
  for (const page of [wc, ...pages]) {
    assert.equal(page.isDestroyed(), false);
    assert.deepEqual(page.requests, [], "close must never be used as an unload probe");
  }
  assert.deepEqual(pages.map((page) => page.owner), ["agent", "agent"]);
  assert.deepEqual(calls, []);
});

it("coalesces secondary close requests and preserves the dashboard on Stay", async (t) => {
  const { win, wc, calls, dialogs } = await harness(t);
  const listeners = listenerCounts(wc);

  for (let attempt = 0; attempt < 3; attempt += 1) {
    win.close();
    win.close();
    assert.deepEqual(wc.requests, [], "the dashboard must not be closed to probe for a veto");
    assert.equal(win.isDestroyed(), false);
    win.close();
    assert.equal(dialogs.length, attempt + 1, "repeated requests share one confirmation");
    assertStayDefault(dialogs[attempt]);
    assert.match(dialogs[attempt].options.detail, /unsaved changes.*dashboard.*lost/i);
    assert.doesNotMatch(dialogs[attempt].options.detail, /browser|pages/i);
    dialogs[attempt].answer("Stay");
    await flush();
    assert.equal(wc.isDestroyed(), false);
    assert.equal(wc.value, "draft in dashboard");
    assert.equal(win._mcAgentChannel.isRunning(), true);
    assert.equal(calls.includes("stop-channel"), false);
    assert.deepEqual(listenerCounts(wc), listeners, "retries must not accumulate listeners");
  }

  win.close();
  dialogs.at(-1).answer("Close Window");
  await flush();
  assert.equal(win.isDestroyed(), true, "successful close must remove the native window");
  assert.equal(wc.isDestroyed(), true);
  assert.deepEqual(listenerCounts(wc), [0, 0, 0]);
});

it("explicit Close Window releases every owner before its page and coalesces repeated commands", async (t) => {
  const { win, wc, pages, calls, dialogs } = await harness(t, {
    panels: [{ id: "first" }, { id: "last", dirty: true }],
  });
  win.close();
  win.close();

  assert.equal(dialogs.length, 1);
  assert.deepEqual(calls, []);
  dialogs[0].answer("Close Window");
  await flush();

  assert.equal(win.isDestroyed(), true);
  assert.equal(wc.isDestroyed(), true);
  assert.equal(win._mcView.webContents, undefined, "cleanup must retain its own contents reference");
  assert.equal(win._mcBrowserPanels.size, 0);
  for (const page of pages) {
    assert.equal(page.isDestroyed(), true);
    assert.equal(page.owner, null);
    assert.ok(calls.indexOf("stop-channel") < calls.indexOf(`release-${page.name}`));
    assert.ok(calls.indexOf(`release-${page.name}`) < calls.indexOf(`close-${page.name}`));
    assert.deepEqual(page.requests, [undefined], "explicit consent covers all embedded pages");
  }
  assert.equal(calls.filter((name) => name === "close-dashboard").length, 1);
  assert.equal(calls.filter((name) => name === "stop-channel").length, 1);
  assert.deepEqual(listenerCounts(wc), [0, 0, 0]);
});

it("confirms a clean browser page without relying on beforeunload", async (t) => {
  const { win, dialogs } = await harness(t, { panels: [{ id: "clean" }] });
  win.close();
  assert.equal(dialogs.length, 1);
  dialogs[0].answer("Stay");
  await flush();
  assert.equal(win.isDestroyed(), false);
});

it("explicit Close Window discards dashboard changes only after confirmation", async (t) => {
  const { win, wc, dialogs } = await harness(t);
  win.close();
  assert.equal(dialogs.length, 1);
  assert.equal(wc.isDestroyed(), false);
  dialogs[0].answer("Close Window");
  await flush();
  assert.equal(win.isDestroyed(), true);
  assert.equal(wc.isDestroyed(), true);
  assert.deepEqual(listenerCounts(wc), [0, 0, 0]);
});

it("ignores native unload vetoes unrelated to a window close", async (t) => {
  const { wc, dialogs } = await harness(t);
  const event = cancelableEvent();
  wc.emit("will-prevent-unload", event);
  await flush();
  assert.equal(dialogs.length, 0);
  assert.equal(event.defaultPrevented, false);
});

it("empty or destroyed browser panels are omitted from the dashboard confirmation", async (t) => {
  const { win, wc, pages, dialogs } = await harness(t, { panels: [{ id: "gone" }] });
  pages[0].destroy();
  win._mcBrowserPanel("not-opened");
  win.close();
  assert.deepEqual(wc.requests, []);
  assert.equal(dialogs.length, 1);
  assert.doesNotMatch(dialogs[0].options.detail, /browser|pages/i);
  dialogs[0].answer("Close Window");
  await flush();
  assert.equal(win.isDestroyed(), true);
});

it("prevents new browser contents during confirmation and permits opening after Stay", async (t) => {
  const { win, dialogs } = await harness(t);
  const existing = win._mcBrowserPanel("not-opened");
  win.close();

  for (const entry of [existing, win._mcBrowserPanel("new-panel")]) {
    assert.throws(() => entry.manager.open("https://example.test"), /clos/i);
    assert.equal(entry.manager.getWebContents(), null, "no page may escape the unload decision");
  }
  dialogs[0].answer("Stay");
  await flush();
  existing.manager.open("https://example.test");
  assert.equal(existing.manager.getWebContents().isDestroyed(), false);
  assert.equal(win._mcAgentChannel.isRunning(), true);
});

it("a clean secondary dashboard also requires explicit consent before destruction", async (t) => {
  const { win, wc, calls, dialogs } = await harness(t);
  win.close();

  assert.equal(win.isDestroyed(), false);
  assert.deepEqual(calls, []);
  assert.equal(dialogs.length, 1);
  dialogs[0].answer("Close Window");
  await flush();
  assert.equal(win.isDestroyed(), true);
  assert.equal(wc.isDestroyed(), true);
  assert.deepEqual(wc.requests, [undefined]);
  assert.deepEqual(listenerCounts(wc), [0, 0, 0]);
});

it("main close still hides to tray without invoking beforeunload or teardown", async (t) => {
  const { win, wc, pages, calls, dialogs } = await harness(t, {
    main: true, panels: [{ id: "dirty", dirty: true }],
  });
  win.close();

  assert.equal(win.hidden, true);
  assert.equal(win.isDestroyed(), false);
  assert.equal(wc.isDestroyed(), false);
  assert.equal(pages[0].isDestroyed(), false);
  assert.equal(dialogs.length, 0);
  assert.deepEqual(calls, []);
});

for (const main of [false, true]) {
  it(`quit closes a ${main ? "main" : "secondary"} window without awaiting beforeunload`, async (t) => {
    const { win, wc, quit, dialogs } = await harness(t, {
      main, panels: [{ id: "dirty", dirty: true }],
    });
    quit();
    win.close();

    assert.equal(win.isDestroyed(), true);
    assert.equal(wc.isDestroyed(), true);
    assert.equal(dialogs.length, 0);
    assert.deepEqual(wc.requests, [undefined]);
  });
}

it("quit takes over a pending secondary close and releases its listeners", async (t) => {
  const { win, wc, quit } = await harness(t);
  win.close();
  quit();
  win.close();

  assert.equal(win.isDestroyed(), true);
  assert.equal(wc.isDestroyed(), true);
  assert.deepEqual(wc.requests, [undefined]);
  assert.deepEqual(listenerCounts(wc), [0, 0, 0]);
});

for (const reason of ["quit", "window destruction"]) {
  it(`${reason} takes over a pending dialog without duplicate teardown on its late answer`, async (t) => {
    const { win, wc, calls, dialogs, quit } = await harness(t, {
      panels: [{ id: "dirty", dirty: true }],
    });
    win.close();
    assert.equal(dialogs.length, 1);
    if (reason === "quit") {
      quit();
      win.close();
    } else {
      win.destroy();
    }
    assert.equal(win.isDestroyed(), true);
    assert.equal(wc.isDestroyed(), true);
    const teardown = [...calls];
    dialogs[0].answer("Close Window");
    await flush();
    assert.deepEqual(calls, teardown);
    assert.deepEqual(listenerCounts(wc), [0, 0, 0]);
  });
}

for (const withPages of [false, true]) {
  it(`dialog failure preserves ${withPages ? "embedded pages" : "the dashboard draft"} and allows retry`, async (t) => {
    const { win, wc, pages, calls, dialogs } = await harness(t, {
      panels: withPages ? [{ id: "dirty", dirty: true }] : [],
    });
    win.close();
    assert.equal(dialogs.length, 1);
    dialogs[0].reject(new Error("native dialog failed"));
    await flush();
    assert.equal(win.isDestroyed(), false);
    assert.equal(win._mcAgentChannel.isRunning(), true);
    for (const page of [wc, ...pages]) {
      assert.equal(page.isDestroyed(), false);
      assert.equal(page.value, `draft in ${page.name}`);
    }
    assert.equal(calls.includes("stop-channel"), false);

    win.close();
    assert.equal(dialogs.length, 2);
    dialogs[1].answer("Close Window");
    await flush();
    assert.equal(win.isDestroyed(), true);
  });
}

it("a crashed secondary dashboard can still be closed through the native confirmation", async (t) => {
  const { win, wc, dialogs } = await harness(t);
  wc.crash();
  win.close();

  assert.equal(dialogs.length, 1);
  dialogs[0].answer("Close Window");
  await flush();
  assert.equal(win.isDestroyed(), true);
  assert.equal(wc.isDestroyed(), true);
  assert.deepEqual(listenerCounts(wc), [0, 0, 0]);
});

it("renderer death does not override an outstanding window-close decision", async (t) => {
  const { win, wc, dialogs } = await harness(t);
  win.close();
  assert.equal(win.isDestroyed(), false);
  wc.crash();

  assert.equal(win.isDestroyed(), false);
  assert.equal(dialogs.length, 1);
  dialogs[0].answer("Close Window");
  await flush();
  assert.equal(win.isDestroyed(), true);
  assert.deepEqual(listenerCounts(wc), [0, 0, 0]);
});

for (const moment of ["before close", "during confirmation"]) {
  it(`a dashboard crash ${moment} cannot discard live browser pages without consent`, async (t) => {
    const { win, wc, pages, dialogs } = await harness(t, {
      panels: [{ id: "dirty", dirty: true }],
    });
    if (moment === "before close") wc.crash();
    win.close();
    if (moment === "during confirmation") wc.crash();
    assert.equal(dialogs.length, 1);
    assert.equal(win.isDestroyed(), false);
    dialogs[0].answer("Stay");
    await flush();
    assert.equal(win.isDestroyed(), false);
    assert.equal(pages[0].value, "draft in dirty");
    assert.equal(pages[0].owner, "agent");
    assert.equal(win._mcAgentChannel.isRunning(), true);
    win.close();
    dialogs[1].answer("Close Window");
    await flush();
    assert.equal(win.isDestroyed(), true);
  });
}

it("external dashboard destruction keeps remaining browser pages until explicit consent", async (t) => {
  const { win, wc, pages, dialogs } = await harness(t, {
    panels: [{ id: "dirty", dirty: true }],
  });
  wc.destroy();
  assert.equal(win._mcView.webContents, undefined);
  assert.equal(win.isDestroyed(), false);
  assert.equal(dialogs.length, 1);
  assert.doesNotThrow(() => win.emit("focus"));
  assert.doesNotThrow(() => win.emit("enter-full-screen"));
  dialogs[0].answer("Stay");
  await flush();
  assert.equal(pages[0].value, "draft in dirty");
  assert.equal(pages[0].owner, "agent");
  assert.equal(win._mcAgentChannel.isRunning(), true);
  win.close();
  assert.equal(dialogs.length, 2);
  dialogs[1].answer("Close Window");
  await flush();
  assert.equal(win.isDestroyed(), true);
  assert.deepEqual(wc.requests, []);
});

it("already destroyed contents do not throw or strand a secondary window", async (t) => {
  const { win, wc } = await harness(t);
  wc.destroy();
  if (!win.isDestroyed()) win.close();

  assert.equal(win.isDestroyed(), true);
  assert.deepEqual(wc.requests, []);
});

it("a secondary close never probes even an unresponsive dashboard", async (t) => {
  const { win, wc, dialogs } = await harness(t);
  const close = t.mock.method(wc, "close", () => {
    throw new Error("probing beforeunload can destroy an unresponsive renderer");
  });
  try {
    win.close();
    assert.equal(dialogs.length, 1);
    assert.deepEqual(wc.requests, []);
    dialogs[0].answer("Stay");
    await flush();

    assert.equal(win.isDestroyed(), false);
    assert.equal(wc.isDestroyed(), false);
    assert.equal(wc.value, "draft in dashboard");
    assert.equal(win._mcAgentChannel.isRunning(), true);
  } finally {
    close.mock.restore();
  }
});
