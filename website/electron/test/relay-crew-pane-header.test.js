// The pane's own header (#42922): the user-agent token a dashboard asks for, and
// the one boolean the shell reads from the pane's untrusted page.
// Split from relay-crew-window.test.js, which holds the rest of the pane.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const { openRelayCrewWindow, PANE_HEADER_READ } = require("../relay-crew-window");
const { ENDPOINT, DOMAIN, CREW_URL, ORIGIN, TOKEN, fakeDeps, openPane, flush } = require("./relay-crew-window-test-helpers");

test("pane: the header read answers a boolean, true only when the page marked a header of its own", () => {
  const vm = require("node:vm");
  const answer = (found) => vm.runInNewContext(PANE_HEADER_READ, {
    document: { querySelector: (sel) => (sel === "header[data-relay-pane-header]" ? found : null) },
  });
  assert.strictEqual(answer({}), true);
  assert.strictEqual(answer(null), false, "an older page, or one told to leave its header out, has no such element");
});

test("pane: the header token is sent only for a dashboard that asked, after the pane marker and whole", () => {
  for (const [asked, ua] of [
    [true, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow kiroCrewRelayPane kiroCrewRelayPaneHeader"],
    // An older dashboard keeps its whole bar, so a page it shows must not draw a second one.
    [undefined, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow kiroCrewRelayPane"],
    [false, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow kiroCrewRelayPane"],
    ["true", "Mozilla/5.0 Electron/44 kiroCrewRelayWindow kiroCrewRelayPane"],
    [1, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow kiroCrewRelayPane"],
  ]) {
    const f = fakeDeps();
    const p = openPane(f, { ownHeader: asked });
    assert.strictEqual(p.view.webContents.userAgent, ua, JSON.stringify(asked));
    // An older page reads the pane marker as a whole token either way, and leaves its header out.
    assert.ok(p.view.webContents.userAgent.split(" ").includes("kiroCrewRelayPane"));
    assert.strictEqual(f.ses.userAgent, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow");
    p.handle.close();
  }
});

test("pane: for a dashboard that did not ask, the page is never read for a header and nothing is ever said", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f);
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  // A page that claims a header anyway.
  wc.headerAnswer = true;
  wc.emit("did-finish-load");
  wc.emit("did-navigate-in-page");
  p.handle.setInactive(false);
  for (let i = 0; i < 50; i += 1) { t.mock.timers.tick(100); await flush(); }
  assert.strictEqual(wc.headerReads, 0);
  assert.deepStrictEqual(seen, []);
  p.handle.close();
});

test("pane: says its page drew a header only after a read answered true, once, and reads no more for that load", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { unloaded: true, ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  wc.emit("did-finish-load");
  for (let i = 0; i < 3; i += 1) { t.mock.timers.tick(100); await flush(); }
  assert.strictEqual(wc.headerReads, 3, "one read each 100 ms while the page has not drawn it");
  assert.deepStrictEqual(seen, [], "false is where it starts, so nothing is said until a header is there");
  wc.headerAnswer = true;
  t.mock.timers.tick(100);
  await flush();
  assert.deepStrictEqual(seen, [true]);
  const reads = wc.headerReads;
  t.mock.timers.tick(5000);
  await flush();
  assert.strictEqual(wc.headerReads, reads, "the load's reads end with the answer");
  assert.deepStrictEqual(seen, [true], "and a listener hears a change, never a repeat");
  p.handle.close();
});

test("pane: an older page never draws one: the load's reads run out and nothing is said", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { unloaded: true, ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  wc.emit("did-finish-load");
  for (let i = 0; i < 80; i += 1) { t.mock.timers.tick(100); await flush(); }
  assert.strictEqual(wc.headerReads, 30, "3 s of reads after a load, then none");
  assert.deepStrictEqual(seen, []);
  p.handle.close();
});

test("pane: only the literal true counts; any other answer, and a failed read, mean no header", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  const read = async (answer) => {
    wc.executeJavaScript = (src) => (src === PANE_HEADER_READ ? answer() : Promise.resolve(""));
    wc.emit("did-navigate-in-page");
    t.mock.timers.tick(100);
    await flush();
  };
  for (const untrusted of ["true", 1, "yes", {}, [true], { header: true }, null, undefined, "<img src=x onerror=alert(1)>"]) {
    await read(() => Promise.resolve(untrusted));
    assert.deepStrictEqual(seen, [], `${JSON.stringify(untrusted)} is not true`);
  }
  await read(() => Promise.resolve(true));
  assert.deepStrictEqual(seen, [true]);
  await read(() => Promise.reject(new Error("page threw")));
  assert.deepStrictEqual(seen, [true, false], "a read that fails takes it back");
  await read(() => Promise.resolve(true));
  await read(() => { throw new Error("no frame to run in"); });
  assert.deepStrictEqual(seen, [true, false, true, false], "as does one that cannot start");
  for (const told of seen) assert.strictEqual(typeof told, "boolean", "only booleans leave the pane");
  p.handle.close();
});

test("pane: a new document or a lost renderer takes the header back at once, and the next load is read again", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  wc.headerAnswer = true;
  wc.emit("did-finish-load");
  t.mock.timers.tick(100);
  await flush();
  assert.deepStrictEqual(seen, [true]);
  // A reload, a notice's about:blank, a sign-in page: the page that drew it is gone.
  wc.headerAnswer = false;
  wc.emit("did-navigate", {}, `${ORIGIN}/`, 200);
  assert.deepStrictEqual(seen, [true, false], "before the new page has answered anything");
  wc.emit("did-finish-load");
  for (let i = 0; i < 40; i += 1) { t.mock.timers.tick(100); await flush(); }
  assert.deepStrictEqual(seen, [true, false], "a page that reloads without one keeps the local bar whole");
  wc.headerAnswer = true;
  wc.emit("did-navigate", {}, `${ORIGIN}/`, 200);
  wc.emit("did-finish-load");
  t.mock.timers.tick(100);
  await flush();
  assert.deepStrictEqual(seen, [true, false, true]);
  wc.emit("render-process-gone", {}, { reason: "crashed" });
  assert.deepStrictEqual(seen, [true, false, true, false]);
  p.handle.close();
});

test("pane: a failed main-frame load takes the header back; a cancelled one or a subframe's does not", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  wc.headerAnswer = true;
  wc.emit("did-finish-load");
  t.mock.timers.tick(100);
  await flush();
  assert.deepStrictEqual(seen, [true]);
  wc.emit("did-fail-load", {}, -3, "ERR_ABORTED", `${ORIGIN}/`, true);
  wc.emit("did-fail-load", {}, -105, "ERR_NAME_NOT_RESOLVED", `${ORIGIN}/frame`, false);
  assert.deepStrictEqual(seen, [true], "the page is still the one that drew it");
  // The relay dropped on a reload: an error page stands where the Crew's page was.
  wc.headerAnswer = false;
  wc.emit("did-fail-load", {}, -105, "ERR_NAME_NOT_RESOLVED", `${ORIGIN}/`, true);
  assert.deepStrictEqual(seen, [true, false], "at once, with no did-navigate");
  // Were the old page still there, the read that follows would say so.
  wc.headerAnswer = true;
  t.mock.timers.tick(100);
  await flush();
  assert.deepStrictEqual(seen, [true, false, true]);
  p.handle.close();
});

test("pane: while it is the tab in front it reads once a second, so a late header and a lost one are both seen", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  p.handle.setInactive(false);
  wc.emit("did-finish-load");
  // The page draws nothing for longer than the load's quick reads last.
  for (let i = 0; i < 40; i += 1) { t.mock.timers.tick(100); await flush(); }
  const afterLoad = wc.headerReads;
  assert.deepStrictEqual(seen, [false], "what it says on coming to the front: none yet");
  wc.headerAnswer = true;
  t.mock.timers.tick(1000);
  await flush();
  assert.deepStrictEqual(seen, [false, true], "drawn late, with no navigation");
  // Its crash screen replaces the header, again with no navigation.
  wc.headerAnswer = false;
  t.mock.timers.tick(1000);
  await flush();
  assert.deepStrictEqual(seen, [false, true, false]);
  const front = wc.headerReads;
  assert.ok(front - afterLoad <= 3, "about one read a second, not a quick loop");
  for (let i = 0; i < 10; i += 1) { t.mock.timers.tick(1000); await flush(); }
  assert.strictEqual(wc.headerReads, front + 10, "one a second while it stays in front");
  // Behind another tab: nothing to keep in step, so no reads.
  p.handle.setInactive(true);
  for (let i = 0; i < 10; i += 1) { t.mock.timers.tick(1000); await flush(); }
  assert.strictEqual(wc.headerReads, front + 10);
  p.handle.close();
});

test("pane: every answer from the page on screen counts, in order, however slowly it comes", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { unloaded: true, ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  const answers = [];
  wc.executeJavaScript = (src) => (src === PANE_HEADER_READ ? new Promise((resolve) => answers.push(resolve)) : Promise.resolve(""));
  wc.emit("did-finish-load");
  // A busy page: three reads go out before the first answer comes back.
  for (let i = 0; i < 3; i += 1) { t.mock.timers.tick(100); await flush(); }
  assert.strictEqual(answers.length, 3);
  answers[0](true);
  await flush();
  assert.deepStrictEqual(seen, [true], "the first answer is not thrown away because later reads were sent");
  p.handle.close();
});

test("pane: a page that keeps flipping its header is held at none after three take-backs, until its next document", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  const flip = async (answer) => {
    wc.headerAnswer = answer;
    wc.emit("did-navigate-in-page");
    t.mock.timers.tick(100);
    await flush();
  };
  for (let i = 0; i < 10; i += 1) { await flip(true); await flip(false); }
  assert.deepStrictEqual(seen, [true, false, true, false, true, false], "three times, then the local bar stays whole");
  await flip(true);
  assert.deepStrictEqual(seen.length, 6);
  // A new document starts again.
  wc.emit("did-navigate", {}, `${ORIGIN}/`, 200);
  await flip(true);
  assert.deepStrictEqual(seen, [true, false, true, false, true, false, true]);
  p.handle.close();
});

test("pane: an answer from the page before a navigation does not count for the one after", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  const answers = [];
  wc.executeJavaScript = (src) => (src === PANE_HEADER_READ ? new Promise((resolve) => answers.push(resolve)) : Promise.resolve(""));
  wc.emit("did-navigate-in-page");
  t.mock.timers.tick(100);
  await flush();
  assert.strictEqual(answers.length, 1);
  wc.emit("did-navigate", {}, `${ORIGIN}/`, 200);
  answers[0](true);
  await flush();
  assert.deepStrictEqual(seen, [], "the old page's late true is dropped");
  p.handle.close();
});

test("pane: coming to the front says the current answer again and reads; a page navigating in a loop gets one read per 100 ms", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  wc.headerAnswer = true;
  t.mock.timers.tick(100);
  await flush();
  // A dashboard that starts listening only now missed the first answer.
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  const base = wc.headerReads;
  p.handle.setInactive(false);
  assert.deepStrictEqual(seen, [true], "said again as the tab comes to the front");
  t.mock.timers.tick(100);
  await flush();
  assert.strictEqual(wc.headerReads, base + 1, "and read again");
  p.handle.setInactive(true);
  wc.headerAnswer = false;
  for (let i = 0; i < 500; i += 1) wc.emit("did-navigate-in-page");
  t.mock.timers.tick(100);
  await flush();
  assert.strictEqual(wc.headerReads, base + 2, "500 navigations, one read");
  assert.deepStrictEqual(seen, [true, false], "and the header it no longer draws is taken back");
  // A page that never stops navigating is still read, once per 100 ms.
  for (let i = 0; i < 5; i += 1) {
    for (let n = 0; n < 50; n += 1) wc.emit("did-navigate-in-page");
    t.mock.timers.tick(100);
    await flush();
  }
  assert.strictEqual(wc.headerReads, base + 7);
  t.mock.timers.tick(5000);
  await flush();
  assert.strictEqual(wc.headerReads, base + 7, "behind another tab, no timer keeps reading");
  p.handle.close();
});

test("pane: a closed pane reads nothing, says nothing, and leaves no header timer behind", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fakeDeps();
  const p = openPane(f, { ownHeader: true });
  const wc = p.view.webContents;
  const seen = [];
  p.handle.onHeaderDrawn((drawn) => seen.push(drawn));
  wc.headerAnswer = true;
  wc.emit("did-finish-load");
  p.handle.close();
  const reads = wc.headerReads;
  t.mock.timers.tick(10000);
  await flush();
  wc.emit("did-navigate-in-page");
  wc.emit("did-navigate", {}, `${ORIGIN}/`, 200);
  p.handle.setInactive(false);
  t.mock.timers.tick(10000);
  await flush();
  assert.strictEqual(wc.headerReads, reads);
  assert.deepStrictEqual(seen, []);
});

test("window: a relay Crew in its own window is sent no header token and is never asked about a header", () => {
  const f = fakeDeps();
  const win = openRelayCrewWindow({ endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN }, f.deps);
  assert.strictEqual(f.ses.userAgent, "Mozilla/5.0 Electron/44 kiroCrewRelayWindow");
  assert.strictEqual(f.views[0].webContents.userAgent, undefined, "the window's page keeps the session's user agent");
  assert.strictEqual(win.onHeaderDrawn, undefined);
  f.views[0].webContents.emit("did-finish-load");
  assert.strictEqual(f.views[0].webContents.headerReads, 0);
  win.close();
});
