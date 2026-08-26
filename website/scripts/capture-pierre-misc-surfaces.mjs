/**
 * Screenshot harness for the REMAINING Pierre-migration surfaces — the ones the
 * chat-focused `capture-pierre-chat-diffs.mjs` does not reach: the renamed
 * links-only side-panel tab, the `+` menu that now offers it, the pull-request
 * panel's per-file diff, the tool-input patch preview, and Papyrus's `.tex`
 * editor.
 *
 * Same gateway-free harness as every other script in this folder: the REAL built
 * SPA (website/dist) behind the shared in-process static server, with every
 * /api/** call answered from fixtures via Playwright route interception (no
 * kiro-cli, no live backend, no token). The client code under test is unmodified.
 *
 * Frames:
 *   20-links-tab-populated  the LINKS tab (renamed from the old Files tab and now
 *                           links-only) with eight resource rows — pull requests,
 *                           issues and docs — plus its label/URL search box, which
 *                           only mounts past five rows.
 *   21-links-tab-empty      the same tab in a session that referenced no URL.
 *   22-side-panel-add-menu  the `+` menu open, showing Links as an UNPINNED entry
 *                           beside Issues / Subagents / Workflows / Git. Changes,
 *                           Files and Artifacts are auto-pinned (PINNED_VIEWS) and
 *                           are deliberately absent from the menu — they are the
 *                           three chips already in the strip behind it.
 *   23-pr-panel-diff        the Changes tab's PullRequestPanel with one changed
 *                           file expanded, so the provider's per-file patch renders
 *                           through Pierre (PullRequestPanel.DiffView synthesizes
 *                           `diff --git`/`---`/`+++` headers around the hunk body).
 *   24-tool-input-diff      a tool call whose INPUT is a patch: ToolDetails →
 *                           PayloadView → ToolInputText trips `isDiffText` and
 *                           renders PierrePatch with the compact inline options
 *                           (no line numbers, `simple` hunk separators, wrapped).
 *   25-papyrus-tex          Papyrus with a `.tex` document open in PierreEditor.
 *                           Papyrus passes only a FILENAME (PapyrusEditor.tsx:70-73)
 *                           and Pierre derives the language itself
 *                           (FileRenderer.js:375), whose extension map sends
 *                           tex/ltx/sty/cls to the Shiki `tex` grammar. Both
 *                           `tex-*.js` and `latex-*.js` chunks ship in dist; the
 *                           frame is evidence of WHICH scoping the editor gets —
 *                           the plainer TeX grammar, not the richer `latex` one the
 *                           deleted Monarch grammar approximated.
 *
 * Frames are ELEMENT (or clipped) screenshots, never full-page: at
 * deviceScaleFactor 2 a 1440x900 viewport is 2880x1800, well over the 2000px-per-
 * edge budget for PR media. Every write asserts both edges.
 *
 * Usage: node scripts/capture-pierre-misc-surfaces.mjs [outDir]
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

/** Hard ceiling for a PR-attached PNG, on BOTH edges. */
const MAX_EDGE = 2000

/** Panel width seeded into mc-side-panel-width: wide enough for a readable diff,
 *  still inside the responsive clamp (viewport 1440 − SIDE_PANEL_RESERVED_W 560). */
const PANEL_W = 620

mkdirSync(OUT, { recursive: true })

// ── Fixtures ────────────────────────────────────────────────────────────────

/* The Links tab is fed by `extractChatLinks(messages)` (ChatPage →
 * useChatNavigation → navLinks), and it HIDES any link that already owns a rich
 * panel — the Changes tab's `sources` and the Issues tab's `issues`. Those two
 * lists only take links whose FIRST mention came from the AGENT
 * (pullRequestLinks.emitChangeSources), so a pull request or issue the USER
 * pasted stays a Resource. Hence: the PR/issue rows below live in a USER
 * message, and only the doc links are agent-surfaced.
 *
 * All eight are markdown links, so `fromMarkdown` is set and useChatNavigation
 * never calls /api/nav-links to resolve a label — the labels in the frame are
 * exactly the fixture's. Eight rows also clears LinksTab's `> 5` threshold, so
 * the search box is part of the frame. */
const USER_LINKS = [
  'Before you start, context on the migration:',
  '',
  '- [PR #843 — Replace Monaco with Pierre](https://github.com/kirodotdev/KiroCrew/pull/843)',
  '- [MR !12 — Pierre worker pool sizing](https://gitlab.com/kiro/dashboard/-/merge_requests/12)',
  '- [Issue #2418 — Monaco worker 404s on file open](https://github.com/kirodotdev/KiroCrew/issues/2418)',
  '- [Issue #77 — .tex highlighting regressed after the swap](https://gitlab.com/kiro/dashboard/-/issues/77)',
].join('\n')

const AGENT_LINKS = [
  'Read through the references before the first commit:',
  '',
  '- [Shiki bundled language list](https://shiki.style/languages)',
  '- [@pierre/diffs option reference](https://docs.pierre.co/diffs/options)',
  '- [Architecture overview](https://github.com/kirodotdev/KiroCrew/blob/main/docs/architecture/overview.md)',
  '- [dashboard-iframe-hosts.md](https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/dashboard-iframe-hosts.md)',
].join('\n')

/* Frame 23's source. An ASSISTANT mention makes this a Changes source (and, being
 * a source, it is excluded from the Links tab — which is why frame 20 uses a
 * different, user-pasted pull request). */
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/861'

/* Frame 24's tool input: a bare patch, NOT JSON. PayloadView's
 * `tryParseJsonObject` declines it, so it reaches ToolInputText verbatim, where
 * `isDiffText` matches the `@@` hunk headers and hands it to PierrePatch. The
 * tool message carries `input` but NO `output`, so ToolDetails' section state
 * defaults to `input` (it prefers `output` whenever one exists) and the diff is
 * the pane on screen without a second click. */
const TOOL_PATCH = [
  '--- a/website/src/pierre/config.ts',
  '+++ b/website/src/pierre/config.ts',
  '@@ -8,10 +8,14 @@',
  " import type { BaseDiffOptions, ThemesType } from '@pierre/diffs'",
  ' ',
  "-export const MONACO_THEMES = { dark: 'vs-dark', light: 'vs' }",
  "-export const MONACO_WORKER_URL = '/assets/monaco/editor.worker.js'",
  "+export const PIERRE_THEMES: ThemesType = { dark: 'pierre-dark', light: 'pierre-light' }",
  '+',
  "+export const PIERRE_COMPACT_HEADER_CSS = `",
  '+[data-diffs-header]{--diffs-gap-block:6px;font-size:12px;line-height:18px}',
  '+[data-change-icon]{width:13px;height:13px}',
  '+`',
  '+',
  '+export const PIERRE_WORKER_POOL_SIZE = 4',
  ' ',
  ' export function pierreThemeType(isDark: boolean) {',
  "   return isDark ? 'dark' : 'light'",
  ' }',
].join('\n')

/* Frame 25's document. `\documentclass`, a `\section{}`, inline `$math$` and a
 * `%` comment are all present on purpose: they are exactly the constructs the
 * deleted Monarch grammar scoped and the ones whose treatment under the Shiki
 * `tex` grammar the frame has to show. */
const TEX_DOC = [
  '% Draft — measurement section. Do not cite yet.',
  '\\documentclass[11pt,twocolumn]{article}',
  '\\usepackage{amsmath}',
  '\\usepackage{graphicx}',
  '',
  '\\title{Rendering Diffs Without a Monaco Worker}',
  '\\author{K. Hasman}',
  '',
  '\\begin{document}',
  '\\maketitle',
  '',
  '\\section{Introduction}',
  'The editor previously shipped a worker bundle per language. We replace it with',
  'a shared highlighter, so the marginal cost of a language is a grammar rather',
  'than a worker \\cite{shiki2024}.',
  '',
  '\\section{Cost model}',
  'Let $n$ be the number of open diff surfaces and $g$ the grammars they need.',
  'The old cost was $O(n \\cdot g)$ workers; the new cost is $O(g)$ grammars and',
  'a single pool, so',
  '\\begin{equation}',
  '  T_{\\text{paint}} = \\alpha g + \\frac{\\beta n}{p}, \\qquad p = 4.',
  '\\end{equation}',
  '',
  '\\subsection{Measured paint time}',
  '% TODO: rerun on the 4-core box before submission',
  'Median first paint fell from $410$ms to $96$ms across $n = 12$ surfaces.',
  '',
  '\\end{document}',
].join('\n')

const t0 = Math.floor(Date.now() / 1000) - 1200

/** One slot per surface: a short transcript per frame keeps the virtualized
 *  transcript out of the way of an element screenshot. */
const SURFACES = {
  links: {
    key: 'pierre-links-populated',
    title: 'Pierre migration — references',
    messages: [
      { role: 'user', content: USER_LINKS, ts: String(t0) },
      { role: 'assistant', content: AGENT_LINKS, ts: String(t0 + 90) },
    ],
  },
  linksEmpty: {
    key: 'pierre-links-empty',
    title: 'Pierre config defaults',
    messages: [
      { role: 'user', content: 'What are the Pierre diff defaults now?', ts: String(t0) },
      {
        role: 'assistant',
        ts: String(t0 + 40),
        content: 'Unified layout, gutter change bars, word-level intra-line diffing, and unchanged regions folded. Nothing in this session referenced a URL.',
      },
    ],
  },
  pr: {
    key: 'pierre-pr-changes',
    title: 'Pull request panel',
    messages: [
      { role: 'user', content: 'Open the PR you pushed for the worker-pool change.', ts: String(t0) },
      {
        role: 'assistant',
        ts: String(t0 + 60),
        // Agent-surfaced → a Changes source, which is what mounts PullRequestPanel.
        content: `Pushed and opened as [PR #861](${PR_URL}) — one file, the Pierre worker pool size.`,
      },
    ],
  },
  tool: {
    key: 'pierre-tool-input-diff',
    title: 'Tool input patch preview',
    messages: [
      { role: 'user', content: 'Apply the config patch.', ts: String(t0) },
      {
        role: 'tool',
        content: '🔧 Editing website/src/pierre/config.ts',
        ts: String(t0 + 30),
        meta: {
          tool_call_id: 'tc-pierre-config',
          purpose: 'swap the Monaco theme constants for the Pierre ones',
          // Persisted tool I/O: ToolCallLine's no-toolLog branch reads
          // meta.input / meta.output straight off the message.
          input: TOOL_PATCH,
        },
      },
    ],
  },
}

const slots = Object.values(SURFACES).map(s => ({
  key: s.key,
  title: s.title,
  running: false,
  last_message: s.title,
  messages: s.messages.length,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}))

const detailByKey = Object.fromEntries(Object.values(SURFACES).map(s => [
  s.key,
  { running: false, has_more: false, total: s.messages.length, queue: [], messages: s.messages },
]))

// ── Pull-request fixture (frame 23) ─────────────────────────────────────────

/* The provider hands back a per-file patch body — hunks only, no `diff --git` /
 * `---` / `+++`. That is deliberate here: DiffView synthesizes those headers
 * before handing the text to Pierre, so a fixture that already carried them
 * would not exercise the real path. */
const PR_FILE_PATCH = [
  '@@ -14,7 +14,12 @@',
  ' export function pierreThemeType(isDark: boolean) {',
  "   return isDark ? 'dark' : 'light'",
  ' }',
  ' ',
  '-const WORKER_POOL_SIZE = 1',
  '+/** Highlighter workers shared by every Pierre surface on the page. Four is the',
  '+ *  point past which a 12-surface transcript stops queueing on paint. */',
  '+export const PIERRE_WORKER_POOL_SIZE = 4',
  '+',
  '+export function pierreWorkerPool(): WorkerPool {',
  '+  return getOrCreatePool(PIERRE_WORKER_POOL_SIZE)',
  '+}',
  ' ',
  ' export const PIERRE_CODE_DEFAULTS: BaseCodeOptions = {',
  "   themes: PIERRE_THEMES,",
].join('\n')

const PR_SOURCE = {
  provider: 'github',
  url: PR_URL,
  number: 861,
  title: 'Share one Pierre highlighter pool across diff surfaces',
  description: 'Every diff surface was spinning up its own highlighter. One pool of four workers, shared.',
  state: 'open',
  draft: false,
  mergedAt: '',
  updatedAt: new Date(Date.now() - 25 * 60 * 1000).toISOString(),
  headBranch: 'pierre-worker-pool',
  baseBranch: 'main',
  headSha: '9f31c0ae4b2d7c5188aa3e6f0b41d92c7ae55031',
  author: 'kiro-dev',
  additions: 9,
  deletions: 1,
  changedFiles: 2,
  mergeable: 'mergeable',
  mergeStateStatus: 'clean',
  autoMerge: false,
  commits: [{
    sha: '9f31c0ae4b2d7c5188aa3e6f0b41d92c7ae55031',
    title: 'Share one Pierre highlighter pool across diff surfaces',
    body: '',
    author: 'kiro-dev',
    date: new Date(Date.now() - 40 * 60 * 1000).toISOString(),
    url: `${PR_URL}/commits/9f31c0a`,
  }],
  checks: [{
    name: 'build', workflow: 'ci', status: 'completed', conclusion: 'success',
    bucket: 'passed', url: `${PR_URL}/checks`, startedAt: '', completedAt: '',
  }],
  comments: [],
  files: [
    { path: 'website/src/pierre/config.ts', status: 'modified', additions: 9, deletions: 1, patch: PR_FILE_PATCH },
    {
      path: 'website/src/pierre/PierreImpl.tsx', status: 'modified', additions: 3, deletions: 3,
      patch: [
        '@@ -208,9 +208,9 @@',
        ' export function PierreCodeImpl({ file, options, className, langHint }: Props) {',
        '-  const pool = useMemo(() => createPool(1), [])',
        '+  const pool = pierreWorkerPool()',
        ' ',
        '   return <FileRenderer file={file} options={resolved} pool={pool} />',
        ' }',
      ].join('\n'),
    },
  ],
}

/** The file row expanded for frame 23. */
const PR_EXPAND_PATH = 'website/src/pierre/config.ts'

// ── Papyrus fixture (frame 25) ──────────────────────────────────────────────

const PAPYRUS_PROJECT = 'pierre-tex-evidence'
const PAPYRUS_MAIN = 'main.tex'
const PAPYRUS_FILES = {
  [PAPYRUS_MAIN]: TEX_DOC,
  'sections/measurement.tex': '\\section{Measurement}\n% filled in later\n',
  'refs.bib': '@article{shiki2024,\n  title = {Shiki},\n  year = {2024},\n}\n',
}

// ── Harness ─────────────────────────────────────────────────────────────────

/** PNG width/height straight out of the IHDR chunk — no image dependency. */
function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

/**
 * Chromium to drive.
 *
 * `website/node_modules/playwright` pins one browser revision, but this machine's
 * `~/.cache/ms-playwright` may only hold builds fetched by a DIFFERENT playwright
 * (the globally installed `@playwright/cli`, say) — in which case a bare
 * `chromium.launch()` dies with "Executable doesn't exist at …chromium_headless_shell-<pinned>".
 * So: honour `PLAYWRIGHT_CHROMIUM` if set, else fall back to the newest cached
 * headless shell, else let playwright resolve its own pin and report its own error.
 * (Same resolution as capture-pierre-chat-diffs.mjs.)
 */
function chromiumExecutable() {
  if (process.env.PLAYWRIGHT_CHROMIUM) return process.env.PLAYWRIGHT_CHROMIUM
  const cache = join(homedir(), '.cache', 'ms-playwright')
  if (!existsSync(cache)) return undefined
  const rev = d => parseInt((/-(\d+)$/.exec(d) || [])[1] || '0', 10)
  const candidates = readdirSync(cache)
    .filter(d => d.startsWith('chromium_headless_shell-') || d.startsWith('chromium-'))
    .sort((a, b) => rev(b) - rev(a))
    .map(d => [
      join(cache, d, 'chrome-headless-shell-linux64', 'chrome-headless-shell'),
      join(cache, d, 'chrome-linux64', 'chrome'),
      join(cache, d, 'chrome-linux', 'chrome'),
    ])
    .flat()
  return candidates.find(existsSync)
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    // 11-13px panel type and Pierre's 12px diff rows render soft at 1x on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  // Pierre's component is a React.lazy chunk while its CSS ships in the main
  // bundle, so a chunk that fails to load yields a mounted-but-empty surface
  // rather than an error. Surface that instead of screenshotting the hole.
  page.on('console', m => {
    if (m.type() === 'error') console.log('CONSOLE-ERR', m.text().slice(0, 300))
  })
  page.on('requestfailed', r =>
    console.log('REQ-FAIL', r.failure()?.errorText, r.url().slice(-90)))
  page.on('response', r => {
    if (r.status() >= 400) console.log('HTTP', r.status(), r.url().slice(-90))
  })

  /** Fixture routes this harness owns, consulted before the shared boot map. */
  const extra = async (path, route) => {
    const url = new URL(route.request().url())

    if (path === '/api/chat/slots') return json(route, slots), true
    const slotMatch = /^\/api\/chat\/slots\/([^/]+)/.exec(path)
    if (slotMatch) {
      const d = detailByKey[decodeURIComponent(slotMatch[1])]
      if (d) return json(route, d), true
    }
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true

    // ── Pull request (frame 23) ──
    // Exact match first: /status and /checks share this prefix.
    if (path === '/api/source/pull-request') return json(route, PR_SOURCE), true
    if (path === '/api/source/pull-request/status') {
      return json(route, {
        statuses: { [PR_URL]: { state: 'open', ci: 'passed', mergeable: 'mergeable', mergeStateStatus: 'clean' } },
        refreshing: [],
        ttlSecs: 120,
      }), true
    }
    if (path === '/api/source/pull-request/checks') return json(route, { checks: PR_SOURCE.checks }), true

    // ── Papyrus (frame 25) ──
    if (path === '/api/apps/papyrus/health') {
      return json(route, {
        status: 'ok', compiler: 'tectonic', git: true,
        managed: {
          supported: true, installed: true, release: 'v0.15.0', version: '0.15.0',
          job: { state: 'done', error: '', attempt: 1, bytes_downloaded: 0, bytes_total: 0, elapsed: 0 },
        },
      }), true
    }
    if (path === '/api/apps/papyrus/projects') {
      return json(route, {
        projects: [{ name: PAPYRUS_PROJECT, modified: Math.floor(Date.now() / 1000) - 600, has_pdf: false }],
      }), true
    }
    if (path === '/api/apps/papyrus/project') {
      return json(route, {
        name: PAPYRUS_PROJECT, main_file: PAPYRUS_MAIN,
        files: Object.keys(PAPYRUS_FILES), has_pdf: false,
      }), true
    }
    if (path === '/api/apps/papyrus/file') {
      const p = url.searchParams.get('path') || PAPYRUS_MAIN
      const body = PAPYRUS_FILES[p]
      // A fixture miss used to fall back to '' and paint an empty editor that
      // looked like a Pierre failure. Say so loudly instead.
      if (body === undefined) {
        console.log('FIXTURE-MISS /file path=' + JSON.stringify(p),
          'known=' + JSON.stringify(Object.keys(PAPYRUS_FILES)))
      }
      console.log('SERVE /file path=' + p, 'len=' + (body ?? '').length)
      return json(route, { path: p, content: body ?? '' }), true
    }
    if (path === '/api/apps/papyrus/git') return json(route, { is_git: false }), true

    // DiffBlock / file chips HEAD-probe this before offering an Open control.
    if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  const wrote = []

  /** Record + assert a written PNG against the per-edge budget. */
  function record(file) {
    const { w, h } = pngSize(file)
    const over = w > MAX_EDGE || h > MAX_EDGE
    console.log(`wrote ${file}  ${w}x${h}${over ? '  ⚠️ OVER 2000px' : ''}`)
    wrote.push({ file, w, h, over })
  }

  /** Element screenshot. */
  async function shot(locator, name) {
    const file = `${OUT}/${name}.png`
    await locator.screenshot({ path: file })
    record(file)
  }

  /** Viewport-clip screenshot, for a portalled overlay that no single element
   *  encloses (the `+` menu lives in a body-level Radix portal). */
  async function clipShot(clip, name) {
    const file = `${OUT}/${name}.png`
    await page.screenshot({ path: file, clip })
    record(file)
  }

  /**
   * Load a chat surface with its side panel already open on a chosen tab.
   *
   * Everything here is a localStorage pre-seed rather than a click, because each
   * piece of state has its own persisted key and the panel reads them on mount:
   *   mc-activity-open:<slot>   the panel's open/closed flag (chatSlice seeds
   *                             slotActivity from it — see loadActivityOpenMap)
   *   mc-panel-tabs:<slot>      the tab strip bucket {activeId, tabs}
   *                             (usePanelTabs.loadPersisted)
   *   mc-side-panel-width       the panel's width, so frames are reproducible
   *
   * `syncPinned(PINNED_VIEWS)` runs unconditionally on SidePanel mount, so the
   * strip always ends up as [Changes, Files, Artifacts, …seeded dynamic tabs] —
   * seeding only the dynamic tab is enough, and the seeded activeId survives
   * because syncPinned keeps a focused tab that still exists.
   *
   * The slot is selected by the `?sid=` deep link, which ChatPage activates on
   * mount; the per-mode restore key (`mc-active-slot-chat`) is seeded too as a
   * fallback. `pinLastPrompt` is off because the pinned-prompt banner floats over
   * the top of the transcript.
   */
  async function load(surface, { tabs = [], activeId = null } = {}) {
    await page.addInitScript(([key, tabList, active, panelWidth]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', key)
      localStorage.setItem('mc-chat-config', JSON.stringify({
        pinLastPrompt: false,
        streamMode: 'immediate',
      }))
      localStorage.setItem('mc-side-panel-width', String(panelWidth))
      if (active) {
        localStorage.setItem(`mc-activity-open:${key}`, 'true')
        localStorage.setItem(`mc-panel-tabs:${key}`, JSON.stringify({ activeId: active, tabs: tabList }))
      }
    }, [surface.key, tabs, activeId, PANEL_W])
    await page.goto(base + '/?sid=' + encodeURIComponent(surface.key), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    // Belt and braces over the localStorage seed, the way the pod-e2e runner
    // dismisses first-run modals.
    await page.keyboard.press('Escape')
    const close = page.locator('[aria-label="Close"]')
    if (await close.count()) await close.first().click().catch(() => {})
    await page.waitForTimeout(400)
  }

  /** The side panel's root: the only element with the tab strip as a child. */
  const panel = () => page.locator('div:has(> div.side-panel-strip)').first()

  // ── Frame 20: the LINKS tab, populated ────────────────────────────────────
  await load(SURFACES.links, {
    activeId: 'links',
    tabs: [{ id: 'links', kind: 'links', title: 'Links' }],
  })
  await panel().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForFunction(
    () => document.querySelectorAll('a[href^="http"][title^="http"]').length >= 8,
    null,
    { timeout: 15000 },
  )
  await page.waitForTimeout(600)
  console.log('DIAG links', JSON.stringify(await page.evaluate(() => ({
    tabs: [...document.querySelectorAll('[role="tab"]')].map(e => e.textContent?.trim()),
    rows: [...document.querySelectorAll('a[title^="http"]')].map(e => e.textContent?.trim().slice(0, 48)),
    search: document.querySelector('input[aria-label^="Search"]')?.getAttribute('placeholder') || null,
  }))))
  await shot(panel(), '20-links-tab-populated')

  // ── Frame 22: the `+` menu (same slot — its strip already has the Links tab,
  //     so the frame shows Links offered in the menu while Changes/Files/
  //     Artifacts sit pinned in the strip behind it) ───────────────────────────
  await page.locator('button[aria-label="Open side panel tab"]').click()
  await page.waitForSelector('[role="menu"]', { timeout: 8000 })
  await page.waitForTimeout(500)
  console.log('DIAG menu', JSON.stringify(await page.evaluate(() => ({
    items: [...document.querySelectorAll('[role="menuitem"]')].map(e => e.textContent?.trim().slice(0, 40)),
    separators: document.querySelectorAll('[role="menu"] [role="separator"]').length,
  }))))
  {
    const pbox = await panel().boundingBox()
    const menu = page.locator('[role="menu"]').first()
    const mbox = await menu.boundingBox()
    // Union of the panel strip region and the portalled menu, clamped to the
    // viewport: the menu is a body-level portal, so no single element encloses
    // both and an element screenshot of either alone loses the point.
    const x = Math.max(0, Math.min(pbox.x, mbox.x) - 12)
    const right = Math.min(1440, Math.max(pbox.x + pbox.width, mbox.x + mbox.width) + 12)
    const bottom = Math.min(900, Math.max(mbox.y + mbox.height, pbox.y + 90) + 12)
    await clipShot({ x, y: 0, width: right - x, height: bottom }, '22-side-panel-add-menu')
  }
  await page.keyboard.press('Escape')

  // ── Frame 21: the LINKS tab, empty ────────────────────────────────────────
  await load(SURFACES.linksEmpty, {
    activeId: 'links',
    tabs: [{ id: 'links', kind: 'links', title: 'Links' }],
  })
  await panel().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1400)
  console.log('DIAG links-empty', JSON.stringify(await page.evaluate(() => {
    const p = document.querySelector('div.side-panel-strip')?.parentElement
    return {
      tabs: [...document.querySelectorAll('[role="tab"]')].map(e => e.textContent?.trim()),
      rows: document.querySelectorAll('a[title^="http"]').length,
      text: p?.textContent?.slice(0, 160).replace(/\s+/g, ' '),
    }
  })))
  await shot(panel(), '21-links-tab-empty')

  // ── Frame 23: the pull-request panel, one file expanded ───────────────────
  await load(SURFACES.pr, { activeId: 'changes', tabs: [] })
  await panel().waitFor({ state: 'visible', timeout: 15000 })
  // The panel's own header + file list first, then the row's diff.
  await page.waitForFunction(
    path => [...document.querySelectorAll('button')].some(b => b.textContent?.includes(path)),
    PR_EXPAND_PATH,
    { timeout: 20000 },
  )
  await page.locator(`button:has-text("${PR_EXPAND_PATH}")`).first().click()
  // DiffView defers mounting Pierre by 140ms behind the drawer animation.
  await page.waitForSelector('[data-testid="pr-diff-surface"]', { timeout: 15000 })
  await page.waitForTimeout(2000)
  console.log('DIAG pr', JSON.stringify(await page.evaluate(() => {
    const s = document.querySelector('[data-testid="pr-diff-surface"]')
    return {
      surface: !!s,
      pierreHeaders: s?.querySelectorAll('[data-diffs-header]').length ?? -1,
      titles: [...(s?.querySelectorAll('[data-title]') ?? [])].map(e => e.textContent?.trim()),
      diffRows: s?.querySelectorAll('[data-diffs-line], [data-line]').length ?? -1,
      addRows: s?.querySelectorAll('[data-diff-type="add"], [data-type="add"]').length ?? -1,
    }
  })))
  await shot(panel(), '23-pr-panel-diff')

  // ── Frame 24: a tool call whose input is a patch ───────────────────────────
  await load(SURFACES.tool)
  // A lone tool message still renders inside a CollapsibleToolGroup (collapsed),
  // so the group opens first and the pill second.
  const group = page.locator('button[aria-label^="Expand"]').first()
  if (await group.count()) { await group.click(); await page.waitForTimeout(500) }
  const pill = page.locator('button[aria-expanded="false"][aria-label^="Show details"]').first()
  await pill.waitFor({ state: 'visible', timeout: 15000 })
  await pill.click()
  await page.waitForSelector('.pierre-surface', { timeout: 15000 })
  await page.waitForTimeout(2000)
  console.log('DIAG tool', JSON.stringify(await page.evaluate(() => {
    const s = document.querySelector('.pierre-surface')
    return {
      surface: !!s,
      pierreHeaders: s?.querySelectorAll('[data-diffs-header]').length ?? -1,
      diffRows: s?.querySelectorAll('[data-diffs-line], [data-line]').length ?? -1,
      section: [...document.querySelectorAll('button')]
        .filter(b => /^(Input|Output|Purpose)$/.test(b.textContent?.trim() || ''))
        .map(b => `${b.textContent?.trim()}:${b.className.includes('text-accent') ? 'active' : 'idle'}`),
      text: s?.textContent?.slice(0, 90).replace(/\s+/g, ' '),
    }
  })))
  {
    // The whole tool row: the pill, the Input/Output segmented control, and the
    // rendered patch. Addressed through the pill's own turn container so the
    // frame is not the entire transcript.
    const row = page.locator('div:has(> div > button[aria-label^="Hide details"])').first()
    const target = (await row.count()) ? row : page.locator('.pierre-surface').first()
    await shot(target, '24-tool-input-diff')
  }

  // ── Frame 25: Papyrus with a .tex document open ───────────────────────────
  await page.addInitScript(project => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    // The page's own last-open-project key: seeded so it lands in the workspace
    // rather than the project list, and opens main_file on its own.
    localStorage.setItem('kc:papyrus:project', project)
  }, PAPYRUS_PROJECT)
  await page.goto(base + '/papyrus', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="papyrus-editor"]', { timeout: 20000 })
  await page.waitForTimeout(3000)
  console.log('DIAG papyrus', JSON.stringify(await page.evaluate(() => {
    const ed = document.querySelector('[data-testid="papyrus-editor"]')
    // Pierre paints into a shadow root, but its injected <style> is the FIRST
    // element child — so probing querySelector('*') finds the style tag, whose
    // shadowRoot is null. Walk every descendant and take the first real host.
    const hosts = [...(ed?.querySelectorAll('*') ?? [])].filter(e => e.shadowRoot)
    const root = hosts[0]?.shadowRoot ?? null
    const spans = [...(root ?? ed ?? document).querySelectorAll('span[style*="color"], span[class]')]
    const colours = new Set(
      spans.map(s => s.getAttribute('style')?.match(/color:\s*([^;]+)/)?.[1]).filter(Boolean),
    )
    // Text with the injected stylesheet excluded, so an empty editor reads empty.
    const styleText = [...(ed?.querySelectorAll('style') ?? [])]
      .map(s => s.textContent || '').join('')
    const visible = (ed?.textContent || '').replace(styleText, '').trim()
    return {
      editor: !!ed,
      hostCount: hosts.length,
      hostTags: hosts.slice(0, 3).map(h => h.tagName.toLowerCase()),
      shadow: !!root,
      spans: spans.length,
      distinctColours: colours.size,
      sampleColours: [...colours].slice(0, 8),
      visibleLen: visible.length,
      visible: visible.slice(0, 120).replace(/\s+/g, ' '),
      shadowText: (root?.textContent || '').slice(0, 120).replace(/\s+/g, ' '),
      edHtml: (ed?.innerHTML || '').replace(styleText, '«css»').slice(0, 400),
    }
  })))
  {
    const editor = page.locator('[data-testid="papyrus-editor"]').first()
    const box = await editor.boundingBox()
    // Element screenshot of a full-height editor column would exceed the budget
    // at dsf 2 only if it were taller than 1000 CSS px; the column is ~760 here,
    // but clamp anyway so the assertion can never be the thing that fails.
    await clipShot({
      x: box.x, y: box.y,
      width: Math.min(box.width, 990),
      height: Math.min(box.height, 990),
    }, '25-papyrus-tex')
  }

  console.log('\n── SUMMARY ─────────────────────────────')
  for (const w of wrote) console.log(`${w.over ? 'OVER' : ' ok '}  ${w.w}x${w.h}  ${w.file}`)
  const over = wrote.filter(w => w.over)
  console.log(over.length ? `FAIL: ${over.length} frame(s) exceed ${MAX_EDGE}px` : `all ${wrote.length} frames within ${MAX_EDGE}px`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
