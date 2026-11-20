"use strict";

// The splash shows a status or ready message only once it has loaded. One sent
// while loading.html is still loading reaches the page being replaced, and the
// splash then shows its default "Starting gateway…" after "Connected ✓". A
// connect to a gateway that already answers sends both at once, so it waits for
// the splash load to end first.

const { EventEmitter } = require("node:events");
const { test } = require("node:test");
const assert = require("node:assert");

const { flush, harness } = require("./gateway-supervisor-test-helpers");

// A connection-window URL: the connect then starts no liveness monitor, whose
// real interval this harness does not inject.
const TARGET = "http://localhost:5999";

// A gateway that is already up: every request, the health probe and the
// dashboard probe alike, answers 200 at once.
const answeringHttp = {
  get(_url, options, callback) {
    const onResponse = typeof options === "function" ? options : callback;
    const request = new EventEmitter();
    request.destroy = () => {};
    queueMicrotask(() => {
      const response = new EventEmitter();
      response.statusCode = 200;
      response.resume = () => {};
      onResponse(response);
      response.emit("end");
    });
    return request;
  },
};

// Each loadFile stays pending until the test settles it, as a real load does
// until the page has loaded or the navigation was replaced.
function splashWindow() {
  const window = {
    loads: [],
    sent: [],
    isDestroyed: () => false,
    show() {},
    webContents: {
      isDestroyed: () => false,
      loadFile(file) {
        let settle;
        const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
        window.loads.push({ file: String(file), ...settle });
        return promise;
      },
      loadURL(url) { window.sent.push(["loadURL", url]); },
      send(channel, message) { window.sent.push([channel, message]); },
    },
  };
  return window;
}

for (const [outcome, settle] of [
  ["loaded", (load) => load.resolve()],
  ["replaced by a later navigation", (load) => load.reject(new Error("ERR_ABORTED (-3)"))],
]) {
  // Bounded: a connect that never settles fails here instead of hanging the suite.
  test(`the connect waits for the splash load to end (${outcome}) before messaging it`, { timeout: 10_000 }, async () => {
    const window = splashWindow();
    const built = harness({ mainWindow: window, httpMod: answeringHttp });

    const connecting = built.supervisor.connect(window, TARGET);
    for (let i = 0; i < 5; i += 1) await flush();
    assert.strictEqual(window.loads.length, 1);
    assert.match(window.loads[0].file, /loading\.html$/);
    assert.deepStrictEqual(window.sent, [], "nothing goes to the page the splash replaces");

    settle(window.loads[0]);
    await connecting;
    const statuses = window.sent.filter(([channel]) => channel === "status").map(([, message]) => message);
    assert.strictEqual(statuses.at(-1), "Connected ✓");
    assert.deepStrictEqual(window.sent.at(-1), ["loadURL", `${TARGET}/`]);
  });
}
