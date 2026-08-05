const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { buildMenuTemplate } = require("../context-menu");

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
