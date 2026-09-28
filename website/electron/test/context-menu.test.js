const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { buildMenuTemplate, localPathForLink } = require("../context-menu");

const ORIGIN = "http://127.0.0.1:3210/";

/** deps for the link block: a recording clipboard + a set-membership probe. */
function mockDeps(overrides = {}) {
  const written = [];
  const probed = [];
  return {
    written,
    probed,
    deps: {
      appOrigin: ORIGIN,
      pathExists: (p) => {
        probed.push(p);
        return (overrides.existing || []).includes(p);
      },
      clipboard: { writeText: (t) => written.push(t) },
      ...(overrides.deps || {}),
    },
  };
}

function mockParams(overrides = {}) {
  const calls = { replaceMisspelling: [], addWord: [] };
  const webContents = {
    replaceMisspelling: (w) => calls.replaceMisspelling.push(w),
    showDefinitionForSelection: () => {},
    session: { addWordToSpellCheckerDictionary: (w) => { calls.addWord.push(w); return true; } },
  };
  return {
    params: {
      misspelledWord: "",
      dictionarySuggestions: [],
      isEditable: false,
      editFlags: { canCut: true, canCopy: true, canPaste: true },
      selectionText: "",
      linkURL: "",
      ...overrides,
    },
    webContents,
    calls,
  };
}

describe("buildMenuTemplate", () => {
  it("1: misspelled word with 3 suggestions + editable", () => {
    const { params, webContents } = mockParams({
      misspelledWord: "teh",
      dictionarySuggestions: ["the", "tea", "ten"],
      isEditable: true,
    });
    const t = buildMenuTemplate(params, "darwin", webContents);
    assert.equal(t[0].label, "the");
    assert.equal(t[1].label, "tea");
    assert.equal(t[2].label, "ten");
    assert.equal(t[3].type, "separator");
    assert.equal(t[4].label, "Add to Dictionary");
    assert.equal(t[5].type, "separator");
    assert.equal(t[6].role, "cut");
  });

  it("2: misspelled word with empty suggestions", () => {
    const { params, webContents } = mockParams({ misspelledWord: "xyz", dictionarySuggestions: [], isEditable: true });
    const t = buildMenuTemplate(params, "darwin", webContents);
    assert.equal(t[0].label, "No suggestions");
    assert.equal(t[0].enabled, false);
    assert.equal(t[2].label, "Add to Dictionary");
  });

  it("3: misspelled word with >5 suggestions caps at 5", () => {
    const { params, webContents } = mockParams({
      misspelledWord: "wrng",
      dictionarySuggestions: ["a", "b", "c", "d", "e", "f", "g"],
    });
    const t = buildMenuTemplate(params, "darwin", webContents);
    const suggestions = t.filter((i) => i.click && i.label !== "Add to Dictionary");
    assert.equal(suggestions.length, 5);
  });

  it("4: editable field, no misspelling", () => {
    const { params, webContents } = mockParams({ isEditable: true });
    const t = buildMenuTemplate(params, "darwin", webContents);
    assert.equal(t[0].role, "cut");
    assert.equal(t[1].role, "copy");
    assert.equal(t[2].role, "paste");
    assert.equal(t[3].type, "separator");
    assert.equal(t[4].role, "selectAll");
    assert.ok(!t.some((i) => i.label === "Add to Dictionary"));
  });

  it("5: editable field, canPaste false", () => {
    const { params, webContents } = mockParams({
      isEditable: true,
      editFlags: { canCut: true, canCopy: true, canPaste: false },
    });
    const t = buildMenuTemplate(params, "darwin", webContents);
    const paste = t.find((i) => i.role === "paste");
    assert.equal(paste.enabled, false);
  });

  it("6: non-editable with selection on darwin", () => {
    const { params, webContents } = mockParams({ selectionText: "hello world" });
    const t = buildMenuTemplate(params, "darwin", webContents);
    assert.equal(t[0].role, "copy");
    assert.ok(t[1].label.startsWith("Look Up '"));
  });

  it("7: non-editable with selection on non-darwin", () => {
    const { params, webContents } = mockParams({ selectionText: "hello world" });
    const t = buildMenuTemplate(params, "linux", webContents);
    assert.equal(t.length, 1);
    assert.equal(t[0].role, "copy");
  });

  it("8: Look Up label truncates at 25 chars and replaces newlines", () => {
    const { params, webContents } = mockParams({ selectionText: "line one\nline two and more text that is very long" });
    const t = buildMenuTemplate(params, "darwin", webContents);
    const lookup = t.find((i) => i.label?.startsWith("Look Up"));
    assert.ok(lookup);
    assert.ok(!lookup.label.includes("\n"));
    const inner = lookup.label.slice("Look Up '".length, -1);
    assert.ok(inner.endsWith("\u2026"));
    assert.equal(inner.length, 26); // 25 + ellipsis
    assert.ok(!inner.includes("\n"));
  });

  it("8b: Look Up label collapses tabs and runs of spaces (not just newlines)", () => {
    const { params, webContents } = mockParams({ selectionText: "word\t\t with\n  lots   of   whitespace here" });
    const t = buildMenuTemplate(params, "darwin", webContents);
    const lookup = t.find((i) => i.label?.startsWith("Look Up"));
    assert.ok(lookup);
    // No runs of 2+ whitespace, no \t, no \n
    assert.ok(!/\s{2,}/.test(lookup.label));
    assert.ok(!lookup.label.includes("\t"));
  });

  it("9: no misspelling, not editable, no selection → empty", () => {
    const { params, webContents } = mockParams({});
    const t = buildMenuTemplate(params, "darwin", webContents);
    assert.equal(t.length, 0);
  });

  it("10: trailing separator is stripped", () => {
    const { params, webContents } = mockParams({ misspelledWord: "teh", dictionarySuggestions: ["the"] });
    const t = buildMenuTemplate(params, "darwin", webContents);
    assert.notEqual(t[t.length - 1].type, "separator");
  });

  it("suggestion click calls replaceMisspelling", () => {
    const { params, webContents, calls } = mockParams({
      misspelledWord: "teh",
      dictionarySuggestions: ["the"],
    });
    const t = buildMenuTemplate(params, "darwin", webContents);
    t[0].click();
    assert.deepEqual(calls.replaceMisspelling, ["the"]);
  });
});

describe("localPathForLink", () => {
  it("reads the bare path out of a dashboard-origin absolute-path link", () => {
    const exists = (p) => p === "/local/home/me/notes.md";
    assert.equal(
      localPathForLink("http://127.0.0.1:3210/local/home/me/notes.md", ORIGIN, exists),
      "/local/home/me/notes.md",
    );
  });

  it("percent-decodes the pathname before probing", () => {
    const seen = [];
    const exists = (p) => { seen.push(p); return p === "/tmp/my file.md"; };
    assert.equal(
      localPathForLink("http://127.0.0.1:3210/tmp/my%20file.md", ORIGIN, exists),
      "/tmp/my file.md",
    );
    assert.deepEqual(seen, ["/tmp/my file.md"]);
  });

  it("reads /api/file-raw?path= without probing the filesystem", () => {
    let probed = false;
    const exists = () => { probed = true; return false; };
    assert.equal(
      localPathForLink(
        "http://127.0.0.1:3210/api/file-raw?path=%2Fsrv%2Freport.md&v=3",
        ORIGIN,
        exists,
      ),
      "/srv/report.md",
    );
    assert.equal(probed, false);
  });

  it("strips the resolution slash off a Windows drive path", () => {
    const exists = (p) => p === "C:/Users/me/x.ts";
    assert.equal(
      localPathForLink("http://127.0.0.1:3210/C:/Users/me/x.ts", ORIGIN, exists),
      "C:/Users/me/x.ts",
    );
  });

  it("leaves a dashboard route alone (nothing on disk)", () => {
    assert.equal(localPathForLink("http://127.0.0.1:3210/schedule", ORIGIN, () => false), null);
    assert.equal(
      localPathForLink("http://127.0.0.1:3210/artifacts/foo", ORIGIN, () => false),
      null,
    );
  });

  it("never treats a cross-origin link as a path, even when it exists on disk", () => {
    assert.equal(localPathForLink("https://example.com/etc/passwd", ORIGIN, () => true), null);
  });

  it("is disabled without an origin or a probe", () => {
    assert.equal(localPathForLink("http://127.0.0.1:3210/tmp/x", "", () => true), null);
    assert.equal(localPathForLink("http://127.0.0.1:3210/tmp/x", ORIGIN, undefined), null);
  });

  it("refuses a host-naming UNC path WITHOUT probing (no outbound SMB auth)", () => {
    // `/%2Fevil/share` decodes to `//evil/share`; probing it would hand Windows
    // an outbound authentication attempt against the attacker's host.
    for (const link of [
      "http://127.0.0.1:3210/%2Fevil/share",
      "http://127.0.0.1:3210//evil/share",
      "http://127.0.0.1:3210/%5C%5Cevil%5Cshare",
      "http://127.0.0.1:3210/%2F%5Cevil/share",
    ]) {
      let probed = false;
      const exists = () => { probed = true; return true; };
      assert.equal(localPathForLink(link, ORIGIN, exists), null, link);
      assert.equal(probed, false, `${link} reached the filesystem`);
    }
  });

  it("refuses a UNC path declared in /api/file-raw?path= too", () => {
    let probed = false;
    const exists = () => { probed = true; return true; };
    for (const raw of ["%2F%2Fevil%2Fshare", "%5C%5Cevil%5Cshare"]) {
      assert.equal(
        localPathForLink(`http://127.0.0.1:3210/api/file-raw?path=${raw}`, ORIGIN, exists),
        null,
        raw,
      );
    }
    assert.equal(probed, false);
  });

  it("a single leading separator is still a normal path", () => {
    // The screen must not swallow the ordinary POSIX case it sits next to.
    const exists = (p) => p === "/evil/share";
    assert.equal(
      localPathForLink("http://127.0.0.1:3210/evil/share", ORIGIN, exists),
      "/evil/share",
    );
  });

  it("refuses a NUL and an over-long pathname without probing", () => {
    let probed = false;
    const exists = () => { probed = true; return true; };
    assert.equal(localPathForLink("http://127.0.0.1:3210/tmp/a%00b", ORIGIN, exists), null);
    assert.equal(
      localPathForLink(`http://127.0.0.1:3210/${"a".repeat(5000)}`, ORIGIN, exists),
      null,
    );
    assert.equal(probed, false);
  });

  it("survives an unparseable link and a throwing probe", () => {
    assert.equal(localPathForLink("not a url", ORIGIN, () => true), null);
    assert.equal(
      localPathForLink("http://127.0.0.1:3210/tmp/x", ORIGIN, () => { throw new Error("EACCES"); }),
      null,
    );
  });
});

describe("buildMenuTemplate link block", () => {
  it("L1: no linkURL leaves the template byte-identical to the pre-link build", () => {
    // The regression guard for the plain-text right-click. Every non-link shape
    // must produce exactly what it produced before the link block existed, so
    // the shapes are compared against the 3-arg call that predates `deps`.
    const shapes = [
      {},
      { selectionText: "hello world" },
      { isEditable: true },
      { isEditable: true, editFlags: { canCut: true, canCopy: true, canPaste: false } },
      { misspelledWord: "teh", dictionarySuggestions: ["the", "tea"] },
      { misspelledWord: "teh", dictionarySuggestions: [], isEditable: true },
    ];
    const shown = (t) => JSON.stringify(t.map((i) => ({
      label: i.label, role: i.role, type: i.type, enabled: i.enabled, click: !!i.click,
    })));
    for (const platform of ["darwin", "linux", "win32"]) {
      for (const overrides of shapes) {
        const a = mockParams(overrides);
        const b = mockParams(overrides);
        const { deps } = mockDeps({ existing: ["/anything"] });
        assert.equal(
          shown(buildMenuTemplate(a.params, platform, a.webContents, deps)),
          shown(buildMenuTemplate(b.params, platform, b.webContents)),
          `shape ${JSON.stringify(overrides)} on ${platform} changed`,
        );
      }
    }
  });

  it("L2: an http(s) link yields exactly one item, Copy Link Address", () => {
    // Clipboard only: left-click already routes an external link to the OS
    // browser via setWindowOpenHandler, so a menu hand-off would duplicate it.
    const { params, webContents } = mockParams({ linkURL: "https://example.com/a?b=1#c" });
    const { deps, written } = mockDeps();
    const t = buildMenuTemplate(params, "darwin", webContents, deps);
    assert.deepEqual(t.map((i) => i.label), ["Copy Link Address"]);
    t[0].click();
    assert.deepEqual(written, ["https://example.com/a?b=1#c"]);
  });

  it("L3: a dashboard-origin file link copies the bare path, NOT the localhost URL", () => {
    const { params, webContents } = mockParams({
      linkURL: "http://127.0.0.1:3210/local/home/me/notes.md",
    });
    const { deps, written, probed } = mockDeps({ existing: ["/local/home/me/notes.md"] });
    const t = buildMenuTemplate(params, "darwin", webContents, deps);
    assert.deepEqual(t.map((i) => i.label), ["Copy File Path"]);
    t[0].click();
    assert.deepEqual(written, ["/local/home/me/notes.md"]);
    assert.ok(!written[0].includes("127.0.0.1"));
    assert.deepEqual(probed, ["/local/home/me/notes.md"]);
  });

  it("L4: a link WITH a live selection keeps both blocks, link first", () => {
    const { params, webContents } = mockParams({
      linkURL: "https://example.com/x",
      selectionText: "some highlighted words",
    });
    const { deps } = mockDeps();
    const t = buildMenuTemplate(params, "darwin", webContents, deps);
    assert.equal(t[0].label, "Copy Link Address");
    assert.equal(t[1].type, "separator");
    // The pre-existing selection entries survive, unchanged and in order.
    assert.equal(t[2].role, "copy");
    assert.ok(t[3].label.startsWith("Look Up '"));
    assert.equal(t.length, 4);
  });

  it("L5: a link inside an editable field keeps cut/copy/paste/selectAll", () => {
    const { params, webContents } = mockParams({
      linkURL: "https://example.com/x",
      isEditable: true,
    });
    const { deps } = mockDeps();
    const t = buildMenuTemplate(params, "linux", webContents, deps);
    assert.equal(t[0].label, "Copy Link Address");
    assert.equal(t[1].type, "separator");
    assert.deepEqual(t.slice(2).map((i) => i.role || i.type),
      ["cut", "copy", "paste", "separator", "selectAll"]);
  });

  it("L6: a misspelled link label keeps its suggestions after the link block", () => {
    const { params, webContents } = mockParams({
      linkURL: "https://example.com/x",
      misspelledWord: "exmaple",
      dictionarySuggestions: ["example"],
    });
    const { deps } = mockDeps();
    const t = buildMenuTemplate(params, "darwin", webContents, deps);
    assert.equal(t[0].label, "Copy Link Address");
    assert.equal(t[1].type, "separator");
    assert.equal(t[2].label, "example");
    assert.equal(t[4].label, "Add to Dictionary");
  });

  it("L7: the browser panel (no origin) copies the URL and never probes disk", () => {
    const { params, webContents } = mockParams({
      linkURL: "https://example.com/local/home/me/notes.md",
    });
    const { deps, written, probed } = mockDeps({
      deps: { appOrigin: "" },
      existing: ["/local/home/me/notes.md"],
    });
    const t = buildMenuTemplate(params, "darwin", webContents, deps);
    assert.deepEqual(t.map((i) => i.label), ["Copy Link Address"]);
    t[0].click();
    assert.deepEqual(written, ["https://example.com/local/home/me/notes.md"]);
    assert.deepEqual(probed, []);
  });

  it("L8: a same-origin non-file link copies the URL and offers no hand-off", () => {
    const { params, webContents } = mockParams({ linkURL: "http://127.0.0.1:3210/schedule" });
    const { deps, written } = mockDeps({ existing: [] });
    const t = buildMenuTemplate(params, "darwin", webContents, deps);
    assert.deepEqual(t.map((i) => i.label), ["Copy Link Address"]);
    t[0].click();
    assert.deepEqual(written, ["http://127.0.0.1:3210/schedule"]);
  });

  // The UX review noted a browser would spell this "Copy Email Address" and copy
  // the bare address. Deliberately not done: the item's contract is "the
  // clipboard gets the link", and one scheme-specific relabel invites the whole
  // per-scheme table (tel:, sms:, magnet:) that #5920 never asked for.
  it("L9: a non-web scheme copies the whole URL, scheme included", () => {
    const { params, webContents } = mockParams({ linkURL: "mailto:someone@example.com" });
    const { deps, written } = mockDeps();
    const t = buildMenuTemplate(params, "darwin", webContents, deps);
    assert.deepEqual(t.map((i) => i.label), ["Copy Link Address"]);
    t[0].click();
    assert.deepEqual(written, ["mailto:someone@example.com"]);
  });

  it("L10: a link with nothing else selected still yields a usable menu", () => {
    const { params, webContents } = mockParams({ linkURL: "https://example.com/x" });
    const { deps } = mockDeps();
    const t = buildMenuTemplate(params, "linux", webContents, deps);
    assert.notEqual(t[t.length - 1].type, "separator");
    assert.deepEqual(t.map((i) => i.label), ["Copy Link Address"]);
  });
});
