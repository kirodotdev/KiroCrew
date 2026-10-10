"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const {
  fsPathKey,
  uniqueSavePath,
  safeFilename,
  urlOriginIsDashboard,
  shouldAutoSave,
  createWillDownloadHandler,
  wireWillDownloadOnce,
} = require("../download-handler");
const { createDownloadExpectations } = require("../download-expectations");

const DOWNLOADS = "/home/u/Downloads";

// A pending-expectations double. `always` claims every call (to exercise the
// save-path mechanics without caring about announcement); otherwise it behaves
// like the real single-use store seeded with the given {origin, filename} pairs.
function fakePending({ always = false, seed = [] } = {}) {
  const entries = seed.map((e) => ({ ...e }));
  const calls = [];
  return {
    calls,
    claim(arg) {
      calls.push(arg);
      if (always) return true;
      const i = entries.findIndex(
        (e) => e.origin === arg.origin && e.filename === arg.filename,
      );
      if (i === -1) return false;
      entries.splice(i, 1);
      return true;
    },
  };
}
const ALWAYS = () => fakePending({ always: true });

// A fake DownloadItem: records the save path set on it and lets a test drive
// the one-shot "done" event with a chosen final state. `url` defaults to a
// dashboard-origin blob: URL so the handler's auto-save eligibility gate
// passes; tests exercising the gate pass an explicit url (and optionally a
// multi-hop `chain`). `getURLChain` defaults to `[url]`.
function fakeItem(filename, url = "blob:http://localhost:5173/abc-123", chain = null) {
  const item = {
    _savePath: null,
    _doneHandler: null,
    _cancelled: false,
    getFilename: () => filename,
    getURL: () => url,
    getURLChain: () => (chain == null ? [url] : chain),
    setSavePath: (p) => { item._savePath = p; },
    cancel: () => { item._cancelled = true; },
    once: (ev, handler) => { if (ev === "done") item._doneHandler = handler; },
    emitDone: (state) => item._doneHandler && item._doneHandler({}, state),
  };
  return item;
}

const fakeApp = { getPath: (name) => (name === "downloads" ? DOWNLOADS : `/${name}`) };

describe("uniqueSavePath", () => {
  it("uses the plain path when nothing exists", () => {
    const fs = { existsSync: () => false };
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "export.zip"),
      path.join(DOWNLOADS, "export.zip"),
    );
  });

  it("suffixes ' (1)', ' (2)' past existing files, keeping the extension", () => {
    const taken = new Set([
      path.join(DOWNLOADS, "export.zip"),
      path.join(DOWNLOADS, "export (1).zip"),
    ]);
    const fs = { existsSync: (p) => taken.has(p) };
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "export.zip"),
      path.join(DOWNLOADS, "export (2).zip"),
    );
  });

  it("skips a reserved path even when it does not yet exist on disk", () => {
    // Nothing on disk, but "export.zip" is already claimed by an in-flight
    // download -> the next request must pick "export (1).zip", not reuse it.
    const fs = { existsSync: () => false };
    const reserved = new Set([path.join(DOWNLOADS, "export.zip")]);
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "export.zip", 1000, reserved),
      path.join(DOWNLOADS, "export (1).zip"),
    );
  });

  it("returns null rather than an occupied path when the budget is exhausted", () => {
    const fs = { existsSync: () => true };
    // maxTries=3: plain, (1), (2), (3) are all taken -> no free name -> null,
    // so the caller refuses the download instead of overwriting (1000)/(3).
    const out = uniqueSavePath(fs, DOWNLOADS, "export.zip", 3);
    assert.equal(out, null);
  });

  it("handles an extensionless filename", () => {
    const taken = new Set([path.join(DOWNLOADS, "data")]);
    const fs = { existsSync: (p) => taken.has(p) };
    assert.equal(uniqueSavePath(fs, DOWNLOADS, "data"), path.join(DOWNLOADS, "data (1)"));
  });

  it("treats a case-variant reservation as taken on a case-insensitive volume (F1)", () => {
    // Nothing on disk; "Report.pdf" is reserved by an in-flight download. On a
    // case-insensitive volume (darwin/win32) "report.pdf" names the SAME entry,
    // so the next request must NOT reuse it -- it has to suffix instead.
    const fs = { existsSync: () => false };
    const reserved = new Set([fsPathKey(path.join(DOWNLOADS, "Report.pdf"), "darwin")]);
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "report.pdf", 1000, reserved, "darwin"),
      path.join(DOWNLOADS, "report (1).pdf"),
      "a case-variant reservation collides on a case-insensitive volume",
    );
    // The returned path keeps its original (lower-case) spelling.
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "report.pdf", 1000, reserved, "darwin"),
      path.join(DOWNLOADS, "report (1).pdf"),
    );
  });

  it("keeps case-variant names distinct on a case-SENSITIVE volume (linux)", () => {
    // On Linux "Report.pdf" and "report.pdf" are two different files, so a
    // reservation of one must NOT block the other.
    const fs = { existsSync: () => false };
    const reserved = new Set([fsPathKey(path.join(DOWNLOADS, "Report.pdf"), "linux")]);
    assert.equal(
      uniqueSavePath(fs, DOWNLOADS, "report.pdf", 1000, reserved, "linux"),
      path.join(DOWNLOADS, "report.pdf"),
      "distinct-case names do not collide on a case-sensitive volume",
    );
  });
});

describe("fsPathKey", () => {
  const P = path.join(DOWNLOADS, "Report.pdf");
  it("folds case on darwin and win32 (case-insensitive volumes)", () => {
    assert.equal(fsPathKey(P, "darwin"), P.toLowerCase());
    assert.equal(fsPathKey(P, "win32"), P.toLowerCase());
  });
  it("preserves case on linux (case-sensitive volume)", () => {
    assert.equal(fsPathKey(P, "linux"), P);
  });
  it("maps two case-variant spellings to the same key on a case-insensitive volume", () => {
    assert.equal(
      fsPathKey(path.join(DOWNLOADS, "Report.pdf"), "win32"),
      fsPathKey(path.join(DOWNLOADS, "report.pdf"), "win32"),
    );
  });
  it("maps them to DIFFERENT keys on a case-sensitive volume", () => {
    assert.notEqual(
      fsPathKey(path.join(DOWNLOADS, "Report.pdf"), "linux"),
      fsPathKey(path.join(DOWNLOADS, "report.pdf"), "linux"),
    );
  });
});

describe("createWillDownloadHandler", () => {
  const DASH = "http://localhost:5173";
  it("sets a save path under the downloads dir so the file actually lands (issue #13047)", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS() });
    const item = fakeItem("kirocrew-export.zip");
    handler({}, item);
    assert.equal(item._savePath, path.join(DOWNLOADS, "kirocrew-export.zip"));
  });

  it("logs nothing extra when the download completes (no OS file-manager reveal)", () => {
    const fs = { existsSync: () => false };
    const logs = [];
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS(), log: (...a) => logs.push(a.join(" ")) });
    const item = fakeItem("export.zip");
    handler({}, item);
    item.emitDone("completed");
    // A completed download is silent: no reveal, no log line.
    assert.equal(logs.length, 0);
  });

  it("logs when the download is interrupted", () => {
    const fs = { existsSync: () => false };
    const logs = [];
    const handler = createWillDownloadHandler({
      app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS(), log: (...a) => logs.push(a.join(" ")),
    });
    const item = fakeItem("export.zip");
    handler({}, item);
    item.emitDone("interrupted");
    assert.match(logs.join("\n"), /did not complete.*interrupted/);
  });

  it("cancels and logs instead of overwriting when no free filename is left (F1)", () => {
    // Every candidate under Downloads already exists -> uniqueSavePath returns
    // null -> the handler must cancel the download and never call setSavePath
    // with an occupied path (which would overwrite an existing file).
    const fs = { existsSync: () => true };
    const logs = [];
    const handler = createWillDownloadHandler({
      app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS(), log: (...a) => logs.push(a.join(" ")),
    });
    const item = fakeItem("export.zip");
    handler({}, item);
    assert.equal(item._savePath, null);
    assert.equal(item._cancelled, true);
    assert.match(logs.join("\n"), /no free filename.*cancelling/);
  });

  it("gives two concurrent same-named downloads distinct paths, and frees the path on done (F1 concurrency)", () => {
    // Nothing on disk; two downloads named "report.pdf" start before either
    // lands. Without an in-flight reservation both would get "report.pdf" and
    // one would overwrite the other.
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS() });
    const first = fakeItem("report.pdf");
    const second = fakeItem("report.pdf");
    handler({}, first);
    handler({}, second);
    assert.equal(first._savePath, path.join(DOWNLOADS, "report.pdf"));
    assert.equal(second._savePath, path.join(DOWNLOADS, "report (1).pdf"));
    assert.notEqual(first._savePath, second._savePath);

    // When the first completes, its reservation is released, so a later
    // download can reuse the (now disk-check-governed) base name again.
    first.emitDone("completed");
    const third = fakeItem("report.pdf");
    handler({}, third);
    // second is still in flight holding "report (1).pdf"; the base name is free
    // again (fs still reports nothing on disk in this fake).
    assert.equal(third._savePath, path.join(DOWNLOADS, "report.pdf"));
  });

  it("gives case-variant concurrent downloads distinct entries on a case-insensitive volume (F1 security-class)", () => {
    // The exact withheld finding: "Report.pdf" and "report.pdf" start
    // concurrently, before either lands, on a case-insensitive Downloads volume
    // (darwin/win32). They name the SAME filesystem entry, so without a
    // filesystem-equivalent reservation key the second reuses the first's path
    // and one overwrites/corrupts the other. The folded reservation key must
    // force the second onto a suffixed name.
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({
      app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS(), platform: "darwin",
    });
    const first = fakeItem("Report.pdf");
    const second = fakeItem("report.pdf");
    handler({}, first);
    handler({}, second);
    assert.equal(first._savePath, path.join(DOWNLOADS, "Report.pdf"), "first keeps its original spelling");
    assert.equal(
      second._savePath, path.join(DOWNLOADS, "report (1).pdf"),
      "the case-variant second download must NOT collide with the first's entry",
    );
    // Distinct even after case-folding -> they are genuinely different files.
    assert.notEqual(
      fsPathKey(first._savePath, "darwin"),
      fsPathKey(second._savePath, "darwin"),
      "the two save paths resolve to different filesystem entries",
    );

    // Releasing the first frees its folded key, so a later case-variant reuses
    // the base name again (disk-check-governed).
    first.emitDone("completed");
    const third = fakeItem("REPORT.pdf");
    handler({}, third);
    assert.equal(third._savePath, path.join(DOWNLOADS, "REPORT.pdf"));
  });

  it("falls back without throwing when the save path cannot be computed", () => {
    const fs = { existsSync: () => false };
    const throwingApp = { getPath: () => { throw new Error("no downloads dir"); } };
    const logs = [];
    const handler = createWillDownloadHandler({
      app: throwingApp, fs, dashboardUrl: DASH, pending: ALWAYS(), log: (...a) => logs.push(a.join(" ")),
    });
    const item = fakeItem("export.zip");
    // Must not throw out of the event handler.
    assert.doesNotThrow(() => handler({}, item));
    assert.equal(item._savePath, null);
    assert.match(logs.join("\n"), /could not set save path/);
  });
});
describe("createWillDownloadHandler per-window origin (multi-window, issue #13047)", () => {
  // The single will-download handler is shared by every window (one per
  // session). A download carries its OWN webContents as the third argument;
  // `resolveDashboardUrl` maps that back to the owning window's backend URL, so
  // a second window on a DIFFERENT gateway origin also auto-saves instead of
  // falling back to Chromium's dialogless (file-never-lands) default.
  const WIN_A = "http://localhost:5173"; // first dashboard window
  const WIN_B = "http://localhost:7777"; // a later window on a different gateway

  function handlerFor(backendByWc) {
    const fs = { existsSync: () => false };
    return createWillDownloadHandler({
      app: fakeApp,
      fs,
      // No static dashboardUrl: proves the save is driven purely by the
      // per-download resolution from webContents, not a captured first origin.
      resolveDashboardUrl: (wc) => backendByWc.get(wc),
      pending: ALWAYS(),
    });
  }

  it("saves exports from TWO windows on different origins (not just the first)", () => {
    const wcA = { id: "A" };
    const wcB = { id: "B" };
    const handler = handlerFor(new Map([[wcA, WIN_A], [wcB, WIN_B]]));

    // Window A's export (same origin as window A).
    const a = fakeItem("export.zip", "http://localhost:5173/export.zip");
    handler({}, a, wcA);
    assert.equal(a._savePath, path.join(DOWNLOADS, "export.zip"), "first window's export saves");

    // Window B's export comes from a DIFFERENT origin. With the old
    // single-captured-URL handler this failed shouldAutoSave and never landed;
    // resolving B's own origin from its webContents makes it save too.
    const b = fakeItem("export.zip", "http://localhost:7777/export.zip");
    handler({}, b, wcB);
    assert.equal(b._savePath, path.join(DOWNLOADS, "export (1).zip"), "second window's export also saves");
  });

  it("still rejects a cross-origin download in a given window", () => {
    const wcB = { id: "B" };
    const handler = handlerFor(new Map([[wcB, WIN_B]]));
    // Window B is on :7777; a :5173 download is cross-origin FOR THIS window.
    const item = fakeItem("x.zip", "http://localhost:5173/x.zip");
    handler({}, item, wcB);
    assert.equal(item._savePath, null, "a download not matching this window's own origin is not saved");
  });

  it("falls back to the static dashboardUrl when the sender cannot be resolved", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({
      app: fakeApp,
      fs,
      dashboardUrl: WIN_A,
      resolveDashboardUrl: () => undefined, // unknown sender
      pending: ALWAYS(),
    });
    const item = fakeItem("export.zip", "http://localhost:5173/export.zip");
    handler({}, item, { id: "unknown" });
    assert.equal(item._savePath, path.join(DOWNLOADS, "export.zip"));
  });

  it("does not throw if resolveDashboardUrl throws; falls back to dashboardUrl", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({
      app: fakeApp,
      fs,
      dashboardUrl: WIN_A,
      resolveDashboardUrl: () => { throw new Error("window gone mid-teardown"); },
      pending: ALWAYS(),
    });
    const item = fakeItem("export.zip", "http://localhost:5173/export.zip");
    assert.doesNotThrow(() => handler({}, item, { id: "x" }));
    assert.equal(item._savePath, path.join(DOWNLOADS, "export.zip"));
  });
});

describe("safeFilename", () => {
  it("passes a plain filename through unchanged", () => {
    assert.equal(safeFilename("export.zip"), "export.zip");
  });

  it("strips any directory component (relative traversal)", () => {
    // A `Content-Disposition`/URL could claim a traversing name; only the leaf
    // may ever be joined onto the downloads dir.
    assert.equal(safeFilename("../../etc/passwd"), "passwd");
    assert.equal(safeFilename("a/b/c.txt"), "c.txt");
  });

  it("strips a leading absolute path to its leaf", () => {
    assert.equal(safeFilename("/etc/hosts"), "hosts");
  });

  it("falls back to 'download' for empty or dot-only names", () => {
    assert.equal(safeFilename(""), "download");
    assert.equal(safeFilename(null), "download");
    assert.equal(safeFilename(undefined), "download");
    assert.equal(safeFilename("."), "download");
    assert.equal(safeFilename(".."), "download");
    assert.equal(safeFilename("/"), "download");
  });

  it("strips a Windows backslash directory component even on a POSIX host", () => {
    assert.equal(safeFilename("..\\..\\secret.zip"), "secret.zip");
    assert.equal(safeFilename("a/b\\c.zip"), "c.zip");
    assert.equal(safeFilename("C:\\Users\\x\\evil.exe"), "evil.exe");
  });

  it("falls back to 'download' when the name contains a NUL byte", () => {
    assert.equal(safeFilename("evil\0.zip"), "download");
    assert.equal(safeFilename("a/b\0c"), "download");
  });

  it("rejects Windows reserved device names (with or without extension)", () => {
    for (const n of ["CON", "con", "nul", "NUL", "COM1", "lpt9", "PRN", "AUX"]) {
      assert.equal(safeFilename(n), "download", n);
      assert.equal(safeFilename(`${n}.txt`), "download", `${n}.txt`);
    }
    // A name that merely starts with a reserved stem is fine.
    assert.equal(safeFilename("console.log"), "console.log");
    assert.equal(safeFilename("communications.zip"), "communications.zip");
  });
});

describe("shouldAutoSave (chain-based, origin-exact)", () => {
  const DASH = "http://localhost:5173";

  it("accepts a dashboard-origin blob: URL", () => {
    assert.equal(shouldAutoSave(["blob:http://localhost:5173/abc"], DASH), true);
  });

  it("REJECTS a cross-origin blob: URL (blob minted by another frame)", () => {
    // The old code accepted any blob: by scheme; a blob from a cross-origin
    // frame must not auto-save.
    assert.equal(shouldAutoSave(["blob:https://evil.example/abc"], DASH), false);
    // An opaque-origin blob resolves to origin "null" -> never matches.
    assert.equal(shouldAutoSave(["blob:null/abc"], DASH), false);
  });

  it("accepts a URL same-origin with the dashboard", () => {
    assert.equal(shouldAutoSave(["http://localhost:5173/export.zip"], DASH), true);
    assert.equal(shouldAutoSave(["http://localhost:5173/a/b?c=d"], DASH), true);
  });

  it("rejects a cross-origin URL (exact origin, no prefix/suffix/userinfo trick)", () => {
    assert.equal(shouldAutoSave(["https://evil.example/x.zip"], DASH), false);
    // Same host, different port is a different origin.
    assert.equal(shouldAutoSave(["http://localhost:9999/x.zip"], DASH), false);
    // Suffix trick: host is localhost:5173.evil.com, not the dashboard.
    assert.equal(shouldAutoSave(["http://localhost:5173.evil.com/x"], DASH), false);
    // Userinfo trick: real origin is evil.example.
    assert.equal(shouldAutoSave(["http://localhost:5173@evil.example/x"], DASH), false);
  });

  it("rejects data:, file:, filesystem: (origin resolves to null)", () => {
    assert.equal(shouldAutoSave(["data:text/plain,hi"], DASH), false);
    assert.equal(shouldAutoSave(["file:///etc/passwd"], DASH), false);
    assert.equal(shouldAutoSave(["filesystem:http://localhost:5173/temporary/x"], DASH), false);
  });

  it("rejects when ANY hop of the redirect chain is not the dashboard origin", () => {
    // Final hop is same-origin, but it redirected from a cross-origin URL.
    assert.equal(
      shouldAutoSave(["https://evil.example/start", "http://localhost:5173/landing.zip"], DASH),
      false,
    );
    // All hops same-origin -> accepted.
    assert.equal(
      shouldAutoSave(["http://localhost:5173/a", "http://localhost:5173/b.zip"], DASH),
      true,
    );
  });

  it("rejects (fails closed) on missing/empty/unparseable inputs or opaque dashboard origin", () => {
    assert.equal(shouldAutoSave(["http://localhost:5173/x"], undefined), false);
    assert.equal(shouldAutoSave(["not a url"], DASH), false);
    assert.equal(shouldAutoSave([], DASH), false);
    assert.equal(shouldAutoSave(null, DASH), false);
    assert.equal(shouldAutoSave(["http://localhost:5173/x"], "not a url"), false);
    // A "null"-origin dashboard must not become a wildcard matching data:/file:.
    assert.equal(shouldAutoSave(["data:text/plain,hi"], "data:x"), false);
  });
});

describe("createWillDownloadHandler auto-save gate (origin / basename)", () => {
  const DASH = "http://localhost:5173";

  it("auto-saves a same-origin dashboard download", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS() });
    const item = fakeItem("export.zip", "http://localhost:5173/export.zip");
    handler({}, item);
    assert.equal(item._savePath, path.join(DOWNLOADS, "export.zip"));
  });

  it("does NOT save a cross-origin download, and sets no path (Chromium default)", () => {
    const fs = { existsSync: () => false };
    const logs = [];
    const handler = createWillDownloadHandler({
      app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS(), log: (...a) => logs.push(a.join(" ")),
    });
    const item = fakeItem("drive-by.exe", "https://evil.example/drive-by.exe");
    handler({}, item);
    assert.equal(item._savePath, null);
    assert.equal(item._cancelled, false);
    assert.match(logs.join("\n"), /not auto-saving non-dashboard download/);
  });

  it("does NOT save a cross-origin blob download", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS() });
    const item = fakeItem("x.zip", "blob:https://evil.example/abc");
    handler({}, item);
    assert.equal(item._savePath, null);
  });

  it("does NOT save when a redirect chain starts cross-origin even if it lands same-origin", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS() });
    const item = fakeItem(
      "x.zip",
      "http://localhost:5173/landing.zip",
      ["https://evil.example/start", "http://localhost:5173/landing.zip"],
    );
    handler({}, item);
    assert.equal(item._savePath, null);
  });

  it("does NOT save when there is no dashboardUrl", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs });
    const item = fakeItem("export.zip", "http://localhost:5173/export.zip");
    handler({}, item);
    assert.equal(item._savePath, null);
  });

  it("reduces a traversing filename to its basename under Downloads", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS() });
    const item = fakeItem("../../etc/passwd", "http://localhost:5173/x");
    handler({}, item);
    assert.equal(item._savePath, path.join(DOWNLOADS, "passwd"));
  });

  it("falls back to 'download' for a dot-only filename", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending: ALWAYS() });
    const item = fakeItem("..", "http://localhost:5173/x");
    handler({}, item);
    assert.equal(item._savePath, path.join(DOWNLOADS, "download"));
  });
});

describe("createWillDownloadHandler expectation gate (announced-only save)", () => {
  const DASH = "http://localhost:5173";
  const ORIGIN = "http://localhost:5173";

  it("does NOT save a same-origin download that was never announced", () => {
    const fs = { existsSync: () => false };
    const logs = [];
    const pending = fakePending({ seed: [] }); // nothing announced
    const handler = createWillDownloadHandler({
      app: fakeApp, fs, dashboardUrl: DASH, pending, log: (...a) => logs.push(a.join(" ")),
    });
    const item = fakeItem("artifact.zip", "http://localhost:5173/artifact.zip");
    handler({}, item);
    assert.equal(item._savePath, null);
    assert.equal(item._cancelled, false);
    assert.match(logs.join("\n"), /no pending expectation/);
  });

  it("saves a same-origin download that WAS announced, exactly once", () => {
    const fs = { existsSync: () => false };
    const pending = fakePending({ seed: [{ origin: ORIGIN, filename: "export.zip" }] });
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending });

    // First download matches the one announcement -> saved.
    const first = fakeItem("export.zip", "http://localhost:5173/export.zip");
    handler({}, first);
    assert.equal(first._savePath, path.join(DOWNLOADS, "export.zip"));

    // A SECOND identical download has no remaining expectation -> not saved
    // (the announcement was single-use).
    const second = fakeItem("export.zip", "http://localhost:5173/export.zip");
    handler({}, second);
    assert.equal(second._savePath, null);
  });

  it("claims with the dashboard origin and the sanitized filename", () => {
    const fs = { existsSync: () => false };
    const pending = fakePending({ always: true });
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH, pending });
    const item = fakeItem("../../export.zip", "http://localhost:5173/x");
    handler({}, item);
    assert.deepEqual(pending.calls[0], { origin: ORIGIN, filename: "export.zip" });
    assert.equal(item._savePath, path.join(DOWNLOADS, "export.zip"));
  });

  it("does NOT save (and does not claim) when there is no pending store", () => {
    const fs = { existsSync: () => false };
    const handler = createWillDownloadHandler({ app: fakeApp, fs, dashboardUrl: DASH });
    const item = fakeItem("export.zip", "http://localhost:5173/export.zip");
    handler({}, item);
    assert.equal(item._savePath, null);
  });
});

describe("createDownloadExpectations", () => {
  const O = "http://localhost:5173";

  it("claims a matching expectation exactly once (single use)", () => {
    const exp = createDownloadExpectations();
    exp.register({ origin: O, filename: "export.zip" });
    assert.equal(exp.size(), 1);
    assert.equal(exp.claim({ origin: O, filename: "export.zip" }), true);
    // Consumed: a second claim fails.
    assert.equal(exp.claim({ origin: O, filename: "export.zip" }), false);
    assert.equal(exp.size(), 0);
  });

  it("does not match a different origin or filename", () => {
    const exp = createDownloadExpectations();
    exp.register({ origin: O, filename: "export.zip" });
    assert.equal(exp.claim({ origin: "http://localhost:9999", filename: "export.zip" }), false);
    assert.equal(exp.claim({ origin: O, filename: "other.zip" }), false);
    // Original still live.
    assert.equal(exp.claim({ origin: O, filename: "export.zip" }), true);
  });

  it("expires an expectation after the TTL", () => {
    let t = 1000;
    const exp = createDownloadExpectations({ now: () => t, ttlMs: 30_000 });
    exp.register({ origin: O, filename: "export.zip" });
    t = 1000 + 30_001; // just past the window
    assert.equal(exp.claim({ origin: O, filename: "export.zip" }), false);
    assert.equal(exp.size(), 0);
  });

  it("ignores a malformed/opaque origin or empty filename (fails closed)", () => {
    const exp = createDownloadExpectations();
    exp.register({ origin: "", filename: "x.zip" });
    exp.register({ origin: "null", filename: "x.zip" });
    exp.register({ origin: O, filename: "" });
    assert.equal(exp.size(), 0);
    assert.equal(exp.claim({ origin: "null", filename: "x.zip" }), false);
    assert.equal(exp.claim({ origin: "", filename: "x.zip" }), false);
  });

  it("lets two different origins each expect the same filename", () => {
    const exp = createDownloadExpectations();
    const A = "http://localhost:5173";
    const B = "http://localhost:5174";
    exp.register({ origin: A, filename: "export.zip" });
    exp.register({ origin: B, filename: "export.zip" });
    assert.equal(exp.claim({ origin: A, filename: "export.zip" }), true);
    assert.equal(exp.claim({ origin: B, filename: "export.zip" }), true);
    assert.equal(exp.claim({ origin: A, filename: "export.zip" }), false);
  });
});


describe("wireWillDownloadOnce", () => {
  // A fake session that records how many will-download listeners were attached.
  function fakeSession() {
    const session = {
      willDownloadListeners: 0,
      on(event) { if (event === "will-download") session.willDownloadListeners += 1; return session; },
    };
    return session;
  }

  it("attaches the handler exactly once per session, even across many windows", () => {
    const fs = { existsSync: () => false };
    const session = fakeSession();
    // Every window that shares this session calls through here.
    const first = wireWillDownloadOnce(session, { app: fakeApp, fs });
    const second = wireWillDownloadOnce(session, { app: fakeApp, fs });
    const third = wireWillDownloadOnce(session, { app: fakeApp, fs });
    assert.equal(first, true);
    assert.equal(second, false);
    assert.equal(third, false);
    assert.equal(session.willDownloadListeners, 1);
  });

  it("wires separate sessions independently", () => {
    const fs = { existsSync: () => false };
    const a = fakeSession();
    const b = fakeSession();
    assert.equal(wireWillDownloadOnce(a, { app: fakeApp, fs }), true);
    assert.equal(wireWillDownloadOnce(b, { app: fakeApp, fs }), true);
    assert.equal(a.willDownloadListeners, 1);
    assert.equal(b.willDownloadListeners, 1);
  });

  it("is a no-op for a missing session", () => {
    assert.equal(wireWillDownloadOnce(null, { app: fakeApp }), false);
  });
});
