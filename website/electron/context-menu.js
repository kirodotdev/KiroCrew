/**
 * Right-click context menu for the KiroCrew dashboard window.
 *
 * Electron enables its built-in spellchecker by default and draws the red
 * underlines under misspelled words, but it does NOT ship a default UI for
 * reading `dictionarySuggestions` or invoking `replaceMisspelling`. Without
 * this module, right-clicking a misspelled word in the dashboard does
 * nothing — the gap this module closes.
 *
 * It also owns the LINK block. Right-clicking a link rendered in chat used to
 * yield only the text-selection menu, so extracting a clean URL meant an
 * imprecise drag-selection over the visible label (issue #5920). Electron's
 * `context-menu` params already carry `linkURL`, so the fix is a branch on the
 * existing template — nothing is plumbed from the renderer.
 *
 * The block is clipboard-only, deliberately: left-click already reaches the OS
 * browser for an external link, so the missing capability was the clipboard,
 * not a second spelling of the hand-off.
 *
 * Logic and wiring are split so the template builder is unit-testable without
 * stubbing Electron:
 *   - buildMenuTemplate(params, platform, webContents, deps): pure. All
 *     branching (link / misspelled word / editable / selection / nothing)
 *     lives here. Takes webContents and `deps` as explicit dependencies so
 *     tests can pass plain mocks.
 *   - attachContextMenu(webContents, options): side-effecting. Registers the
 *     `context-menu` listener and pops the built menu on the owning
 *     BrowserWindow.
 */

/** A `C:\…` / `C:/…` drive-absolute path, mirroring the renderer's own rule
 * (`WINDOWS_ABS_PATH_RE` in src/utils/urlTransform.ts). */
const WINDOWS_ABS_PATH_RE = /^[A-Za-z]:[\\/]/;

/** Longest string worth a filesystem probe. A pathname past this is not a real
 * path on any supported platform, so it stays a URL without touching disk. */
const MAX_PROBE_LENGTH = 4096;

/** Two leading separators name a HOST, not a path — `//evil/share`, and the
 * backslash spelling `\\evil\share` Windows treats identically.
 *
 * A chat message is untrusted, so a crafted link such as `/%2Fevil/share`
 * decodes to `//evil/share`; probing that hands Windows an outbound SMB
 * authentication attempt against the attacker's host, leaking the user's
 * credentials with no click beyond the right-press itself. The renderer already
 * refuses the same shape on both of its paths (`MdAnchor` drops a `//` href, and
 * `urlTransform.ts` excludes backslash UNC from the local-file branch for this
 * exact reason), so this is that boundary restated where the probe lives.
 *
 * Screened BEFORE the first filesystem call: a probe on a linked ancestor is
 * already the outbound connection, so a check that runs after it is too late. */
const UNC_PREFIX_RE = /^[/\\]{2}/;

/**
 * Read the on-disk path a dashboard-origin link stands for, or null.
 *
 * A file link rendered in chat is an anchor whose href is the bare absolute
 * path, so the browser resolves it against the dashboard origin and `linkURL`
 * arrives as `http://127.0.0.1:PORT/local/home/me/notes.md`. Copying THAT is
 * useless — the user wants `/local/home/me/notes.md`. Two same-origin shapes
 * carry a path:
 *   - `/api/file-raw?path=<abs>` — the path is explicit in the query.
 *   - `/<abs>` — the pathname IS the path.
 * The second shape is indistinguishable from a dashboard route (`/schedule`,
 * `/artifacts/foo`) by shape alone, and the route table has a catch-all, so it
 * is settled by a single `pathExists` probe rather than a route list that would
 * drift. A route that does not exist on disk stays a URL.
 *
 * Both shapes are screened for a host-naming UNC prefix (see `UNC_PREFIX_RE`)
 * before anything touches the filesystem or the clipboard.
 *
 * @param {string} linkURL the raw `params.linkURL`
 * @param {string} appOrigin any URL on the dashboard origin (absent = disabled)
 * @param {(p: string) => boolean} pathExists filesystem probe
 * @returns {string|null} the bare path, or null when this is not a file link
 */
function localPathForLink(linkURL, appOrigin, pathExists) {
  if (!appOrigin || typeof pathExists !== "function") return null;
  let url;
  let origin;
  try {
    url = new URL(linkURL);
    origin = new URL(appOrigin).origin;
  } catch {
    return null;
  }
  if (url.origin !== origin) return null;

  // Explicit form first: /api/file-raw?path=<abs> addresses a file BY path, so
  // no probe is needed — the query already says what it is.
  const declared = url.pathname === "/api/file-raw" ? url.searchParams.get("path") : null;
  if (declared) {
    if (declared.includes("\u0000") || UNC_PREFIX_RE.test(declared)) return null;
    return declared;
  }

  let pathname;
  try {
    pathname = decodeURIComponent(url.pathname);
  } catch {
    return null;
  }
  if (!pathname.startsWith("/") || pathname === "/") return null;
  // A NUL would crash a realpath downstream; length past the cap is not a path.
  if (pathname.includes("\u0000") || pathname.length > MAX_PROBE_LENGTH) return null;
  // `/C:/Users/me/x.ts` is how a Windows drive path survives URL resolution.
  const candidate = WINDOWS_ABS_PATH_RE.test(pathname.slice(1)) ? pathname.slice(1) : pathname;
  // The screen tests the value handed to `pathExists`, so no later rewrite of
  // `candidate` can slip a host past it.
  if (UNC_PREFIX_RE.test(candidate)) return null;
  try {
    return pathExists(candidate) ? candidate : null;
  } catch {
    // An unreadable ancestor or an EACCES is not a reason to disturb the menu.
    return null;
  }
}

/** Items for the link block, or an empty array when there is no link. */
function buildLinkItems(params, deps) {
  const linkURL = params.linkURL;
  if (!linkURL || typeof linkURL !== "string") return [];
  const items = [];
  const write = (value) => {
    const clipboard = deps.clipboard || require("electron").clipboard;
    clipboard.writeText(value);
  };

  const localPath = localPathForLink(linkURL, deps.appOrigin, deps.pathExists);
  if (localPath) {
    items.push({ label: "Copy File Path", click: () => write(localPath) });
    return items;
  }

  // Copy only. An "open in browser" item would re-spell what a plain left-click
  // already does: chat renders external links `target="_blank"`, so activating
  // one goes through `setWindowOpenHandler` -> `openExternalSafely` and reaches
  // the same OS hand-off. #5920 asks for the clipboard, which left-click has no
  // spelling for.
  items.push({ label: "Copy Link Address", click: () => write(linkURL) });
  return items;
}

function buildMenuTemplate(params, platform, webContents, deps = {}) {
  const items = [];

  // ── Link block ──
  //
  // First, mirroring every browser's own menu: when the click landed on a link
  // the link is what the user meant. The selection block below still renders
  // when a selection is also live, so the existing entries never disappear.
  const linkItems = buildLinkItems(params, deps);
  if (linkItems.length) {
    items.push(...linkItems);
    items.push({ type: "separator" });
  }

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

/**
 * Register the `context-menu` listener on a webContents.
 *
 * @param {object} webContents the target
 * @param {object} [options]
 * @param {() => string} [options.getAppOrigin] resolves the dashboard origin.
 *   Supplying it enables the dashboard-origin file-path branch, so pass it ONLY
 *   for webContents that actually render the dashboard — for arbitrary web
 *   content a same-origin pathname that happens to exist on disk is not a file.
 */
function attachContextMenu(webContents, options = {}) {
  const { Menu, BrowserWindow } = require("electron");
  const { existsSync } = require("node:fs");
  webContents.on("context-menu", (_event, params) => {
    let appOrigin = "";
    if (typeof options.getAppOrigin === "function") {
      try {
        appOrigin = options.getAppOrigin() || "";
      } catch {
        /* a missing origin only disables the path branch */
      }
    }
    const template = buildMenuTemplate(params, process.platform, webContents, {
      appOrigin,
      pathExists: existsSync,
    });
    if (!template.length) return;
    const win = BrowserWindow.fromWebContents(webContents);
    if (!win) return;
    Menu.buildFromTemplate(template).popup({ window: win });
  });
}

module.exports = { buildMenuTemplate, attachContextMenu, localPathForLink };
