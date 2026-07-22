/**
 * Right-click context menu for the KiroCrew dashboard window.
 *
 * Electron enables its built-in spellchecker by default and draws the red
 * underlines under misspelled words, but it does NOT ship a default UI for
 * reading `dictionarySuggestions` or invoking `replaceMisspelling`. Without
 * this module, right-clicking a misspelled word in the dashboard does
 * nothing — the gap this module closes.
 *
 * Logic and wiring are split so the template builder is unit-testable without
 * stubbing Electron:
 *   - buildMenuTemplate(params, platform, webContents): pure. All branching
 *     (misspelled word / editable / selection / nothing) lives here. Takes
 *     webContents as an explicit dependency so tests can pass a plain mock.
 *   - attachContextMenu(webContents): side-effecting. Registers the
 *     `context-menu` listener and pops the built menu on the owning
 *     BrowserWindow.
 */
function buildMenuTemplate(params, platform, webContents) {
  const items = [];

  // ── Misspelled-word block ──
  if (params.misspelledWord) {
    const suggestions = (params.dictionarySuggestions || []).slice(0, 5);
    if (suggestions.length) {
      for (const s of suggestions) {
        items.push({ label: s, click: () => webContents.replaceMisspelling(s) });
      }
    } else {
      items.push({ label: "No suggestions", enabled: false });
    }
    items.push({ type: "separator" });
    items.push({
      label: "Add to Dictionary",
      click: () => {
        const ok = webContents.session.addWordToSpellCheckerDictionary(params.misspelledWord);
        if (!ok) console.warn(`Failed to add "${params.misspelledWord}" to dictionary`);
      },
    });
    items.push({ type: "separator" });
  }

  // ── Editable block ──
  if (params.isEditable) {
    const f = params.editFlags || {};
    items.push({ role: "cut", enabled: !!f.canCut });
    items.push({ role: "copy", enabled: !!f.canCopy });
    items.push({ role: "paste", enabled: !!f.canPaste });
    items.push({ type: "separator" });
    items.push({ role: "selectAll" });
  } else if (params.selectionText) {
    // ── Selection block (non-editable) ──
    items.push({ role: "copy" });
    if (platform === "darwin") {
      // Collapse all whitespace (newlines, tabs, runs of spaces) so the menu
      // label stays single-line and readable before truncation.
      let truncated = params.selectionText.replace(/\s+/g, " ").trim();
      if (truncated.length > 25) truncated = truncated.slice(0, 25) + "\u2026";
      items.push({
        label: `Look Up '${truncated}'`,
        click: () => webContents.showDefinitionForSelection(),
      });
    }
  }

  // Strip trailing separators
  while (items.length && items[items.length - 1].type === "separator") {
    items.pop();
  }

  return items;
}

function attachContextMenu(webContents) {
  const { Menu, BrowserWindow } = require("electron");
  webContents.on("context-menu", (_event, params) => {
    const template = buildMenuTemplate(params, process.platform, webContents);
    if (!template.length) return;
    const win = BrowserWindow.fromWebContents(webContents);
    if (!win) return;
    Menu.buildFromTemplate(template).popup({ window: win });
  });
}

module.exports = { buildMenuTemplate, attachContextMenu };
