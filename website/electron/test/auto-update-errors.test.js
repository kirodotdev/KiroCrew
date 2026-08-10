// Failure classification + phase-aware error emission (#735, #736).
//
// Why this is its own file: the updater's error path is the one surface with no
// happy-path coverage, and two distinct defects lived here -- a phase-less emit
// that made the UI label download failures as check failures, and raw library
// exception text reaching a settings panel.
const { test } = require("node:test");
const assert = require("node:assert");

const { classifyError, initAutoUpdate } = require("../auto-update");

// --------------------------------------------------------------------------
// classifyError: stable codes, so user-facing copy can live in the renderer
// (and be translated) instead of shipping English from the main process.
// --------------------------------------------------------------------------

test("classifyError: a missing channel file is 'no-release', not a bare HTTP error", () => {
  const err = Object.assign(new Error('Cannot find channel "latest-mac.yml" update info'), {
    code: "ERR_UPDATER_CHANNEL_FILE_NOT_FOUND",
    statusCode: 404,
  });
  // Order matters: the specific signal must win over the generic status code,
  // because "no update published yet" is far more actionable than "HTTP 404".
  assert.strictEqual(classifyError(err).code, "no-release");
});

test("classifyError: a checksum failure is 'integrity'", () => {
  const err = new Error("sha512 checksum mismatch, expected AbC, got XyZ");
  assert.strictEqual(classifyError(err).code, "integrity");
});

test("classifyError: network failures are 'offline'", () => {
  for (const code of ["ENOTFOUND", "ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "EAI_AGAIN"]) {
    const err = Object.assign(new Error("request failed"), { code });
    assert.strictEqual(classifyError(err).code, "offline", `${code} should be offline`);
  }
});

test("classifyError: an HTTP status becomes 'server' and carries the status", () => {
  const err = Object.assign(new Error("Cannot download"), { statusCode: 503 });
  const out = classifyError(err);
  assert.strictEqual(out.code, "server");
  assert.strictEqual(out.httpStatus, 503);
});

test("classifyError: a missing app-update.yml is 'misconfigured'", () => {
  // This is the exact failure the build.publish fix prevents; if it ever
  // regresses, the user should be told to reinstall, not shown an ENOENT path.
  const err = Object.assign(new Error("ENOENT: no such file or directory, open '/a/app-update.yml'"), {
    code: "ENOENT",
  });
  assert.strictEqual(classifyError(err).code, "misconfigured");
});

test("classifyError: detail is the FIRST LINE ONLY and length-capped", () => {
  // electron-updater HttpErrors are multi-line dumps; a settings panel must not
  // render a stack trace.
  const err = new Error(`first line\nsecond line\n${"x".repeat(500)}`);
  const { detail } = classifyError(err);
  assert.strictEqual(detail, "first line");
  const long = classifyError(new Error("y".repeat(500)));
  assert.ok(long.detail.length <= 200, `detail was ${long.detail.length} chars`);
});

test("classifyError: an unrecognised failure is 'unknown' (never throws)", () => {
  assert.strictEqual(classifyError(new Error("weird")).code, "unknown");
  assert.strictEqual(classifyError(undefined).code, "unknown");
  assert.strictEqual(classifyError("a string").code, "unknown");
});

// --------------------------------------------------------------------------
// Phase routing: which stage failed must reach the renderer, or a download
// failure is labelled as a check failure and the update card is unmounted.
// --------------------------------------------------------------------------

function makeDeps({ appVersion = "1.0.0" } = {}) {
  const calls = { setFeedURL: [], checkForUpdates: 0, downloadUpdate: 0, quitAndInstall: [] };
  const handlers = {};
  const states = [];
  const autoUpdater = {
    setFeedURL: (o) => calls.setFeedURL.push(o),
    checkForUpdates: async () => { calls.checkForUpdates += 1; },
    downloadUpdate: async () => { calls.downloadUpdate += 1; },
    quitAndInstall: (...a) => calls.quitAndInstall.push(a),
    on: (ev, fn) => { handlers[ev] = fn; },
  };
  const deps = {
    app: {
      isPackaged: true,
      getVersion: () => appVersion,
      once: () => {},
      removeListener: () => {},
      exit: () => {},
    },
    autoUpdater,
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "stable",
    stopGateway: async () => {},
    osPlatform: "darwin",
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (s) => states.push(s),
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };
  return { deps, calls, states, emit: (ev, p) => handlers[ev] && handlers[ev](p) };
}

test("a check failure is emitted with phase 'check'", async () => {
  const { deps, states } = makeDeps();
  deps.autoUpdater.checkForUpdates = async () => { throw Object.assign(new Error("nope"), { code: "ENOTFOUND" }); };
  const u = initAutoUpdate(deps);
  await u.check();
  const err = states.find((s) => s.state === "error");
  assert.ok(err, "a failure must surface");
  assert.strictEqual(err.phase, "check");
  assert.strictEqual(err.code, "offline");
});

test("a download failure is emitted with phase 'download' AND the pending version", async () => {
  // The version is what lets the UI keep the card on screen for a retry
  // instead of dropping the version the user just consented to.
  const { deps, states, emit } = makeDeps({ appVersion: "1.0.0" });
  deps.autoUpdater.downloadUpdate = async () => { throw Object.assign(new Error("boom"), { statusCode: 502 }); };
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  await u.download();
  const err = states.find((s) => s.state === "error");
  assert.ok(err);
  assert.strictEqual(err.phase, "download");
  assert.strictEqual(err.code, "server");
  assert.strictEqual(err.httpStatus, 502);
  assert.strictEqual(err.version, "1.1.0", "must report the version being downloaded, not the running one");
});

test("the library's error event is attributed to the phase in flight", async () => {
  const { deps, states, emit } = makeDeps();
  const pending = [];
  deps.autoUpdater.downloadUpdate = () => new Promise((r) => pending.push(r));
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  u.download(); // leave the download in flight
  states.length = 0;
  emit("error", Object.assign(new Error("mid-download"), { code: "ECONNRESET" }));
  const err = states.find((s) => s.state === "error");
  assert.ok(err);
  assert.strictEqual(err.phase, "download", "a mid-download failure must NOT be reported as a check failure");
  pending.forEach((r) => r());
});

test("the library's error event outside a download is attributed to 'check'", () => {
  const { deps, states, emit } = makeDeps();
  initAutoUpdate(deps);
  states.length = 0;
  emit("error", new Error("idle failure"));
  const err = states.find((s) => s.state === "error");
  assert.ok(err);
  assert.strictEqual(err.phase, "check");
});

test("emitted failures never carry multi-line library text", async () => {
  const { deps, states } = makeDeps();
  deps.autoUpdater.checkForUpdates = async () => {
    throw new Error("HttpError: 404\n  url: https://example/feed\n  headers: {...}");
  };
  const u = initAutoUpdate(deps);
  await u.check();
  const err = states.find((s) => s.state === "error");
  assert.ok(err);
  assert.ok(!err.message.includes("\n"), `message leaked newlines: ${JSON.stringify(err.message)}`);
});

// ---------------------------------------------------------------------------
// Manual-reinstall escape hatch.
//
// When an update downloads but will not APPLY, the card otherwise re-offers the
// same update forever with no way out. The permalink is resolved in the main
// process because the renderer has no trustworthy platform value.
// ---------------------------------------------------------------------------

const { manualDownloadUrl, DOWNLOAD_BASE } = require("../auto-update");

test("manualDownloadUrl: per-platform artifact on the byte host", () => {
  assert.strictEqual(
    manualDownloadUrl("nightly", "darwin"),
    `${DOWNLOAD_BASE}/desktop/nightly/latest/KiroCrew.dmg`,
  );
  assert.strictEqual(
    manualDownloadUrl("stable", "linux", "x64"),
    `${DOWNLOAD_BASE}/desktop/stable/latest/KiroCrew-x86_64.AppImage`,
  );
  assert.strictEqual(
    manualDownloadUrl("insider", "darwin"),
    `${DOWNLOAD_BASE}/desktop/insider/latest/KiroCrew.dmg`,
  );
});

test("manualDownloadUrl: Linux picks the AppImage for the running arch", () => {
  // The published basenames are publish-linux.yml's contract. Handing an ARM
  // user the x86_64 AppImage produces "cannot execute binary file" -- the exact
  // dead end this link exists to escape.
  assert.strictEqual(
    manualDownloadUrl("stable", "linux", "arm64"),
    `${DOWNLOAD_BASE}/desktop/stable/latest/KiroCrew-aarch64.AppImage`,
  );
  assert.strictEqual(
    manualDownloadUrl("stable", "linux", "x64"),
    `${DOWNLOAD_BASE}/desktop/stable/latest/KiroCrew-x86_64.AppImage`,
  );
  // The mac DMG is universal, so darwin must ignore the arch entirely.
  assert.strictEqual(
    manualDownloadUrl("stable", "darwin", "arm64"),
    manualDownloadUrl("stable", "darwin", "x64"),
  );
});

test("manualDownloadUrl: null wherever there is no publish lane", () => {
  // A dev build has no channel lane, and Windows has none until
  // publish-windows.yml lands -- offering a 404 is worse than offering nothing.
  assert.strictEqual(manualDownloadUrl("dev", "darwin"), null);
  assert.strictEqual(manualDownloadUrl("", "darwin"), null);
  assert.strictEqual(manualDownloadUrl("nightly", "win32"), null);
  assert.strictEqual(manualDownloadUrl(undefined, undefined), null);
  // A Linux arch with no published AppImage returns null rather than guessing
  // x86_64: a wrong-arch binary is a worse answer than no link.
  assert.strictEqual(manualDownloadUrl("stable", "linux", "armv7l"), null);
  assert.strictEqual(manualDownloadUrl("stable", "linux", "ia32"), null);
});

test("manualDownloadUrl: points at the same CDN the updater pulls from", () => {
  // A manual reinstall must land on identical artifacts, not a different host.
  assert.match(manualDownloadUrl("nightly", "darwin"), /^https:\/\/download\.crew\.kiro\.dev\//);
});
