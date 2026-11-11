"use strict";

// Enter in the shell's small forms submits them, but Enter on a focused BUTTON is
// that button's own press. A keyboard user who tabs to Cancel and presses Enter
// must get Cancel: the form closes and nothing is saved. The handler on the
// document used to run the submit first, so Cancel saved the form.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

const { createRemoteCrewPrompt } = require("../runtime/gateway/remote-crew-prompt");
const { createWindowPrompts } = require("../runtime/window/prompts");

class HTMLButtonElement {}
class HTMLInputElement {}

/** Run a form's inline script, press a key on `target`, and report what it did. */
function pressKey(html, key, target) {
  const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\b[^>]*>/gi)];
  assert.ok(scripts.length, "the form has an inline script");
  const keydown = [];
  const outcome = { saved: false, closed: false };
  const document = {
    set title(value) { outcome.saved = true; outcome.title = value; },
    get title() { return outcome.title || ""; },
    addEventListener(type, fn) { if (type === "keydown") keydown.push(fn); },
    getElementById: () => ({ value: "devbox", checked: false }),
  };
  const window = { close() { outcome.closed = true; } };
  vm.runInNewContext(scripts.at(-1)[1], { document, window, HTMLButtonElement, HTMLInputElement });
  for (const fn of keydown) fn({ key, target });
  return outcome;
}

function decode(url) {
  return decodeURIComponent(url.replace(/^data:text\/html;charset=utf-8,/, ""));
}

async function remoteCrewForm() {
  let html = "";
  class FakeWindow extends EventEmitter {
    setMenu() {}
    isDestroyed() { return false; }
    loadURL(url) { html = decode(url); this.emit("closed"); }
  }
  const { promptRemoteCrew } = createRemoteCrewPrompt({
    BrowserWindow: FakeWindow,
    nativeTheme: { shouldUseDarkColors: false },
  });
  await promptRemoteCrew(null, 5477, { host: "devbox" });
  return html;
}

function promptsHarness() {
  const pages = [];
  class PromptWindow {
    constructor() { this.handlers = {}; }
    setMenu() {}
    on(event, handler) { this.handlers[event] = handler; }
    loadURL(url) { pages.push(decode(url)); }
  }
  const focused = {
    _mcSetCustomName() {},
    _mcBackendUrl: "http://127.0.0.1:5477",
    getTitle: () => "Kiro Crew",
    isDestroyed: () => false,
  };
  const prompts = createWindowPrompts({
    BaseWindow: { getFocusedWindow: () => focused },
    BrowserWindow: PromptWindow,
    nativeTheme: { shouldUseDarkColors: false },
    store: { get: () => null },
    getMainWindow: () => null,
  });
  return { prompts, pages };
}

const settle = () => new Promise((resolve) => setImmediate(() => setImmediate(resolve)));

async function forms() {
  const { prompts, pages } = promptsHarness();
  await prompts.promptConnectionPort(() => null, async () => {});
  prompts.renameFocusedWindow();
  await settle();
  assert.equal(pages.length, 2, "both window prompts rendered");
  return { "remote crew": await remoteCrewForm(), "connection port": pages[0], "rename window": pages[1] };
}

test("Enter on a focused button does not submit the form", async () => {
  for (const [name, html] of Object.entries(await forms())) {
    const outcome = pressKey(html, "Enter", new HTMLButtonElement());
    assert.equal(outcome.saved, false, `${name}: Enter on Cancel saved the form`);
  }
});

test("Enter in a field still submits the form, and Escape still closes it unsaved", async () => {
  for (const [name, html] of Object.entries(await forms())) {
    const enter = pressKey(html, "Enter", new HTMLInputElement());
    assert.equal(enter.saved, true, `${name}: Enter in a field no longer submits`);
    assert.equal(enter.closed, true, `${name}: a submit closes the form`);
    const escape = pressKey(html, "Escape", new HTMLInputElement());
    assert.deepEqual([escape.saved, escape.closed], [false, true], `${name}: Escape`);
  }
});

// The remote-host form in window-lifecycle.js is built inside the window
// lifecycle itself, so its handler is pinned on the source.
test("the remote-host form in window-lifecycle.js leaves Enter on a button to the button", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "window-lifecycle.js"), "utf8");
  const enterHandlers = [...source.matchAll(/if \(event\.key === 'Enter'[^)]*\)?[^\n]*/g)].map((m) => m[0]);
  assert.ok(enterHandlers.length > 0, "the form's Enter handler was found");
  for (const line of enterHandlers) {
    assert.match(line, /!\(event\.target instanceof HTMLButtonElement\)/, line);
  }
});
