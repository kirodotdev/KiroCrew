/**
 * Screenshot harness for the FILES-TAB surfaces of the Monaco → Pierre
 * migration: the pinned Files tab's tree rail, and the file viewer's
 * preview / source / diff modes with that rail docked beside them.
 *
 * Same house pattern as `capture-pierre-chat-diffs.mjs`: the REAL built SPA
 * (`website/dist`) behind the shared in-process static server, with every
 * `/api/**` answered from fixtures via Playwright route interception —
 * gateway-free (no kiro-cli, no live backend, no token). The client code under
 * test is unmodified.
 *
 * NEW FIXTURES (no sibling script stubbed these before):
 *   /api/project/tree         → the workspace path set the Pierre tree renders
 *   /api/project/git/status   → the changed-file set (Changed mode + lanes)
 *   /api/project/git          → repo/branch probe
 *   /api/file-read            → cold-tab content hydration (ChatPage useQueries)
 *   /api/file-diff            → the working-tree baseline the diff mode renders
 *
 * The panel state is pre-seeded through localStorage rather than driven by
 * clicks, because both surfaces are PERSISTED stores rather than props:
 *   `mc-activity-open:<slot>`  chatSlice's per-slot panel open flag
 *   `mc-panel-tabs:<slot>`     usePanelTabs' bucket ({activeId, tabs})
 * A file tab persists WITHOUT its content by design (`serializeBucket` strips
 * heavy bodies), so ChatPage's cold-tab hydration re-reads it from
 * `/api/file-read` — which is why that fixture is load-bearing, not decorative.
 *
 * Frames:
 *   10-files-tab-all-files    the pinned Files tab, ALL FILES: the tree rail
 *                             beside the (fileless) viewer column
 *   11-files-tab-changed      the same tab with the CHANGED filter selected —
 *                             the git-status set only, fully expanded, count
 *                             badge on the segment
 *   12-file-preview-aligned   THE PAIR (with 13): one markdown file in PREVIEW
 *   13-file-source-aligned    …and in SOURCE. Both are a clip of the panel's
 *                             TOP BAND, so the top of the prose/code AND the
 *                             top of the rail are in frame and directly
 *                             comparable. They prove the fix where preview sat
 *                             ~16px lower than source: a `py-4` inset lived on
 *                             the flex row that ALSO holds the tree rail, so
 *                             the preview inset pushed the RAIL down too. The
 *                             inset is horizontal-only now (`pl-4 pr-0`).
 *   14-file-diff-mode         the viewer in DIFF mode (buffer vs HEAD), split
 *   15-unsaved-banner         the unsaved-changes banner: inside the preview
 *                             column only, header height unchanged, rail not
 *                             shifted
 *   16-overflow-menu          the ⋯ menu open: the view-options group (word
 *                             wrap / line numbers / collapse unchanged) ruled
 *                             off from Refresh / Full screen above it
 *
 * Frames 10, 11, 14, 15, 16 are ELEMENT screenshots of the side panel; 12 and
 * 13 are top-band clips of the same element. Dimensions are asserted after
 * every write against the 2000px-per-edge PR-media budget.
 *
 * Usage: node scripts/capture-pierre-files-tab.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, readdirSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pierre-diffs'
/** Repo root, derived from this script's own location: the fixtures show a real
 *  project path in breadcrumbs and the file rail without pinning the frames to
 *  one machine's worktree. */
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-pierre-files'

/** Hard ceiling for a PR-attached PNG, on BOTH edges. */
const MAX_EDGE = 2000

/** Height of the top band captured for the 12/13 alignment pair, in CSS px:
 *  enough for the header row, the first prose block / first code lines, and the
 *  rail's own header plus its first few tree rows. A full-panel shot would show
 *  the same two tops, but a ~16px offset is not legible inside 850px of panel. */
const ALIGN_BAND_H = 300

mkdirSync(OUT, { recursive: true })

// ── Fixtures ────────────────────────────────────────────────────────────────

/* `/api/project/tree` returns paths RELATIVE to `root`. The tree model is
 * created with `flattenEmptyDirectories: true`, so single-child directory
 * chains (src/kiro_crew/dashboard/handlers) collapse into one row — which is
 * what the real workspace looks like too. Deep + shallow paths on purpose, so
 * All-files shows nesting and Changed shows a flat handful. */
const TREE_PATHS = [
  'AGENTS.md',
  'README.md',
  'package.json',
  'website/scripts/fixtures/pierre-migration.md',
  'docs/architecture/side-panel.md',
  'docs/system-specs/modules/session.md',
  'src/kiro_crew/dashboard/handlers/files.py',
  'src/kiro_crew/dashboard/handlers/sessions.py',
  'src/kiro_crew/dashboard/server.py',
  'src/kiro_crew/session.py',
  'website/package.json',
  'website/scripts/capture-pierre-chat-diffs.mjs',
  'website/scripts/capture-pierre-files-tab.mjs',
  'website/scripts/lib/serve-dist.mjs',
  'website/scripts/lib/stub-dashboard-api.mjs',
  'website/src/components/ContentRenderer.tsx',
  'website/src/components/DiffBlock.tsx',
  'website/src/components/MarkdownPanel.tsx',
  'website/src/pages/chat/FileBrowserRail.tsx',
  'website/src/pages/chat/FilesHomePanel.tsx',
  'website/src/pages/chat/SidePanel.tsx',
  'website/src/pierre/PierreEditorImpl.tsx',
  'website/src/pierre/PierreImpl.tsx',
  'website/src/pierre/PierreWorkspaceTreeImpl.tsx',
  'website/src/pierre/config.ts',
  'website/src/pierre/tree.tsx',
]

/* Porcelain letters, mapped client-side to Pierre's lane vocabulary by
 * `gitStatusFor`: M→modified, A→added, ?→untracked. Six entries so the Changed
 * segment carries a visible count badge and the flat set is obviously a
 * subset of the tree above. */
const GIT_FILES = [
  { path: 'website/scripts/fixtures/pierre-migration.md', status: 'M', staged: false, additions: 42, deletions: 6 },
  { path: 'website/src/components/MarkdownPanel.tsx', status: 'M', staged: false, additions: 88, deletions: 31 },
  { path: 'website/src/pages/chat/FileBrowserRail.tsx', status: 'M', staged: true, additions: 61, deletions: 12 },
  { path: 'website/src/pages/chat/FilesHomePanel.tsx', status: 'A', staged: true, additions: 57, deletions: 0 },
  { path: 'website/src/pierre/PierreWorkspaceTreeImpl.tsx', status: 'M', staged: false, additions: 34, deletions: 9 },
  { path: 'website/scripts/capture-pierre-files-tab.mjs', status: '?', staged: false },
]

/** The markdown file frames 12-16 open. An H1 on line 1 makes the top of the
 *  prose an unmistakable landmark for the alignment pair. */
const MD_PATH = `${PROJECT}/website/scripts/fixtures/pierre-migration.md`

const MD_CONTENT = `# Pierre migration

Every code and diff surface in the dashboard now renders through
\`@pierre/diffs\` instead of Monaco. The worker bundle is gone from the
graph and the eager chunk lost ~1.1 MB.

## Surfaces

| Surface | Before | After |
| --- | --- | --- |
| Chat diff block | MonacoDiffViewer | \`PierrePatch\` |
| Tool detail code | MonacoCodeBlock | \`PierreCode\` |
| File viewer diff | MonacoDiffViewer | \`PierreFilePair\` |
| File editor | Monaco editor | \`PierreEditor\` |
| Workspace tree | hand-rolled list | \`@pierre/trees\` |

## The file-tab layout

The viewer column and the browser rail are siblings in ONE flex row, under a
single full-width header. That is what makes the inset rule below load-bearing:

- the row's inset is **horizontal only** (\`pl-4 pr-0\`)
- vertical breathing room belongs INSIDE the scroll box
- a \`py-4\` on the row would push the rail down with the prose

\`\`\`ts
const options = {
  diffStyle: sideBySide ? 'split' : 'unified',
  overflow: wordWrap ? 'wrap' : 'scroll',
  expandUnchanged: !collapseUnchanged,
}
\`\`\`

## Open questions

1. Should the rail width persist per project rather than app-wide?
2. Does the collapsed-region separator need a keyboard affordance?
`

/** The on-disk baseline the diff view renders the buffer against. Differs from
 *  MD_CONTENT in two places, so DIFF mode has real hunks (an identical pair
 *  renders `ZeroDiffNotice` instead of a diff canvas). */
const MD_ORIGINAL = MD_CONTENT
  .replace('Every code and diff surface in the dashboard now renders through\n`@pierre/diffs` instead of Monaco. The worker bundle is gone from the\ngraph and the eager chunk lost ~1.1 MB.',
    'Every code and diff surface in the dashboard renders through Monaco.\nThe worker bundle is 1.1 MB of the eager chunk.')
  .replace('- the row\'s inset is **horizontal only** (`pl-4 pr-0`)\n- vertical breathing room belongs INSIDE the scroll box\n- a `py-4` on the row would push the rail down with the prose',
    '- the row carries a `py-4` inset\n- the rail inherits that inset and sits 16px low in preview')

/* `api.fileDiff` returns {diff, original, status}. `diff` only has to be
 * non-empty for the auto-diff heuristic; the rendered diff is computed
 * client-side from the original/buffer pair by PierreFilePair. */
const MD_DIFF = `--- a/website/scripts/fixtures/pierre-migration.md
+++ b/website/scripts/fixtures/pierre-migration.md
@@ -1,6 +1,7 @@
 # Pierre migration
-Every code and diff surface in the dashboard renders through Monaco.
+Every code and diff surface in the dashboard now renders through
+\`@pierre/diffs\` instead of Monaco. The worker bundle is gone from the
+graph and the eager chunk lost ~1.1 MB.
`

/** Files the fixture can serve to `/api/file-read`, keyed by absolute path. */
const FILE_CONTENT = { [MD_PATH]: MD_CONTENT }

const slots = [{
  key: SLOT,
  title: 'Pierre migration',
  running: false,
  last_message: 'Pierre migration',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  // The Files tab, the rail and the tree all hang off the slot's project dir.
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const t0 = Math.floor(Date.now() / 1000) - 900
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Move the file viewer and the workspace tree onto Pierre.', ts: String(t0) },
    { role: 'assistant', content: 'Done — the viewer, its diff mode and the browser rail all render through Pierre now.', ts: String(t0 + 90) },
  ],
}

// ── Panel state seeds (persisted stores, not props) ─────────────────────────

const FILES_TAB = { id: 'files', kind: 'files', title: 'Files' }
const fileTab = (diffMode) => ({
  id: `file:${MD_PATH}`,
  kind: 'file',
  title: 'pierre-migration.md',
  path: MD_PATH,
  slot: SLOT,
  // Explicit, so the auto-open-diff heuristic (status === 'modified' would
  // otherwise force diff mode on) can't decide the view out from under us.
  diffMode,
})

/** Bucket shape `usePanelTabs` rehydrates from `mc-panel-tabs:<slot>`. */
const bucket = (tabs, activeId) => JSON.stringify({ activeId, tabs })

// ── Harness ─────────────────────────────────────────────────────────────────

/** PNG width/height straight out of the IHDR chunk — no image dependency. */
function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

/**
 * Chromium to drive. `website/node_modules/playwright` pins one browser
 * revision, but this machine's `~/.cache/ms-playwright` may only hold builds
 * fetched by a DIFFERENT playwright — so honour `PLAYWRIGHT_CHROMIUM`, else the
 * newest cached shell, else let playwright resolve (and report) its own pin.
 */
function chromiumExecutable() {
  if (process.env.PLAYWRIGHT_CHROMIUM) return process.env.PLAYWRIGHT_CHROMIUM
  const cache = join(homedir(), '.cache', 'ms-playwright')
  if (!existsSync(cache)) return undefined
  const rev = d => parseInt((/-(\d+)$/.exec(d) || [])[1] || '0', 10)
  return readdirSync(cache)
    .filter(d => d.startsWith('chromium_headless_shell-') || d.startsWith('chromium-'))
    .sort((a, b) => rev(b) - rev(a))
    .flatMap(d => [
      join(cache, d, 'chrome-headless-shell-linux64', 'chrome-headless-shell'),
      join(cache, d, 'chrome-linux64', 'chrome'),
      join(cache, d, 'chrome-linux', 'chrome'),
    ])
    .find(existsSync)
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    // 11-12px tree rows and menu type render soft at 1x on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  /** The five project/file endpoints no sibling harness stubs, plus the slot
   *  list/detail pair. Consulted BEFORE the shared boot map. */
  const extra = async (path, route) => {
    const url = new URL(route.request().url())
    const q = url.searchParams.get('path') || ''

    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true

    // The tree: paths relative to `root`. `repo: true` also makes the rail's
    // git-status query fire in All-files mode, so the lanes paint there too.
    if (path === '/api/project/tree') {
      return json(route, { root: PROJECT, paths: TREE_PATHS, repo: true, truncated: false }), true
    }
    // Status paths are relative to `repoRoot`, which the client re-anchors
    // against the tree root — same directory here, which is the common case.
    if (path === '/api/project/git/status') {
      return json(route, { repo: true, repoRoot: PROJECT, branch: 'pierre-diffs', ahead: 0, behind: 0, files: GIT_FILES }), true
    }
    if (path === '/api/project/git') {
      return json(route, { path: PROJECT, repo: true, repoRoot: PROJECT, branch: 'pierre-diffs', detached: false, head: 'a1b2c3d' }), true
    }
    if (path === '/api/project/git/log') return json(route, { repo: true, commits: [] }), true

    // Cold-tab hydration reads the body as TEXT, not JSON — the shared map's
    // catch-all would hand it a JSON `[]` and the viewer would render "[]".
    if (path === '/api/file-read') {
      const body = FILE_CONTENT[q]
      return route.fulfill(body != null
        ? { status: 200, contentType: 'text/plain; charset=utf-8', body }
        : { status: 404, contentType: 'text/plain', body: 'not found' }), true
    }
    if (path === '/api/file-diff') {
      return json(route, q === MD_PATH
        ? { diff: MD_DIFF, original: MD_ORIGINAL, status: 'modified' }
        : { diff: '', original: '', status: 'clean' }), true
    }
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  const wrote = []

  /**
   * A frame is only saved once the SURFACE it names has demonstrably rendered.
   *
   * Two independent gates, because they catch different failures:
   *
   *  1. BEFORE the write — every probe (a Playwright locator, so it pierces
   *     Pierre's shadow roots) must resolve to a visible node with non-empty
   *     text. This is what catches "the panel is up but focus landed on another
   *     tab" and "the route stub 404'd so the viewer is an empty state". The
   *     text found is recorded and reported per frame, so a green run is
   *     auditable rather than merely exit-0.
   *  2. AFTER the write — bytes per pixel. A PNG of a blank surface compresses
   *     to almost nothing, so a frame under MIN_MBPP milli-bytes/pixel is
   *     reported as a FAILURE even though it exists on disk.
   *
   * A failed probe throws rather than saving a lookalike: the point of these
   * frames is evidence, and a frame nobody can trust is worse than a missing one.
   */
  const MIN_MBPP = 15

  /** Assert each probe is visible with non-empty text; return what was found.
   *  A probe may name an ATTRIBUTE instead of text (`attr`) — an icon-only
   *  button or an `<input>` carries no innerText, so demanding text there would
   *  fail on a perfectly rendered control. */
  async function assertRendered(name, probes) {
    const found = []
    for (const { selector, locator, min = 1, attr } of probes) {
      const count = await locator.count()
      if (count < min) {
        throw new Error(`frame ${name}: probe \`${selector}\` matched ${count} node(s), need >= ${min} — surface did not render; fix the fixture, do not save the frame`)
      }
      const texts = []
      for (let i = 0; i < Math.min(count, min + 2); i++) {
        const v = attr
          ? await locator.nth(i).getAttribute(attr).catch(() => null)
          : await locator.nth(i).innerText().catch(() => '')
        const t = (v || '').trim()
        if (t) texts.push(`${attr ? `${attr}=` : ''}${t.replace(/\s+/g, ' ').slice(0, 70)}`)
      }
      if (texts.length === 0) {
        throw new Error(`frame ${name}: probe \`${selector}\` matched ${count} node(s) but every one is EMPTY — blank surface; fix the fixture, do not save the frame`)
      }
      found.push({ selector, count, text: texts.join(' ⏐ ') })
    }
    return found
  }

  /** Record a written PNG: edge budget + blank-frame density gate. */
  function record(file, evidence) {
    const { w, h } = pngSize(file)
    const bytes = readFileSync(file).length
    const mbpp = Math.round((bytes * 1000) / (w * h))
    const over = w > MAX_EDGE || h > MAX_EDGE
    const blank = mbpp < MIN_MBPP
    console.log(`wrote ${file}  ${w}x${h}  ${bytes}B  ${mbpp} milli-bytes/px${over ? '  ⚠️ OVER 2000px' : ''}${blank ? `  ⚠️ LIKELY BLANK (< ${MIN_MBPP})` : ''}`)
    for (const e of evidence) console.log(`      asserted ${e.selector}  ×${e.count}  →  ${e.text}`)
    wrote.push({ file, w, h, bytes, mbpp, over, blank, evidence })
    if (blank) throw new Error(`frame ${file}: ${mbpp} milli-bytes/px is below the ${MIN_MBPP} blank-frame floor — re-shoot, do not ship`)
  }

  /** Element screenshot, gated on `probes` having rendered. `extra` carries
   *  already-collected evidence (the shadow-DOM tree assertion) into the log. */
  async function shot(locator, name, probes, extra = []) {
    const evidence = [...extra, ...await assertRendered(name, probes)]
    const file = `${OUT}/${name}.png`
    await locator.screenshot({ path: file })
    record(file, evidence)
  }

  /** Top-band clip of a locator — the alignment pair's framing. Same gates. */
  async function shotBand(locator, name, probes, extra = [], bandH = ALIGN_BAND_H) {
    const evidence = [...extra, ...await assertRendered(name, probes)]
    const box = await locator.boundingBox()
    const file = `${OUT}/${name}.png`
    await page.screenshot({ path: file, clip: { x: box.x, y: box.y, width: box.width, height: Math.min(bandH, box.height) } })
    record(file, evidence)
  }

  /** Probe helper: a labelled, shadow-piercing locator for `assertRendered`.
   *  Pass `attr` when the control's evidence is an attribute, not text. */
  const probe = (selector, locator, opts = {}) => ({ selector, locator, min: opts.min, attr: opts.attr })

  /**
   * Seed the persisted panel stores and load the dashboard.
   *
   * `?sid=` selects the slot deterministically (the localStorage restore key is
   * per-MODE — `mc-active-slot-chat` — so the bare `mc-active-slot` does
   * nothing). Everything else here exists to keep the frame clean:
   *   mc-theme / mc-onboarded            no first-run theme modal
   *   mc-activity-open:<slot>            the side panel is OPEN (chatSlice seeds
   *                                      its per-slot flag from this key)
   *   mc-panel-tabs:<slot>               the tab strip + which tab is active
   *   mc-files-rail-open / -w            rail shown, wide enough to read
   *   mc-side-panel-width                panel wide enough to hold viewer + rail
   *   kirocrew:comment-hint-dismissed    the markdown comment-tip banner would
   *                                      appear in PREVIEW only and shift the
   *                                      very geometry frames 12/13 compare
   *   mc-chat-config.pinLastPrompt=false the pinned-prompt banner floats over
   *                                      the transcript, not the panel, but it
   *                                      steals height on load
   *   mc-git-panel-opened:<slot>:<dir>   ChatPage creates a Git TAB
   *                                      UNCONDITIONALLY for any slot whose
   *                                      project dir is a repo — and focuses it,
   *                                      so without this marker every frame
   *                                      captured the Git panel instead of the
   *                                      seeded active tab. The marker is
   *                                      exactly the "already did this" flag
   *                                      that effect checks.
   */
  async function load(tabs, activeId, opts = {}) {
    await page.addInitScript(([slot, project, tabsJson, railOpen, panelW, railW, diffSplit]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', slot)
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-panel-tabs:' + slot, tabsJson)
      // '1'/'0', NOT 'true'/'false': every one of these is read through
      // `usePersistedBool`, whose getter is `v === '1'` — so a 'true' here reads
      // as FALSE and silently closes the rail / unsets the toggle. (The
      // `mc-activity-open:` key above is the exception: chatSlice persists it
      // with `String(open)` and compares against 'true'.)
      localStorage.setItem('mc-files-rail-open', railOpen)
      localStorage.setItem('mc-diff-split', diffSplit)
      localStorage.setItem('mc-file-linenums', '1')
      localStorage.setItem('mc-file-wordwrap', '1')
      localStorage.setItem('mc-file-collapse-unchanged', '0')
      localStorage.setItem('mc-files-rail-w', railW)
      localStorage.setItem('mc-side-panel-width', panelW)
      localStorage.setItem('kirocrew:comment-hint-dismissed', '1')
      localStorage.setItem('mc-git-panel-opened:' + slot + ':' + project, '1')
      localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
    }, [
      SLOT,
      PROJECT,
      bucket(tabs, activeId),
      opts.railOpen === false ? '0' : '1',
      String(opts.panelW ?? 820),
      String(opts.railW ?? 320),
      opts.diffSplit === false ? '0' : '1',
    ])
    await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  /* Deliberately NO post-load `Escape` / click-[aria-label="Close"] cleanup here,
   * unlike the chat harness. Both are destructive on THIS surface: MarkdownPanel
   * binds Escape to its own `guardedClose`, and a tab chip's close control is
   * itself labelled Close — so the pair silently closed the seeded file tab and
   * every frame captured whichever pinned tab focus fell back to. The
   * `mc-onboarded` + `mc-theme` seeds are what keep the first-run modal away. */

  /** The side panel's own root: the only element with `.side-panel-strip` as a
   *  direct child (see SidePanel's render). */
  const panel = () => page.locator('div:has(> .side-panel-strip)').last()

  /**
   * The rail's tree is asserted by reading its SHADOW ROOT's text, not with
   * locators. Three compounding reasons, each found the hard way:
   *
   *  - `@pierre/trees`' `FileTree` ignores the `className` it is handed (same as
   *    `PierreCode` / `PierrePatch`), so there is no `.pierre-tree` node to
   *    address.
   *  - every row renders inside a `<file-tree-container>` shadow root, so the
   *    panel's own `innerText` / `textContent` never sees a single filename.
   *  - a row name is SPLIT across nodes around a middle-truncation marker:
   *    `AGENTS.md` renders as `AGENTS.` + `…` + `md`, and `website` as
   *    `webs` + `…` + `ite`. So NO element contains a whole filename, and
   *    `getByText('AGENTS.md')` / `text=website` both match zero nodes even
   *    though the name is plainly on screen. That is why the earlier
   *    locator-based probes failed on a tree that had rendered perfectly.
   *
   * The root is open, so `host.shadowRoot.textContent` is readable. Dropping the
   * `…` markers rejoins the halves, which makes a plain `includes(name)` a true
   * statement about what the tree actually painted.
   */
  const treeTextRaw = () => page.evaluate(
    () => document.querySelector('file-tree-container')?.shadowRoot?.textContent ?? '',
  )
  /** Row names as one searchable string (truncation markers removed). */
  const treeNames = async () => (await treeTextRaw()).replace(/…/g, '')
  /** Readable sample for the log: Pierre paints each label twice (visible +
   *  measurement layer), so collapse doubled runs before showing it. */
  const sampleTree = (s) => s.replace(/:host\s*\{[^}]*\}/g, '').replace(/(.{3,40}?)\1/g, '$1').replace(/\s+/g, ' ').trim().slice(0, 220)

  /** Wait until the tree has painted a given row name. */
  const waitTreeText = async (name) => {
    await page.waitForFunction(
      n => (document.querySelector('file-tree-container')?.shadowRoot?.textContent ?? '')
        .replace(/…/g, '').includes(n),
      name,
      { timeout: 20000 },
    )
  }

  /** Assert the tree shows every named row; returns an evidence row for the log. */
  const assertTreeRows = async (frame, names) => {
    const text = await treeNames()
    const missing = names.filter(n => !text.includes(n))
    if (missing.length) {
      throw new Error(`frame ${frame}: the file tree is missing ${JSON.stringify(missing)} — rendered: "${sampleTree(text)}"; fix the fixture, do not save the frame`)
    }
    return {
      selector: 'file-tree-container.shadowRoot textContent',
      count: names.length,
      text: `${names.join(', ')} ⏐ rendered: ${sampleTree(text)}`,
    }
  }

  /** Assert a row is ABSENT — how the Changed filter is proven. */
  const assertNoTreeRow = async (frame, name) => {
    const text = await treeNames()
    if (text.includes(name)) {
      throw new Error(`frame ${frame}: "${name}" is an unchanged file and must not appear under the Changed filter — the filter did not apply`)
    }
  }

  const diag = async (label, fn) => console.log(`DIAG ${label}`, JSON.stringify(await page.evaluate(fn)))

  /** The rail's mode segment — `aria-label` is "File list mode", NOT "Tree mode"
   *  (`pages.chat.fileBrowserRail.tree_mode` in `en.manual.json`). Wrong guess
   *  silently returns null and every rail measurement reads as absent. */
  const RAIL_GROUP = '[aria-label="File list mode"]'

  /** In the tree fixture but NOT in the git-status fixture — so its presence
   *  proves ALL-FILES, and its absence proves the CHANGED filter really filters. */
  const ALL_ONLY_ROW = 'AGENTS.md'

  // ── Frame 10: the pinned Files tab, ALL FILES ──────────────────────────────
  await load([FILES_TAB], 'files')
  const p10 = panel()
  await p10.waitFor({ state: 'visible', timeout: 20000 })
  await waitTreeText(ALL_ONLY_ROW)
  await page.waitForTimeout(1200)
  await diag('files-all', () => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    return {
      allPressed: panelEl?.querySelector('[aria-label="All files"]')?.getAttribute('aria-pressed'),
      changedBadge: panelEl?.querySelector('[aria-label="Changed"]')?.textContent?.trim(),
      railGroup: !!panelEl?.querySelector('[aria-label="File list mode"]'),
    }
  })
  console.log('DIAG files-all tree text', JSON.stringify(sampleTree(await treeNames())))
  await shot(p10, '10-files-tab-all-files', [
    probe('FilesHomePanel empty-viewer hint', p10.getByText('Select a file from the tree to open it in a new tab')),
    probe('rail filter field placeholder', p10.locator('input[placeholder="Filter files…"]'), { attr: 'placeholder' }),
    probe('All-files segment aria-pressed=true', p10.locator('[aria-label="All files"][aria-pressed="true"]'), { attr: 'aria-pressed' }),
  ], [await assertTreeRows('10-files-tab-all-files', ['AGENTS.md', 'README.md', 'package.json', 'docs', 'website', 'kiro_crew'])])

  // ── Frame 11: the CHANGED filter ───────────────────────────────────────────
  // Module-level `sessionChangedMode` in FileBrowserRail is deliberately NOT
  // persisted (a fresh load always defaults to All files), so this is a click.
  await page.locator('[aria-label="Changed"]').first().click()
  await page.waitForTimeout(1600)
  await waitTreeText('capture-pierre-files-tab.mjs')
  await diag('files-changed', () => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    return {
      changedPressed: panelEl?.querySelector('[aria-label="Changed"]')?.getAttribute('aria-pressed'),
      noChangesEmptyState: panelEl?.textContent?.includes('No changes'),
    }
  })
  console.log('DIAG files-changed tree text', JSON.stringify(sampleTree(await treeNames())))
  /* The filter is only PROVEN by what it removed: AGENTS.md is in the tree
   * fixture and NOT in the git-status fixture, so its disappearance is the
   * assertion that Changed is actually filtering rather than re-rendering All. */
  await assertNoTreeRow('11-files-tab-changed', ALL_ONLY_ROW)
  await shot(panel(), '11-files-tab-changed', [
    probe('Changed segment aria-pressed=true', panel().locator('[aria-label="Changed"][aria-pressed="true"]'), { attr: 'aria-pressed' }),
  ], [await assertTreeRows('11-files-tab-changed', [
    'pierre-migration.md', 'MarkdownPanel.tsx', 'FileBrowserRail.tsx',
    'FilesHomePanel.tsx', 'PierreWorkspaceTreeImpl.tsx', 'capture-pierre-files-tab.mjs',
  ])])

  // ── Frames 12 + 13: PREVIEW vs SOURCE, top band, rail in frame ────────────
  await load([FILES_TAB, fileTab(false)], `file:${MD_PATH}`)
  const p12 = panel()
  await p12.waitFor({ state: 'visible', timeout: 20000 })
  // The prose only exists once cold-tab hydration has landed the content, so
  // wait on a phrase from the BODY — not the filename, which the tab chip and
  // the breadcrumb already carry before any content arrives.
  await page.getByText('The file-tab layout').first().waitFor({ state: 'visible', timeout: 20000 })
  await waitTreeText(ALL_ONLY_ROW)
  await page.waitForTimeout(1600)
  /* The measurement the pair exists to prove. `contentTop` is the top of the
   * viewer's SCROLL BOX and `railHeaderTop` the top of the rail's own header —
   * the two things a vertical inset on the shared flex row moves together. Both
   * are ordinary DOM (Pierre's shadow roots live inside the scroll box), so one
   * `evaluate` covers preview and source identically; the first painted LINE is
   * measured separately per mode, because in source mode it is a Pierre row
   * inside a shadow root and only a locator can reach it. */
  const geometry = () => page.evaluate(railGroup => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    const column = panelEl?.querySelector('[data-mc-mdpanel]')
    const scrollBox = column?.querySelector(':scope > div.overflow-auto')
    const railHeader = panelEl?.querySelector(railGroup)
    const y = el => (el ? Math.round(el.getBoundingClientRect().top) : null)
    return {
      panelTop: y(panelEl),
      panelW: panelEl ? Math.round(panelEl.getBoundingClientRect().width) : null,
      columnTop: y(column),
      contentTop: y(scrollBox),
      railHeaderTop: y(railHeader),
    }
  }, RAIL_GROUP)
  /** Top of the first painted line of the file, through a shadow-piercing locator. */
  const firstLineTop = async (locator) => {
    const box = await locator.first().boundingBox().catch(() => null)
    return box ? Math.round(box.y) : null
  }

  const gPreview = { ...await geometry(), firstLineTop: await firstLineTop(p12.locator('h1')) }
  console.log('DIAG geometry-preview', JSON.stringify(gPreview))
  await shotBand(p12, '12-file-preview-aligned', [
    // `h1` exists only in the RENDERED markdown — its presence is what tells
    // preview mode apart from source, where the same line is literal `# …` text.
    probe('rendered markdown h1', p12.locator('h1')),
    probe('rendered table cell text=PierrePatch', p12.getByText('PierrePatch', { exact: false })),
    probe('prose heading text=The file-tab layout', p12.getByText('The file-tab layout')),
    probe('Edit toggle (so we are in PREVIEW)', p12.getByRole('button', { name: 'Edit' })),
  ], [await assertTreeRows('12-file-preview-aligned', ['AGENTS.md', 'website'])])

  await page.locator('button:has-text("Edit")').first().click()
  await page.waitForTimeout(1800)
  const gSource = { ...await geometry(), firstLineTop: await firstLineTop(page.getByText('# Pierre migration', { exact: false })) }
  console.log('DIAG geometry-source', JSON.stringify(gSource))
  await shotBand(panel(), '13-file-source-aligned', [
    // The literal `# Pierre migration` (hash included) only exists as text in
    // SOURCE mode; preview renders it as an <h1> with the marker consumed.
    probe('literal source line text="# Pierre migration"', page.getByText('# Pierre migration', { exact: false })),
    probe('literal source text="## Surfaces"', page.getByText('## Surfaces', { exact: false })),
    probe('Preview toggle (so we are in SOURCE)', panel().getByRole('button', { name: 'Preview' })),
  ], [await assertTreeRows('13-file-source-aligned', ['AGENTS.md', 'website'])])
  const delta = k => (gPreview[k] == null || gSource[k] == null ? null : gPreview[k] - gSource[k])
  console.log('DIAG alignment-delta preview-minus-source', JSON.stringify({
    columnTop: delta('columnTop'),
    contentTop: delta('contentTop'),
    firstLineTop: delta('firstLineTop'),
    railHeaderTop: delta('railHeaderTop'),
  }))

  // ── Frame 14: DIFF mode ────────────────────────────────────────────────────
  await load([FILES_TAB, fileTab(true)], `file:${MD_PATH}`)
  const p14 = panel()
  await p14.waitFor({ state: 'visible', timeout: 20000 })
  /* A REMOVED line — it exists only in the HEAD baseline, never in the buffer —
   * so seeing it is what proves the diff canvas rendered BOTH sides, rather than
   * having fallen back to the plain file view or to `ZeroDiffNotice`. */
  await page.getByText('The worker bundle is 1.1 MB', { exact: false }).first()
    .waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(2000)
  await diag('diff', () => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    return {
      diffPressed: panelEl?.querySelector('[aria-label="Toggle diff view"]')?.getAttribute('aria-pressed'),
      pierreSurface: !!panelEl?.querySelector('.pierre-surface'),
      zeroDiffNotice: !!panelEl?.textContent?.includes('No changes in file'),
      stats: [...(panelEl?.querySelectorAll('.text-ok, .text-danger') ?? [])].map(e => e.textContent?.trim()).slice(0, 4),
    }
  })
  await shot(p14, '14-file-diff-mode', [
    probe('removed line (HEAD-only) text="The worker bundle is 1.1 MB"', page.getByText('The worker bundle is 1.1 MB', { exact: false })),
    probe('added line (buffer-only) text="@pierre/diffs` instead of Monaco"', page.getByText('instead of Monaco', { exact: false })),
    probe('diff toggle aria-pressed=true', p14.locator('[aria-label="Toggle diff view"][aria-pressed="true"]'), { attr: 'aria-pressed' }),
  ], [await assertTreeRows('14-file-diff-mode', ['AGENTS.md', 'website'])])

  // ── Frame 15: the unsaved-changes banner ──────────────────────────────────
  // `dirty` is only ever set by a real edit through the editor's onChange
  // (FileTabBody passes no `savedBaseline`), so this types into Pierre's editor.
  await load([FILES_TAB, fileTab(false)], `file:${MD_PATH}`)
  const p15 = panel()
  await p15.waitFor({ state: 'visible', timeout: 20000 })
  await page.getByText('The file-tab layout').first().waitFor({ state: 'visible', timeout: 20000 })
  await waitTreeText(ALL_ONLY_ROW)
  await page.waitForTimeout(1500)
  await page.locator('button:has-text("Edit")').first().click()
  await page.getByText('# Pierre migration', { exact: false }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1200)
  // Put the caret at the end of the file's first line and type.
  await page.getByText('# Pierre migration', { exact: false }).first().click()
  await page.keyboard.press('End')
  await page.keyboard.type(' (Pierre)')
  await page.waitForTimeout(1400)
  /* The banner's inner 40px row is in the DOM even when the file is clean — the
   * grid wrapper animates `grid-template-rows` 0fr→1fr and clips it — so a
   * locator alone would report it "visible" on a pristine file. The wrapper's
   * measured HEIGHT is the only honest test, and its geometry vs the rail is
   * the claim the frame makes: banner confined to the preview column, rail
   * neither overlapped nor pushed down. */
  const bannerGeom = await page.evaluate(railGroup => {
    const panelEl = document.querySelector('div > .side-panel-strip')?.parentElement
    const column = panelEl?.querySelector('[data-mc-mdpanel]')
    const wrapper = column?.querySelector(':scope > div.grid')
    const railGroupEl = panelEl?.querySelector(railGroup)
    const railColumn = railGroupEl?.closest('div[style]')
    const r = el => { if (!el) return null; const b = el.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) } }
    return {
      wrapperH: wrapper ? Math.round(wrapper.getBoundingClientRect().height) : null,
      wrapper: r(wrapper), column: r(column), railColumn: r(railColumn), railGroup: r(railGroupEl),
      panel: r(panelEl),
    }
  }, RAIL_GROUP)
  console.log('DIAG unsaved', JSON.stringify(bannerGeom))
  if (!bannerGeom.wrapperH || bannerGeom.wrapperH < 20) {
    throw new Error(`frame 15: the unsaved-changes wrapper measured ${bannerGeom.wrapperH}px — the edit never marked the buffer dirty, so the banner is still collapsed; fix the typing step, do not save the frame`)
  }
  // The claim in words: the banner must not reach into the rail's column.
  if (bannerGeom.railColumn && bannerGeom.wrapper
      && bannerGeom.wrapper.x + bannerGeom.wrapper.w > bannerGeom.railColumn.x + 8) {
    throw new Error('frame 15: the banner overlaps the rail column — it must be confined to the preview column')
  }
  await shot(p15, '15-unsaved-banner', [
    probe('banner label text="Unsaved changes"', p15.getByText('Unsaved changes')),
    probe('banner Save button', p15.getByRole('button', { name: 'Save' })),
    probe('banner Cancel button', p15.getByRole('button', { name: 'Cancel' })),
  ], [await assertTreeRows('15-unsaved-banner', ['AGENTS.md', 'website'])])

  // ── Frame 16: the ⋯ overflow menu, view-options group ──────────────────────
  await load([FILES_TAB, fileTab(false)], `file:${MD_PATH}`)
  const p16 = panel()
  await p16.waitFor({ state: 'visible', timeout: 20000 })
  await page.getByText('The file-tab layout').first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1400)
  await page.locator('[data-testid="markdown-panel-more-options"]').first().click()
  await page.locator('[role="menu"]').first().waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(700)
  await diag('overflow', () => {
    const menu = document.querySelector('[role="menu"]')
    return {
      open: !!menu,
      rows: [...(menu?.querySelectorAll('[role="menuitem"], [role="menuitemcheckbox"]') ?? [])].map(e => e.textContent?.trim()),
      dividers: menu?.querySelectorAll('div.h-px.bg-border').length ?? -1,
    }
  })
  await shot(p16, '16-overflow-menu', [
    probe('menu row text=Refresh', page.locator('[role="menu"]').getByText('Refresh', { exact: false })),
    probe('menu row text=Full screen', page.locator('[role="menu"]').getByText('Full screen', { exact: false })),
    probe('menu checkbox row text=Word wrap', page.locator('[role="menuitemcheckbox"]', { hasText: 'Word wrap' })),
    probe('menu checkbox row text=Line numbers', page.locator('[role="menuitemcheckbox"]', { hasText: 'Line numbers' })),
    probe('menu checkbox row text=Collapse unchanged', page.locator('[role="menuitemcheckbox"]', { hasText: 'Collapse unchanged' })),
  ])

  console.log('\n── SUMMARY ─────────────────────────────')
  for (const w of wrote) {
    console.log(`${w.over || w.blank ? 'FAIL' : ' ok '}  ${w.w}x${w.h}  ${String(w.mbpp).padStart(4)} mB/px  ${w.file}`)
    for (const e of w.evidence) console.log(`        ${e.selector} → ${e.text}`)
  }
  const bad = wrote.filter(w => w.over || w.blank)
  console.log(bad.length
    ? `FAIL: ${bad.length} frame(s) over ${MAX_EDGE}px or below the ${MIN_MBPP} mB/px blank floor`
    : `all ${wrote.length} frames within ${MAX_EDGE}px and above the ${MIN_MBPP} mB/px blank floor`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
