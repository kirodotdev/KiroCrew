const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  armSplashHistoryClear,
  transientEntryIndexes,
  isTransientShellPage,
} = require("../splash-history");

// Regression tests for #5538: mouse Back (button 4) navigated the main window
// back to the boot splash (loading.html), which offers no way forward — the
// splash was left in Chromium's navigation history because the dashboard is
// loaded into the SAME webContents that painted it. The fix removes the
// transient shell entries (loading.html / token-prompt.html) from history once
// the dashboard commits, so the splash is no longer a reachable history entry.
// Two things are deliberately NOT done, and these tests pin both:
//   - the back gesture itself is not intercepted (the dashboard SPA's own
//     history must keep working), and
//   - the history is not wholesale-cleared: after a gateway reconnect the
//     history legitimately holds the user's own prior dashboard routes behind
//     the re-painted splash, and clearing would erase those too, breaking
//     in-app Back. Only the transient entries are removed, surgically.

const LOADING = "file:///app/resources/loading.html";
const LOADING_WITH_ACCENT = "file:///app/resources/loading.html?accent=%23a259ff";
const TOKEN_PROMPT = "file:///app/resources/token-prompt.html?port=5476&kind=own";
const DASHBOARD = "http://localhost:5476/?token=abc123";
const DASH_ROUTE_A = "http://localhost:5476/settings";
const DASH_ROUTE_B = "http://localhost:5476/sessions/42";

/**
 * Minimal fake of the slice of Electron's webContents the helper touches:
 * an EventEmitter-ish `on`, `getURL`, and `navigationHistory` with
 * getAllEntries/getActiveIndex/removeEntryAtIndex. Navigations are simulated
 * by pushing entries and firing the captured did-finish-load handler, which is
 * how the real event sequences (boot, recovery re-paint, token-prompt submit)
 * are replayed below. `navigateInPage` models an SPA pushState route change:
 * it mints a history entry but does NOT fire did-finish-load.
 */
function makeFakeWebContents() {
  const handlers = {};
  let entries = [];
  let activeIndex = -1;
  const removedIndexes = [];
  const wc = {
    on(event, fn) {
      handlers[event] = fn;
    },
    getURL() {
      return activeIndex >= 0 ? entries[activeIndex].url : "";
    },
    navigationHistory: {
      getAllEntries: () => entries.map((e) => ({ ...e })),
      getActiveIndex: () => activeIndex,
      removeEntryAtIndex(i) {
        removedIndexes.push(i);
        if (i === activeIndex) return false; // Chromium refuses the active entry
        entries.splice(i, 1);
        if (i < activeIndex) activeIndex -= 1;
        return true;
      },
    },
  };
  const commit = (url) => {
    // A committed navigation truncates any forward entries, then appends.
    entries.splice(activeIndex + 1);
    entries.push({ url, title: "" });
    activeIndex = entries.length - 1;
  };
  return {
    wc,
    navigate(url) {
      commit(url);
      if (handlers["did-finish-load"]) handlers["did-finish-load"]();
    },
    navigateInPage(url) {
      commit(url); // pushState mints an entry but fires no did-finish-load
    },
    urls: () => entries.map((e) => e.url),
    removedIndexes,
    handlers,
  };
}

describe("isTransientShellPage", () => {
  it("matches the splash and the token prompt as loaded by loadFile (file: URLs, with query)", () => {
    assert.equal(isTransientShellPage(LOADING), true);
    assert.equal(isTransientShellPage(LOADING_WITH_ACCENT), true);
    assert.equal(isTransientShellPage(TOKEN_PROMPT), true);
  });

  it("never matches the dashboard, even when a dashboard URL mentions loading.html", () => {
    assert.equal(isTransientShellPage(DASHBOARD), false);
    // Only file: URLs qualify — an http path/query naming loading.html is the
    // dashboard's business, not a shell page.
    assert.equal(isTransientShellPage("http://localhost:5476/loading.html"), false);
    assert.equal(isTransientShellPage("http://localhost:5476/?page=loading.html"), false);
  });

  it("tolerates junk without throwing", () => {
    assert.equal(isTransientShellPage(undefined), false);
    assert.equal(isTransientShellPage(null), false);
    assert.equal(isTransientShellPage(""), false);
    assert.equal(isTransientShellPage("file:"), false);
  });
});

describe("transientEntryIndexes", () => {
  it("lists the splash entry when the dashboard is active with the splash behind it", () => {
    assert.deepEqual(
      transientEntryIndexes({
        currentUrl: DASHBOARD,
        entries: [{ url: LOADING_WITH_ACCENT }, { url: DASHBOARD }],
        activeIndex: 1,
      }),
      [0]
    );
  });

  it("returns indexes in DESCENDING order (removal shifts later indexes down)", () => {
    assert.deepEqual(
      transientEntryIndexes({
        currentUrl: DASHBOARD,
        entries: [
          { url: LOADING },
          { url: TOKEN_PROMPT },
          { url: DASH_ROUTE_A },
          { url: LOADING_WITH_ACCENT },
          { url: DASHBOARD },
        ],
        activeIndex: 4,
      }),
      [3, 1, 0]
    );
  });

  it("lists nothing while the splash itself is the active page", () => {
    // During boot/recovery the splash is legitimately current — the prune runs
    // on the success transition, not on splash load.
    assert.deepEqual(
      transientEntryIndexes({
        currentUrl: LOADING,
        entries: [{ url: LOADING }],
        activeIndex: 0,
      }),
      []
    );
  });

  it("lists nothing for a pure-dashboard history (the SPA's own back must keep working)", () => {
    assert.deepEqual(
      transientEntryIndexes({
        currentUrl: DASHBOARD,
        entries: [{ url: DASH_ROUTE_A }, { url: DASHBOARD }],
        activeIndex: 1,
      }),
      []
    );
  });

  it("never lists the active entry, even if it is transient by URL", () => {
    // Belt-and-braces: Chromium refuses to remove the active entry anyway.
    assert.deepEqual(
      transientEntryIndexes({
        currentUrl: DASHBOARD, // current URL says dashboard…
        entries: [{ url: LOADING }, { url: DASHBOARD }],
        activeIndex: 0, // …but the snapshot marks the splash active
      }),
      []
    );
  });

  it("tolerates a malformed snapshot without throwing", () => {
    assert.deepEqual(
      transientEntryIndexes({ currentUrl: DASHBOARD, entries: null, activeIndex: 0 }),
      []
    );
    assert.deepEqual(
      transientEntryIndexes({ currentUrl: DASHBOARD, entries: [null, undefined], activeIndex: 1 }),
      []
    );
  });
});

describe("armSplashHistoryClear (simulated handoffs)", () => {
  it("boot: splash → dashboard removes the splash entry, leaving it unreachable", () => {
    const fake = makeFakeWebContents();
    armSplashHistoryClear(fake.wc);
    fake.navigate(LOADING_WITH_ACCENT); // splash paints — no prune (splash is active)
    assert.equal(fake.removedIndexes.length, 0);
    fake.navigate(DASHBOARD); // handoff commits
    assert.deepEqual(fake.urls(), [DASHBOARD]);
  });

  it("reconnect: prunes ONLY the splash — the user's prior dashboard routes survive", () => {
    // The regression the wholesale-clear design would have introduced (and the
    // GPT review blocked on): the user navigates the SPA (real pushState
    // history entries), the gateway dies, recovery re-paints loading.html into
    // the LIVE window, then the reconnect handoff loads the dashboard. The
    // splash entry must go; the user's own routes must NOT — in-app Back keeps
    // working after a reconnect.
    const fake = makeFakeWebContents();
    armSplashHistoryClear(fake.wc);
    fake.navigate(LOADING_WITH_ACCENT); // boot splash
    fake.navigate(DASHBOARD); // boot handoff → splash pruned
    fake.navigateInPage(DASH_ROUTE_A); // user navigates the SPA
    fake.navigateInPage(DASH_ROUTE_B);
    fake.navigate(LOADING); // gateway died — recovery re-paints the splash
    fake.navigate(DASHBOARD); // reconnect handoff
    assert.deepEqual(fake.urls(), [DASHBOARD, DASH_ROUTE_A, DASH_ROUTE_B, DASHBOARD]);
  });

  it("boot-only fix cannot pass: a second recovery re-paint is pruned too", () => {
    const fake = makeFakeWebContents();
    armSplashHistoryClear(fake.wc);
    fake.navigate(LOADING_WITH_ACCENT);
    fake.navigate(DASHBOARD);
    fake.navigate(LOADING); // recovery #1
    fake.navigate(DASHBOARD);
    fake.navigate(LOADING); // recovery #2
    fake.navigate(DASHBOARD);
    assert.equal(fake.urls().some(isTransientShellPage), false);
  });

  it("token prompt: the renderer-driven prompt → dashboard handoff is covered too", () => {
    // token-prompt.html navigates itself (window.location.href = dashboard),
    // so there is no main-process loadURL to hook — the persistent
    // did-finish-load listener must catch it. Both the splash AND the prompt
    // entries are pruned.
    const fake = makeFakeWebContents();
    armSplashHistoryClear(fake.wc);
    fake.navigate(LOADING_WITH_ACCENT);
    fake.navigate(TOKEN_PROMPT); // 403 → prompt shown; prompt is active: no prune
    assert.equal(fake.removedIndexes.length, 0);
    fake.navigate(DASHBOARD); // user submits the token
    assert.deepEqual(fake.urls(), [DASHBOARD]);
  });

  it("in-page SPA route changes never trigger a prune (no did-finish-load)", () => {
    const fake = makeFakeWebContents();
    armSplashHistoryClear(fake.wc);
    fake.navigate(LOADING_WITH_ACCENT);
    fake.navigate(DASHBOARD);
    const removedSoFar = fake.removedIndexes.length;
    fake.navigateInPage(DASH_ROUTE_A);
    fake.navigateInPage(DASH_ROUTE_B);
    assert.equal(fake.removedIndexes.length, removedSoFar);
    assert.deepEqual(fake.urls(), [DASHBOARD, DASH_ROUTE_A, DASH_ROUTE_B]);
  });

  it("dashboard-only reloads (token retry) touch nothing", () => {
    const fake = makeFakeWebContents();
    armSplashHistoryClear(fake.wc);
    fake.navigate(LOADING_WITH_ACCENT);
    fake.navigate(DASHBOARD);
    const removedSoFar = fake.removedIndexes.length;
    fake.navigate("http://localhost:5476/?token=rotated"); // token-retry reload
    assert.equal(fake.removedIndexes.length, removedSoFar); // pure dashboard — untouched
  });

  it("is inert when the window is already torn down (isAlive false)", () => {
    const fake = makeFakeWebContents();
    armSplashHistoryClear(fake.wc, { isAlive: () => false });
    fake.navigate(LOADING);
    fake.navigate(DASHBOARD);
    assert.equal(fake.removedIndexes.length, 0);
  });

  it("is inert on a webContents without navigationHistory", () => {
    const handlers = {};
    const wc = {
      on: (ev, fn) => {
        handlers[ev] = fn;
      },
      getURL: () => DASHBOARD,
      navigationHistory: undefined,
    };
    armSplashHistoryClear(wc);
    assert.doesNotThrow(() => handlers["did-finish-load"]());
  });

  it("swallows a teardown race (getURL throwing) like the surrounding loadFile sites do", () => {
    const handlers = {};
    const wc = {
      on: (ev, fn) => {
        handlers[ev] = fn;
      },
      getURL: () => {
        throw new Error("Object has been destroyed");
      },
      navigationHistory: {
        getAllEntries: () => [],
        getActiveIndex: () => 0,
        removeEntryAtIndex: () => true,
      },
    };
    armSplashHistoryClear(wc);
    assert.doesNotThrow(() => handlers["did-finish-load"]());
  });
});

// Wiring contract: the behavior tests above prove the helper works; this pins
// that the window owner actually uses it — armed once per window in
// setupWindowContents, which is what covers the main window AND per-connection
// windows, and (being a persistent listener) the boot, recovery, and
// token-prompt handoffs alike. A refactor that drops the arm call would leave
// every other test green while reintroducing #5538 — the same pattern
// hide-to-tray.test.js and fullscreen-bounds-settle.test.js use.
describe("window lifecycle splash-history wiring", () => {
  const WINDOW_LIFECYCLE_JS = fs.readFileSync(
    path.join(__dirname, "..", "window-lifecycle.js"),
    "utf8",
  );

  it("requires the helper", () => {
    assert.match(WINDOW_LIFECYCLE_JS, /require\("\.\/splash-history"\)/);
  });

  it("arms the prune inside setupWindowContents (once per window, before any handoff)", () => {
    const start = WINDOW_LIFECYCLE_JS.indexOf("function setupWindowContents");
    assert.notEqual(start, -1, "window-lifecycle.js must define setupWindowContents");
    // The arm call lives early in the function body — well before the ~2000
    // chars of view/bounds plumbing that follow view creation.
    const region = WINDOW_LIFECYCLE_JS.slice(start, start + 2000);
    assert.match(
      region,
      /armSplashHistoryClear\(view\.webContents/,
      "setupWindowContents must arm the splash-history prune on the view's webContents"
    );
  });

  it("guards the arm with the window/view liveness check window-lifecycle.js uses everywhere else", () => {
    const at = WINDOW_LIFECYCLE_JS.indexOf("armSplashHistoryClear(view.webContents");
    assert.notEqual(at, -1);
    const region = WINDOW_LIFECYCLE_JS.slice(at, at + 300);
    assert.match(region, /isAlive:.*isDestroyed\(\)/s);
  });
});
