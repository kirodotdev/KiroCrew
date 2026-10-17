"use strict";

// The failure dialog driven the way the user drives it: Start Local Gateway
// and Retry on a client-only launch, the copy an occupied port gets, and the
// "Installation still finishing" dialog's auto-retry while the bundle lands.
// Shared fakes live in gateway-supervisor-test-helpers.js.

const { EventEmitter } = require("node:events");
const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert");

const {
  fakeStore,
  fakeTimers,
  flush,
  harness,
  APP_EXEC_PATH,
  APP_ARGV,
  successorCall,
  SUCCESSOR_POLL_MS,
  ownerProbe,
  OWN_GATEWAY_COMMAND,
  INSTALLING_PROBE_MS,
  INSTALLING_COMPLETE_LINGER_MS,
  fakeDialogWindows,
} = require("./gateway-supervisor-test-helpers");

// Driving the failure dialog itself: the fake window records the document the
// dialog loads and answers it the way the page does, by setting a title the
// supervisor reads back. That makes the enable-retry click reachable without an
// Electron runtime, so the states the user actually sees can be asserted rather
// than inferred from the source.
function clientOnlyClickHarness({
  readyAnswers,
  actions,
  store,
  port,
  execFileFn,
  httpMod: httpOverride,
  platform = "test",
  timers,
  appExecMissing = false,
}) {
  const documents = [];
  const queue = [...actions];
  const state = { exits: [], lockReleases: 0 };

  class DialogWindow {
    constructor() { this.handlers = new Map(); }
    setMenu() {}
    on(event, handler) { this.handlers.set(event, handler); }
    isDestroyed() { return false; }
    loadURL(url) {
      documents.push(decodeURIComponent(
        String(url).replace(/^data:text\/html;charset=utf-8,/, ""),
      ));
      const action = queue.shift() || "quit";
      setImmediate(() => {
        const titled = this.handlers.get("page-title-updated");
        if (titled) titled({}, `mc-action:${action}`);
        const closed = this.handlers.get("closed");
        if (closed) closed();
      });
    }
  }

  // Refuses this app's own status probe, so the client-only failure surfaces, and
  // optionally ANSWERS the successor's readiness probe, which is what makes the
  // handoff refuse before spawning anything.
  const httpMod = {
    get(url, _options, callback) {
      const request = new EventEmitter();
      request.destroy = () => {};
      const isReady = String(url).includes("/api/ready");
      queueMicrotask(() => {
        if (isReady && readyAnswers) {
          const response = new EventEmitter();
          response.statusCode = 200;
          response.resume = () => {};
          if (typeof callback === "function") callback(response);
          response.emit("data", "{}");
          response.emit("end");
          return;
        }
        request.emit("error", new Error("connection refused"));
      });
      return request;
    },
  };

  const mainWindow = {
    isDestroyed: () => false,
    isMinimized: () => false,
    restore() {},
    show() {},
    focus() {},
    webContents: { isDestroyed: () => false, loadFile() {}, send() {} },
  };

  const built = harness({
    store: store || fakeStore({
      runLocalGateway: false,
      remoteHosts: { 7778: { host: "crew.example.com" } },
    }),
    port: port ?? 7778,
    predictLocalPort: () => 5476,
    BrowserWindow: DialogWindow,
    httpMod: httpOverride || httpMod,
    mainWindow,
    ...(execFileFn ? { execFileFn } : {}),
    ...(timers ? { timers } : {}),
    app: {
      exit: (code) => state.exits.push(code),
      releaseSingleInstanceLock: () => { state.lockReleases += 1; },
      requestSingleInstanceLock: () => true,
    },
    processRef: {
      platform,
      arch: "x64",
      execPath: APP_EXEC_PATH,
      argv: APP_ARGV,
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    fsMod: {
      constants: { X_OK: 1 },
      mkdirSync() {},
      // The app executable is present, so a re-exec is possible and the button
      // is offered; nothing else on disk is. With `appExecMissing` it is absent
      // too, which is the machine where the button is withheld and the message
      // falls back to the explicit-port route.
      accessSync(target) {
        if (target === APP_EXEC_PATH && !appExecMissing) return;
        const error = new Error("not found");
        error.code = "ENOENT";
        throw error;
      },
      existsSync() { return false; },
      openSync() { return 41; },
      closeSync() {},
      readFileSync() { throw new Error("no log"); },
    },
  });

  return { ...built, documents, mainWindow, state };
}

test("a gateway is not started on a port a configured crew is on", async () => {
  // Selection TARGETS a crew's port on purpose, so a live tunnel there is found
  // and adopted. Reaching the spawn means nothing answered, and binding it now is
  // the shadowing the whole path exists to prevent: the conflict resolver reads
  // the same remoteHosts entry and would call the gateway this app just started
  // that crew. PORT is fixed for this process, so refusing is the only answer
  // available here -- moving is the successor's job.
  const { supervisor, documents, mainWindow, spawnCalls, logs } = clientOnlyClickHarness({
    readyAnswers: false,
    actions: ["quit"],
    store: fakeStore({
      runLocalGateway: true,
      remoteHosts: { 5476: { host: "crew.example.com", remotePort: "7777" } },
    }),
    port: 5476,
  });

  assert.strictEqual(await supervisor.start(), false, "the launch does not report a gateway");
  assert.strictEqual(spawnCalls.length, 0, "and nothing was bound to the crew's port");
  // Whole-line equality, not "the host appears somewhere in the line". A
  // containment test on a host-shaped literal is satisfied by any string that
  // merely carries it -- `evil.test/crew.example.com` passes one -- so writing a
  // test that way declares that much strength sufficient. The host's POSITION in
  // the sentence is part of what is being pinned, so the whole sentence is the
  // assertion. Ownership itself is never decided this way: the gate reads
  // getRemoteHostConfig(store, PORT), an exact port-keyed lookup.
  assert.ok(
    logs.some((line) => line === "not starting a gateway on :5476: the crew "
      + "crew.example.com is configured there and a gateway bound here would shadow it"),
    "the refusal names the port and the crew that holds it in configuration",
  );

  await supervisor.connect(mainWindow);
  for (let i = 0; i < 80 && documents.length < 1; i += 1) await flush();
  assert.strictEqual(documents.length, 1, "the failure dialog opens");
  const shown = documents[0];
  // End to end: the record carries localStartBlocked, the classifier reads that
  // as a launch that started nothing here, and the copy names the button the
  // dialog therefore renders. A record that classified as a crash instead would
  // print this remedy under a log pane and withhold the button it names.
  assert.match(shown, /reserved for reaching crew\.example\.com/, "the message explains why nothing was started");
  // The BUTTON, not the sentence naming it: the message names Start Local Gateway
  // either way, so only the rendered control proves the dialog agrees with the
  // record. A launch that classified as a crash would print this remedy and
  // withhold the control, under a log pane for a state that did not crash.
  assert.match(shown, /onclick="act\('enable-retry'\)"/, "the button it names is rendered");
  assert.doesNotMatch(shown, /onclick="act\('reveal'\)"/, "and no crash log pane appears");
  // Retry names the crew here even though the accent has not moved: one control
  // must not read as two across openings of the same dialog.
  assert.match(
    shown,
    /onclick="act\('retry'\)">Retry crew\.example\.com</,
    "the retry slot names the crew it reaches",
  );
  assert.doesNotMatch(
    shown,
    /set not to start a gateway/,
    "the setting is on, so that sentence must not appear",
  );
});

// Something IS answering on the port, and its payload carries no Kiro Crew
// identity -- the shape decideGatewayAction reuses by design. Whether that
// responder is a legacy gateway or an unrelated service is exactly what the
// LISTEN owner decides, so these two tests differ only in the owner probe.
function unidentifiedResponder() {
  return {
    get(url, _options, callback) {
      const request = new EventEmitter();
      request.destroy = () => {};
      queueMicrotask(() => {
        const response = new EventEmitter();
        response.statusCode = 200;
        response.resume = () => {};
        if (typeof callback === "function") callback(response);
        response.emit("data", JSON.stringify({ ok: true }));
        response.emit("end");
      });
      return request;
    },
  };
}

test("a port held by something that is neither our gateway nor a crew is refused, not adopted", async () => {
  // decideGatewayAction reuses any responder it cannot identify, which is the
  // fail-open that keeps a gateway too old to carry identity fields adoptable.
  // Telling that legacy gateway from an unrelated service takes the two facts
  // this scope has and that function does not: the LISTEN owner, and whether a
  // crew is configured here. Neither says ours, so adopting would point this
  // app's gateway calls -- and the internal secret the heartbeat sends -- at
  // somebody else's service.
  const { supervisor, documents, mainWindow, spawnCalls, logs } = clientOnlyClickHarness({
    actions: ["quit"],
    store: fakeStore({ runLocalGateway: true, remoteHosts: {} }),
    port: 5476,
    httpMod: unidentifiedResponder(),
    execFileFn: ownerProbe("some-other-server --port 5476"),
  });

  assert.strictEqual(await supervisor.start(), false, "the holder is not reported as our gateway");
  assert.strictEqual(spawnCalls.length, 0, "and the port is not ours to bind either");
  // Whole-line equality here too, for the reason above: the sibling assertion is
  // the same shape and a strictness that holds at only one of two sites is not a
  // convention. What decided this refusal is the LISTEN owner -- a tokenized
  // command line matched against a set of executable names -- and the absence of
  // a port-keyed crew entry. Neither is a substring test.
  assert.ok(
    logs.some((line) => line === ":5476 is served by a process this app did not "
      + "start and no remote crew is configured there \u2014 refusing to adopt it"),
    "the refusal names what it found rather than timing out",
  );

  await supervisor.connect(mainWindow);
  for (let i = 0; i < 80 && documents.length < 1; i += 1) await flush();
  assert.strictEqual(documents.length, 1, "the failure dialog opens");
  const shown = documents[0];
  assert.match(shown, /served by a program this app did not start/);
  assert.match(shown, /choose Add Remote Crew and fill in its address/, "saving the crew is the first remedy");
  assert.match(shown, /If it is unrelated software, quit it and then retry/);
  // The title says what is true of this port. Left on the generic line it read
  // "no gateway on port N" above a body saying that port IS served.
  assert.match(shown, /<div class="title">[^<]*port 5476 is in use by another program/,
    "the title agrees with the body");
  // And the button is withheld: this message names freeing the port or saving the
  // crew, never this control, and pressing it re-enters the same refusal.
  assert.doesNotMatch(shown, /act\('enable-retry'\)/,
    "a control that returns the user to this same dialog is not offered");
  assert.doesNotMatch(
    shown,
    /free port/,
    "no crew is configured here, so nothing re-execs onto another port",
  );
  // Nothing was launched, so this is not a crash and must not be dressed as one:
  // a log pane here shows an earlier run's log under a state that never spawned.
  assert.doesNotMatch(shown, /onclick="act\('reveal'\)"/, "no crash log pane");
});

test("the same unidentified payload IS adopted when our own gateway holds the port", async () => {
  // The control for the refusal above: identical payload, identical probe
  // mechanism, and the one fact that differs is who holds the port. A legacy
  // local gateway stays adoptable, which is the fail-open the refusal narrows
  // rather than removes.
  const { supervisor, spawnCalls } = clientOnlyClickHarness({
    actions: ["quit"],
    store: fakeStore({ runLocalGateway: true, remoteHosts: {} }),
    port: 5476,
    httpMod: unidentifiedResponder(),
    execFileFn: ownerProbe(OWN_GATEWAY_COMMAND),
  });

  assert.strictEqual(await supervisor.start(), true, "the existing gateway is adopted");
  assert.strictEqual(spawnCalls.length, 0, "nothing is spawned over it");
});

test("the handoff waits for the successor's own platform budget, not one flat number", async () => {
  // A primary local gateway on Windows gets 120s to bind, because importing a
  // freshly installed bundled Python tree exceeds the ordinary deadline there.
  // The successor IS that, so a flat 60s here expired while the successor was
  // still inside its own budget, and the deadline's fail() killed a gateway that
  // was starting normally. The deadline is derived from the same function the
  // successor applies to itself, plus one Electron-boot margin -- which is why
  // the non-Windows value is unchanged.
  for (const [platform, expected] of [["darwin", 60_000], ["win32", 150_000]]) {
    const timers = fakeTimers();
    const built = clientOnlyClickHarness({
      readyAnswers: false,
      actions: ["enable-retry", "quit"],
      platform,
      timers,
    });

    assert.strictEqual(await built.supervisor.start(), false, platform);
    await built.supervisor.connect(built.mainWindow);
    for (let i = 0; i < 80 && built.spawnCalls.length === 0; i += 1) await flush();
    assert.strictEqual(built.spawnCalls.length, 1, `${platform}: the click spawns one successor`);
    built.spawnCalls[0].child.emit("spawn");
    await flush();

    const armed = timers.pending.filter((t) => t.ms !== SUCCESSOR_POLL_MS).map((t) => t.ms);
    assert.deepStrictEqual(armed, [expected], `${platform}: the readiness deadline`);
  }
});

test("a probe that cannot run reports its own outcome, not a successor that never served", async () => {
  // The successor answered readiness and was then stopped by this app, so
  // reporting "the restarted app never served one" would deny what the user
  // watched -- and offering the same button would repeat a check that cannot
  // succeed on this host. The state has its own flag and its own message, and the
  // message names the route that does recover.
  const timers = fakeTimers();
  // The pre-spawn probe must find the port free, or the click refuses before it
  // spawns anything (that is the occupied-port state). Readiness therefore starts
  // silent and answers only once the successor is running -- which is the real
  // sequence, and the one where "who holds this port" becomes the open question.
  const ready = { answers: false };
  const built = clientOnlyClickHarness({
    actions: ["enable-retry", "quit"],
    timers,
    httpMod: {
      get(url, _options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        const isReady = String(url).includes("/api/ready");
        queueMicrotask(() => {
          if (isReady && ready.answers) {
            const response = new EventEmitter();
            response.statusCode = 200;
            response.resume = () => {};
            if (typeof callback === "function") callback(response);
            response.emit("data", JSON.stringify({ ready: true }));
            response.emit("end");
            return;
          }
          request.emit("error", new Error("connection refused"));
        });
        return request;
      },
    },
    execFileFn: (_file, _args, _options, callback) => {
      const error = new Error("lsof unavailable");
      error.code = "ENOENT";
      callback(error);
    },
  });

  assert.strictEqual(await built.supervisor.start(), false);
  await built.supervisor.connect(built.mainWindow);
  for (let i = 0; i < 80 && built.spawnCalls.length === 0; i += 1) await flush();
  assert.strictEqual(built.spawnCalls.length, 1, "the click spawns one successor");
  built.spawnCalls[0].child.emit("spawn");
  await flush();
  ready.answers = true;
  timers.fire(SUCCESSOR_POLL_MS);
  for (let i = 0; i < 200 && built.documents.length < 2; i += 1) await flush();

  assert.strictEqual(built.documents.length, 2, "the dialog reopens after the refusal");
  const reopened = built.documents[1];
  assert.match(reopened, /could not check which program holds that port/,
    "the message states what actually failed");
  assert.doesNotMatch(reopened, /never served one/,
    "it must not deny a successor that answered");
  assert.match(reopened, /KIROCREW_PORT set to a port number that has no remote host/,
    "and it names the route that actually starts a gateway here");
  assert.doesNotMatch(reopened, /open it again instead/,
    "a plain reopen lands on the crew-configured refusal and starts nothing");
  // The accent follows the sentence, and this sentence asks for Quit. An orange
  // Retry under text saying to quit is a contradiction a reader cannot resolve --
  // they reported not daring to click the highlighted control.
  assert.match(reopened, /class="ok" onclick="act\('quit'\)"/,
    "Quit carries the accent in this state");
  assert.doesNotMatch(reopened, /class="ok" onclick="act\('retry'\)"/,
    "so Retry must not");
  assert.doesNotMatch(reopened, /class="ok" onclick="act\('enable-retry'\)"/,
    "and neither must the button the text says reaches the same check");
  // The title is what a reader takes in first, so it names the local outcome
  // rather than repeating the crew line every other state also carries.
  assert.match(reopened, /<div class="title">[^<]*could not identify what is on the port/,
    "the title names this outcome");
  // Two stories, two paragraphs: the local start, then what to do about the crew.
  assert.ok(
    (reopened.match(/<div class="msg">[\s\S]*?<\/div>/) || [""])[0].split("<p>").length > 2,
    "the message renders as more than one paragraph",
  );
  assert.deepStrictEqual(built.state.exits, [],
    "this instance stays, since it could not identify the holder");
});

test("a later busy-port refusal retires the unreadable-probe outcome", async () => {
  // Both readers test that flag first, so a leftover keeps reporting a successor
  // this click never spawned, never names the port to free, and keeps the accent
  // on Quit under a paragraph asking for a port to be freed. The three post-click
  // outcomes each retire the other two; this is the pairing the first version
  // missed.
  const timers = fakeTimers();
  const ready = { answers: false };
  const built = clientOnlyClickHarness({
    actions: ["enable-retry", "enable-retry", "quit"],
    timers,
    httpMod: {
      get(url, _options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        const isReady = String(url).includes("/api/ready");
        queueMicrotask(() => {
          if (isReady && ready.answers) {
            const response = new EventEmitter();
            response.statusCode = 200;
            response.resume = () => {};
            if (typeof callback === "function") callback(response);
            response.emit("data", JSON.stringify({ ready: true }));
            response.emit("end");
            return;
          }
          request.emit("error", new Error("connection refused"));
        });
        return request;
      },
    },
    execFileFn: (_file, _args, _options, callback) => {
      const error = new Error("lsof unavailable");
      error.code = "ENOENT";
      callback(error);
    },
  });

  assert.strictEqual(await built.supervisor.start(), false);
  await built.supervisor.connect(built.mainWindow);
  // First click: the successor answers, the probe cannot run, so this is the
  // unreadable-probe outcome.
  for (let i = 0; i < 80 && built.spawnCalls.length === 0; i += 1) await flush();
  built.spawnCalls[0].child.emit("spawn");
  await flush();
  ready.answers = true;
  timers.fire(SUCCESSOR_POLL_MS);
  for (let i = 0; i < 200 && built.documents.length < 2; i += 1) await flush();
  assert.match(built.documents[1], /could not check which program holds that port/);

  // Second click: readiness already answers, so the pre-spawn probe finds the
  // port occupied and nothing is spawned -- the busy-port outcome, which is now
  // the newer fact.
  for (let i = 0; i < 260 && built.documents.length < 3; i += 1) await flush();
  assert.strictEqual(built.documents.length, 3, "the dialog reopens after the second click");
  const second = built.documents[2];
  assert.match(second, /did not begin/, "the newer outcome is the occupied port");
  assert.doesNotMatch(second, /could not check which program holds that port/,
    "the earlier unreadable-probe story must not be reprinted");
  assert.match(second, /<div class="title">[^<]*already in use/, "and the title moves with it");
  assert.doesNotMatch(second, /class="ok" onclick="act\('quit'\)"/,
    "the Quit accent belonged to the other state and goes with it");
});

test("a Retry after an occupied-port refusal still names the port", async () => {
  // Retry discards the failure record and startGateway builds a fresh one, so a
  // busy port written only onto the old object would vanish -- and the rebuilt
  // message would say this app is set not to start a gateway, which the click has
  // already made false. The port is supervisor state for that reason.
  const { supervisor, documents, mainWindow } = clientOnlyClickHarness({
    readyAnswers: true,
    actions: ["enable-retry", "retry", "quit"],
  });

  assert.strictEqual(await supervisor.start(), false);
  await supervisor.connect(mainWindow);
  for (let i = 0; i < 80 && documents.length < 3; i += 1) await flush();

  assert.strictEqual(documents.length, 3, "the dialog reopens after the refusal and again after Retry");
  const rebuilt = documents[2];
  assert.match(rebuilt, /did not begin/, "the rebuilt record still reports the refusal");
  assert.match(rebuilt, /port 5476/, "and still names the occupied port");
  // Two post-click states shared a title and a button set and differed by one
  // word in the body, so a reader could not tell which situation they were in.
  // The title now carries the outcome.
  assert.match(rebuilt, /<div class="title">[^<]*port 5476 is already in use/,
    "the title names this outcome rather than repeating the crew line");
  assert.doesNotMatch(
    rebuilt,
    /set not to start a gateway/,
    "the setting is on by now, so that sentence must not come back",
  );
});

test("a lost restart promotes Start Local Gateway too, because its own copy asks for it", async () => {
  // The successor here is a fake EventEmitter, so ending it is what reaches the
  // state a lost restart leaves behind -- no real readiness deadline has to
  // elapse for the dialog to be driven to it.
  const { supervisor, documents, mainWindow, spawnCalls } = clientOnlyClickHarness({
    readyAnswers: false,
    actions: ["enable-retry", "quit"],
  });

  assert.strictEqual(await supervisor.start(), false);
  await supervisor.connect(mainWindow);
  for (let i = 0; i < 60 && spawnCalls.length === 0; i += 1) await flush();
  assert.strictEqual(spawnCalls.length, 1, "the click spawns one successor");
  const { child } = spawnCalls[0];
  child.exitCode = 1;
  child.emit("exit", 1, null);
  for (let i = 0; i < 200 && documents.length < 2; i += 1) await flush();

  assert.strictEqual(documents.length, 2, "the dialog reopens after the lost restart");
  const reopened = documents[1];
  assert.match(reopened, /did not finish/, "this is the lost-restart state");
  // This state's own paragraph ends "choose Start Local Gateway to try again", so
  // the accent has to sit there. Gated on the busy port alone it stayed on Retry,
  // which reaches only the crew the same message has just said is unreachable --
  // so the highlighted control was the one that cannot help.
  assert.match(reopened, /class="ok" onclick="act\('enable-retry'\)"/,
    "Start Local Gateway carries the accent after a lost restart");
  assert.doesNotMatch(reopened, /class="ok" onclick="act\('retry'\)"/,
    "Retry must not keep the accent here either");
  assert.strictEqual(
    (reopened.match(/<button[^>]*onclick="act\('enable-retry'\)"/g) || []).length,
    1,
    "promoted, it must not also render as a secondary",
  );
  // Same set and same order as every other opening of this dialog, so only the
  // accent differs. Built primary-first, promotion moved Start Local Gateway to
  // the leftmost slot and Retry to the third, which is a habit-press hazard
  // between two openings that carry the same title.
  const order = (doc) => (doc.match(/onclick="act\('([a-z-]+)'\)"/g) || [])
    .map((m) => m.replace(/.*act\('([a-z-]+)'\).*/, "$1"));
  assert.deepStrictEqual(
    order(reopened),
    order(documents[0]),
    "the promoted row keeps the unpromoted row's action order",
  );
  assert.match(
    reopened,
    /onclick="act\('retry'\)">Retry crew\.example\.com</,
    "the demoted Retry names the crew it reaches",
  );
});

test("clicking Start Local Gateway onto an occupied port names the port and keeps the button", async () => {
  // predictLocalPort answers 5476 and something is already serving there. Nothing
  // is spawned and nothing is torn down, so the user must not be told a restart
  // ran, and the button must not be spent: freeing that port is outside this app
  // and makes the same click work.
  const { supervisor, documents, mainWindow, state, spawnCalls } = clientOnlyClickHarness({
    readyAnswers: true,
    actions: ["enable-retry", "quit"],
  });

  assert.strictEqual(await supervisor.start(), false);
  await supervisor.connect(mainWindow);
  // The reopen is dispatched from the refusal callback rather than awaited by
  // connect(), so wait for the document itself. Bounded, so a reopen that never
  // happens fails the assertion below instead of hanging.
  for (let i = 0; i < 50 && documents.length < 2; i += 1) await flush();

  assert.strictEqual(documents.length, 2, "the dialog reopens after the refused handoff");
  const [first, second] = documents;
  assert.match(first, /Start Local Gateway/, "the first dialog offers the button");

  assert.match(second, /did not begin/, "the reopened dialog says nothing restarted");
  assert.match(second, /port 5476/, "it names the port that is occupied");
  assert.doesNotMatch(second, /port 7778 is already served/,
    "the occupied port is the successor's, not this process's own");
  assert.doesNotMatch(second, /did not finish/,
    "it must not report a restart that never happened");
  assert.match(second, /enable-retry/,
    "the button survives, because the occupied port is not this app's to fix");
  // The message asks for Start Local Gateway, and Retry reaches only the crew
  // that is already unreachable -- so the accent has to sit on the action the
  // sentence names, or the colouring points the eye at the one that cannot work.
  assert.match(second, /class="ok" onclick="act\('enable-retry'\)"/,
    "Start Local Gateway is the primary action in this state");
  assert.doesNotMatch(second, /class="ok" onclick="act\('retry'\)"/,
    "Retry must not keep the accent here");
  // Promoted to primary, it must not also render as a secondary: one control. The
  // count is over BUTTONS, since the Enter key binding names the same action.
  assert.strictEqual(
    (second.match(/<button[^>]*onclick="act\('enable-retry'\)"/g) || []).length,
    1,
    "Start Local Gateway appears as exactly one button",
  );
  // Moving the accent must not remove the control: this same message still tells
  // the user to repair the tunnel and "then retry to reach ... again", so a
  // window without a Retry button leaves that sentence naming nothing, and a
  // user who has just fixed the tunnel can only quit.
  //
  // Demoted, it is the one button whose target is ambiguous: the sentence above
  // it is about a local start that just refused, so a bare "Retry" reads as
  // retrying THAT. The label names the crew instead.
  assert.match(
    second,
    /<button class="cancel" onclick="act\('retry'\)">Retry crew\.example\.com<\/button>/,
    "Retry stays available as a secondary and names the crew it reaches",
  );
  // The ORDER must be the order the unpromoted dialog uses, not primary-first.
  // Built primary-first, promotion moved Start Local Gateway from the third slot
  // to the first and pushed Retry the other way, so the leftmost button did two
  // different things between two openings of a dialog with the same title and a
  // habit press landed on the wrong recovery. Each action owns a slot; only the
  // accent travels. Compared against the FIRST dialog, which is the unpromoted
  // rendering of the same row -- asserting a literal order here would pass just
  // as well if both renderings moved together.
  const slots = (doc) => (doc.match(/onclick="act\('([a-z-]+)'\)"/g) || [])
    .map((m) => m.replace(/.*act\('([a-z-]+)'\).*/, "$1"));
  assert.deepStrictEqual(
    slots(second),
    slots(first),
    "the promoted row renders the same actions in the same order as the unpromoted one",
  );
  assert.ok(
    slots(second).indexOf("retry") < slots(second).indexOf("enable-retry"),
    "and the retry slot stays ahead of the local-start slot in both",
  );
  assert.strictEqual(
    (second.match(/<button[^>]*onclick="act\('retry'\)"/g) || []).length,
    1,
    "Retry appears as exactly one button",
  );

  assert.deepStrictEqual(state.exits, [], "this instance keeps running");
  assert.strictEqual(state.lockReleases, 0, "nothing was torn down");
  assert.strictEqual(successorCall(spawnCalls), undefined, "no successor is exec'd");
});

// ---------------------------------------------------------------------------
// "Installation still finishing" dialog: auto-retry while the bundle lands.
// ---------------------------------------------------------------------------

const BUNDLE_ROOT = "/virtual/resources/backend-dist/kirocrew-backend-x64";
const BUNDLE_BIN = `${BUNDLE_ROOT}/bin/kirocrew`;
const BUNDLE_LIB = `${BUNDLE_ROOT}/lib/python3.12`;
const { REQUIRED_STDLIB_PARTS, SPAWN_MARKER } = require(path.join(__dirname, "..", "bundle-integrity.js"));

// A bundled backend tree the test extracts piece by piece: `files` is the set of
// paths on disk right now. Only the supervisor's own fs calls are modelled.
function extractingBundleFs(files, { launchLog = null } = {}) {
  const dirs = () => ({
    [`${BUNDLE_ROOT}/bin`]: ["python3", "kirocrew"],
    [`${BUNDLE_ROOT}/lib`]: ["python3.12"],
  });
  return {
    files,
    constants: { X_OK: 1 },
    mkdirSync() {},
    accessSync(file) {
      if (files.has(file)) return;
      const error = new Error("not found");
      error.code = "ENOENT";
      throw error;
    },
    existsSync(file) { return files.has(file); },
    readdirSync(dir) {
      const listing = dirs()[dir];
      if (!listing) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
      return listing;
    },
    openSync() { return 41; },
    closeSync() {},
    readFileSync(file) {
      if (launchLog !== null && file === "/virtual/logs/gateway-launch.log") return launchLog;
      throw new Error("no launch log yet");
    },
  };
}

function bundleFiles({ missing = [] } = {}) {
  const files = new Set([
    BUNDLE_BIN,
    `${BUNDLE_ROOT}/bin`,
    `${BUNDLE_ROOT}/lib`,
    BUNDLE_LIB,
  ]);
  for (const part of REQUIRED_STDLIB_PARTS) {
    if (missing.includes(part)) continue;
    files.add(`${BUNDLE_LIB}/${part}/__init__.py`);
  }
  return files;
}

// Boot straight into the installing dialog: the pre-spawn refusal sets the
// failure record, connect() reads it and opens the dialog. The parent window
// reports destroyed once a gateway child has been spawned, which ends the
// post-retry boot before it starts polling a backend this harness has not got.
async function installingDialogHarness({ missing }) {
  const files = bundleFiles({ missing });
  const fsMod = extractingBundleFs(files);
  const timers = fakeTimers();
  const { windows, FakeBrowserWindow } = fakeDialogWindows();
  const statuses = [];
  let spawnCalls = [];
  const window = {
    // Destroyed once a retry has spawned: that ends the post-retry boot before
    // it polls a backend this harness has not got.
    isDestroyed: () => spawnCalls.length > 0,
    show() {},
    focus() {},
    isMinimized: () => false,
    restore() {},
    webContents: {
      isDestroyed: () => false,
      loadFile() {},
      send: (channel, message) => statuses.push(`${channel}:${message}`),
    },
  };
  let quittingNow = false;
  const built = harness({
    fsMod,
    timers,
    BrowserWindow: FakeBrowserWindow,
    mainWindow: window,
    app: { show() {} },
    isQuitting: () => quittingNow,
  });
  built.setQuitting = (value) => { quittingNow = value; };
  ({ spawnCalls } = built);
  assert.strictEqual(await built.supervisor.start(), false, "the incomplete bundle is refused before spawn");
  assert.ok(built.errors.some((line) => line.includes("spawn REFUSED: incomplete bundle")));
  const connected = built.supervisor.connect(window);
  await flush();
  assert.strictEqual(windows.length, 1, "the failure dialog opened");
  return { ...built, files, timers, windows, dialog: windows[0], connected, statuses };
}

test("installing dialog re-probes the bundle and repaints the falling count", async () => {
  const { dialog, timers, files, spawnCalls } = await installingDialogHarness({
    missing: ["urllib", "zipfile", "zoneinfo"],
  });
  assert.match(decodeURIComponent(dialog.loaded[0]), /installation still finishing/);
  assert.match(decodeURIComponent(dialog.loaded[0]), /3 components are/);
  assert.strictEqual(timers.intervals.length, 1, "one probe interval is armed");
  assert.strictEqual(timers.intervals[0].ms, INSTALLING_PROBE_MS);

  // Nothing changed on disk: no repaint, no retry.
  timers.tick(INSTALLING_PROBE_MS);
  assert.deepStrictEqual(dialog.paintedMessages(), []);
  assert.strictEqual(dialog.destroyed, false);

  // Two more parts land: the count moves, the dialog stays.
  files.add(`${BUNDLE_LIB}/urllib/__init__.py`);
  files.add(`${BUNDLE_LIB}/zipfile/__init__.py`);
  timers.tick(INSTALLING_PROBE_MS);
  assert.match(dialog.paintedMessages().at(-1), /1 component is/);
  assert.strictEqual(dialog.completionChrome(), "", "title and buttons untouched while parts remain");
  assert.strictEqual(dialog.destroyed, false);
  assert.strictEqual(spawnCalls.length, 0, "no retry while a part is still missing");
  assert.strictEqual(timers.intervals.length, 1, "the probe keeps running");
});

// Reveal Log resolves the dialog and the caller's loop reopens it with the
// message it computed at refusal time. The reopened dialog must show the
// CURRENT count, or the number the user watched fall climbs back up.
test("a reopened installing dialog shows the current count, not the refusal-time one", async () => {
  const { dialog, timers, files, windows, spawnCalls } = await installingDialogHarness({
    missing: ["urllib", "zipfile", "zoneinfo"],
  });
  assert.match(decodeURIComponent(dialog.loaded[0]), /3 components are/);
  files.add(`${BUNDLE_LIB}/urllib/__init__.py`);
  files.add(`${BUNDLE_LIB}/zipfile/__init__.py`);
  timers.tick(INSTALLING_PROBE_MS);
  assert.match(dialog.paintedMessages().at(-1), /1 component is/);
  dialog.click("reveal");
  await flush();
  assert.strictEqual(windows.length, 2, "Reveal Log reopened the dialog");
  const reopened = decodeURIComponent(windows[1].loaded[0]);
  assert.match(reopened, /1 component is/, "the reopened dialog carries the current count");
  assert.ok(!/3 components are/.test(reopened), "the refusal-time count must not come back");
  assert.strictEqual(timers.intervals.length, 1, "exactly one probe is armed for the reopened dialog");
  assert.strictEqual(spawnCalls.length, 0);
  windows[1].click("quit");
});

test("installing dialog retries exactly once when the probe reports complete", async () => {
  const { dialog, timers, files, spawnCalls, connected, logs } = await installingDialogHarness({
    missing: ["zoneinfo"],
  });
  files.add(`${BUNDLE_LIB}/zoneinfo/__init__.py`);
  timers.tick(INSTALLING_PROBE_MS);

  // The completion line gets a moment on screen before the window goes, and the
  // whole frame agrees with it: title swapped, Retry greyed, Quit restated.
  assert.match(dialog.paintedMessages().at(-1), /Installation finished/);
  const chrome = dialog.completionChrome();
  assert.match(chrome, /"Kiro Crew — installation finished"/, "the title no longer says finishing");
  assert.match(chrome, /act\('retry'\)"\) \{ b\.disabled = true; b\.textContent = 'Starting…'; \}/,
    "Retry is disabled AND relabelled, so a paler button is not the only cue");
  assert.match(chrome, /Quit anyway/, "Quit names its changed stakes");
  // Enter must go through the disabled button, never straight to act('retry'):
  // an update install dispatched during the linger disables Retry for the
  // mouse, and the keyboard has to honour the same guard.
  const html = decodeURIComponent(dialog.loaded[0]);
  assert.match(html, /var primaryAct = "act\('retry'\)";/, "Enter's primary is Retry here");
  assert.match(html, /if \(e\.key === 'Enter'\) \{\s*for \(const b of document\.querySelectorAll\('button'\)\) \{\s*if \(b\.getAttribute\('onclick'\) !== primaryAct\) continue;\s*if \(!b\.disabled\) b\.click\(\);/,
    "Enter presses the primary button and respects its disabled state");
  assert.ok(!/if \(e\.key === 'Enter'\) act\(/.test(html), "Enter never calls act() directly");
  assert.strictEqual(dialog.destroyed, false, "the dialog lingers on the completion line");
  assert.strictEqual(timers.intervals.length, 0, "the probe was cleared at completion");
  assert.ok(logs.some((line) => line.includes("bundle complete — retrying")));
  assert.throws(() => timers.tick(INSTALLING_PROBE_MS), /interval is armed/, "no tick can arm a retry now");
  assert.strictEqual(spawnCalls.length, 0, "no retry before the linger ends");
  timers.fire(INSTALLING_COMPLETE_LINGER_MS);
  assert.strictEqual(dialog.destroyed, true, "the dialog closed itself after the linger");

  await connected;
  assert.strictEqual(spawnCalls.length, 1, "the retry re-entered startGateway once and spawned");
  assert.strictEqual(spawnCalls[0][0], BUNDLE_BIN);
  // A second interval tick after close must be impossible: nothing is armed.
  assert.throws(() => timers.tick(INSTALLING_PROBE_MS), /interval is armed/);
});

test("a user click stops the probe so a later tick cannot overrule the choice", async () => {
  const { dialog, timers, files, spawnCalls, connected } = await installingDialogHarness({
    missing: ["zoneinfo"],
  });
  assert.strictEqual(timers.intervals.length, 1, "the probe is armed while the dialog is up");
  files.add(`${BUNDLE_LIB}/zoneinfo/__init__.py`);
  // The user quits in the instant the bundle completes. In Electron the title
  // update and the `closed` event are separate turns, so a probe tick can land
  // between them; it must find the probe already gone.
  dialog.emit("page-title-updated", {}, "mc-action:quit");
  assert.strictEqual(timers.intervals.length, 0, "the click cleared the probe");
  assert.throws(() => timers.tick(INSTALLING_PROBE_MS), /interval is armed/);
  dialog.close();
  await connected;
  assert.strictEqual(spawnCalls.length, 0, "quit was honoured; no retry spawned");
  assert.deepStrictEqual(dialog.paintedMessages(), [], "nothing was repainted after the choice");
});

// The retry must be decided when the linger ends, not when the probe saw the
// bundle complete: an update install dispatched in between stops the gateway
// on purpose to swap the bundle, and a retry then would spawn into the swap.
test("an update install dispatched during the linger cancels the pending retry", async () => {
  const { supervisor, dialog, timers, files, spawnCalls, logs } = await installingDialogHarness({
    missing: ["zoneinfo"],
  });
  files.add(`${BUNDLE_LIB}/zoneinfo/__init__.py`);
  timers.tick(INSTALLING_PROBE_MS);
  assert.match(dialog.paintedMessages().at(-1), /Installation finished/);
  supervisor.onInstallDispatched();
  timers.fire(INSTALLING_COMPLETE_LINGER_MS);
  assert.strictEqual(dialog.destroyed, false, "the dialog stays for the user");
  assert.strictEqual(spawnCalls.length, 0, "no retry spawned into the bundle swap");
  assert.ok(logs.some((line) => line.includes("leaving the dialog to the user")));
  // The user can still act; their choice resolves the dialog as always.
  dialog.click("quit");
  assert.strictEqual(dialog.destroyed, true);
});

test("a quit that began during the linger cancels the pending retry", async () => {
  const built = await installingDialogHarness({ missing: ["zoneinfo"] });
  const { dialog, timers, files, spawnCalls } = built;
  files.add(`${BUNDLE_LIB}/zoneinfo/__init__.py`);
  timers.tick(INSTALLING_PROBE_MS);
  built.setQuitting(true);
  timers.fire(INSTALLING_COMPLETE_LINGER_MS);
  assert.strictEqual(dialog.destroyed, false, "no auto-close while the app is quitting");
  assert.strictEqual(spawnCalls.length, 0, "no retry spawned during quit");
});

// The click's title update and its close are separate turns in Electron. A
// linger timer firing in between sees a live window and must still honour the
// action the user already chose.
test("a user action recorded during the linger is not overwritten by the auto-retry", async () => {
  const { dialog, timers, files, spawnCalls, connected } = await installingDialogHarness({
    missing: ["zoneinfo"],
  });
  files.add(`${BUNDLE_LIB}/zoneinfo/__init__.py`);
  timers.tick(INSTALLING_PROBE_MS);
  dialog.emit("page-title-updated", {}, "mc-action:quit");
  timers.fire(INSTALLING_COMPLETE_LINGER_MS);
  assert.strictEqual(dialog.destroyed, false, "the auto-close stood down; the click's own close is in flight");
  dialog.close();
  await connected;
  assert.strictEqual(spawnCalls.length, 0, "quit was honoured, not overwritten by retry");
});

test("a click during the completion linger wins over the pending auto-close", async () => {
  const { dialog, timers, files, spawnCalls, connected } = await installingDialogHarness({
    missing: ["zoneinfo"],
  });
  files.add(`${BUNDLE_LIB}/zoneinfo/__init__.py`);
  timers.tick(INSTALLING_PROBE_MS);
  assert.strictEqual(dialog.destroyed, false);
  dialog.click("quit");
  assert.strictEqual(dialog.destroyed, true);
  // The linger timer still fires; it must find nothing left to close.
  timers.fire(INSTALLING_COMPLETE_LINGER_MS);
  await connected;
  assert.strictEqual(spawnCalls.length, 0, "the user's quit was honoured over the auto-retry");
});

// The crash matcher accepts stdlib names and dotted submodules the probe never
// inspects (a package's sibling file, a module outside REQUIRED_STDLIB_PARTS).
// A dialog reached that way must not auto-retry: with a permanently truncated
// bundle the probe would report complete on every tick and the gateway would
// respawn and crash forever. It keeps the manual dialog and the manual copy.
test("a crash reclassified as installing gets the manual dialog, no probe", async () => {
  const files = bundleFiles();
  const launchLog = `${SPAWN_MARKER}
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
ModuleNotFoundError: No module named 'ssl'
`;
  const fsMod = extractingBundleFs(files, { launchLog });
  const timers = fakeTimers();
  const { windows, FakeBrowserWindow } = fakeDialogWindows();
  let spawnCalls = [];
  const window = {
    // The first spawn is the crashing one; destroyed only after a retry spawned.
    isDestroyed: () => spawnCalls.length > 1,
    show() {},
    focus() {},
    isMinimized: () => false,
    restore() {},
    webContents: { isDestroyed: () => false, loadFile() {}, send() {} },
  };
  const built = harness({
    fsMod,
    timers,
    BrowserWindow: FakeBrowserWindow,
    mainWindow: window,
    app: { show() {} },
  });
  ({ spawnCalls } = built);
  assert.strictEqual(await built.supervisor.start(), true, "the complete-looking bundle spawns");
  assert.strictEqual(spawnCalls.length, 1);
  spawnCalls[0].child.exitCode = 1;
  spawnCalls[0].child.emit("exit", 1, null);
  const connected = built.supervisor.connect(window);
  await flush();
  assert.strictEqual(windows.length, 1, "the failure dialog opened");
  const html = decodeURIComponent(windows[0].loaded[0]);
  assert.match(html, /installation still finishing/, "the crash was reclassified as installing");
  assert.match(html, /Wait, then retry/, "manual copy: no automatic start is promised");
  assert.ok(!/starts on its own/i.test(html), "auto-retry copy must not appear here");
  assert.strictEqual(timers.intervals.length, 0, "no probe is armed for a reclassified crash");
  windows[0].click("quit");
  await connected;
  assert.strictEqual(spawnCalls.length, 1, "no automatic respawn");
});

test("installing dialog stops probing once an update install is dispatched", async () => {
  const { supervisor, dialog, timers, files, spawnCalls } = await installingDialogHarness({
    missing: ["zoneinfo"],
  });
  supervisor.onInstallDispatched();
  files.add(`${BUNDLE_LIB}/zoneinfo/__init__.py`);
  timers.tick(INSTALLING_PROBE_MS);

  assert.strictEqual(timers.intervals.length, 0, "the probe disarmed itself");
  assert.strictEqual(dialog.destroyed, false, "the dialog stays for the user");
  assert.deepStrictEqual(dialog.paintedMessages(), [], "nothing was repainted");
  assert.strictEqual(spawnCalls.length, 0, "no retry while the updater owns the gateway");
});

test("non-installing failure dialogs arm no probe", async () => {
  // Local gateway off: the "no gateway on port" dialog, manual-only as before.
  const timers = fakeTimers();
  const { windows, FakeBrowserWindow } = fakeDialogWindows();
  const window = {
    isDestroyed: () => false,
    show() {},
    focus() {},
    isMinimized: () => false,
    restore() {},
    webContents: { isDestroyed: () => false, loadFile() {}, send() {} },
  };
  const { supervisor } = harness({
    store: fakeStore({ runLocalGateway: false }),
    timers,
    BrowserWindow: FakeBrowserWindow,
    mainWindow: window,
    app: { show() {} },
  });
  assert.strictEqual(await supervisor.start(), false);
  const connected = supervisor.connect(window);
  await flush();
  assert.strictEqual(windows.length, 1);
  assert.match(decodeURIComponent(windows[0].loaded[0]), /no gateway on port/);
  assert.strictEqual(timers.intervals.length, 0, "no probe for a failure that does not self-resolve");
  windows[0].click("quit");
  await connected;
});
