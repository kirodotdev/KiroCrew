/**
 * Screenshot harness for the pull-request panel's CodeView-based Changes tab.
 *
 * The tab no longer draws rows of its own: every changed file is a `CodeViewItem`
 * in ONE Pierre CodeView, each file's row IS Pierre's stock file header, and the
 * header pins itself while its file scrolls. These frames are the evidence for
 * all three of those claims.
 *
 * Same gateway-free harness as its sibling scripts: the REAL built SPA
 * (website/dist) behind the shared in-process static server, with every /api/**
 * call answered from fixtures via Playwright route interception — no kiro-cli, no
 * live backend, no token. The client code under test is unmodified.
 *
 * Frames:
 *   30-codeview-all-files   the top of the change set: five files, each with its
 *                           own diff surface, in one scroll container. The rows
 *                           are Pierre file headers — change icon (add / delete /
 *                           modify), path, its own +/- counts — with the panel's
 *                           collapse chevron in the header PREFIX slot and the
 *                           provider's status token in the METADATA slot.
 *   31-codeview-sticky      scrolled into the middle of the set, so the file
 *                           whose body is on screen has its header pinned to the
 *                           top of the scroller while the previous file's rows
 *                           have left. This is `stickyHeaders`, which only does
 *                           anything because the CodeView defaults also turn the
 *                           file header back ON (getStickyHeaderOffset returns 0
 *                           when the header is disabled).
 *   32-codeview-collapsed   two files collapsed through the prefix chevron:
 *                           CodeView reserves only their header band and never
 *                           tokenizes their bodies, so a large change set stays
 *                           navigable as a list.
 *   33-codeview-no-patch    a file whose patch the provider withheld (binary, or
 *                           over its size ceiling). It is still an item, so it
 *                           stays present and countable; its header can only
 *                           report +0 −0, so the panel says "No patch" rather
 *                           than printing a second, disagreeing count pair.
 *   34-codeview-collapse-all  the summary bar's bulk control, clicked from a
 *                           PARTLY collapsed set (the case that must collapse the
 *                           rest, not expand). It writes the same per-path state
 *                           the header chevrons do, so the two can never
 *                           disagree, and the control flips to Expand all.
 *   35-codeview-expand-all  the same control taking the whole set back open.
 *
 * Frames are ELEMENT screenshots, never full-page: at deviceScaleFactor 2 a
 * 1440x900 viewport is 2880x1800, well over the 2000px-per-edge budget. Every
 * write asserts both edges.
 *
 * Usage: node scripts/capture-pr-codeview.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync, readdirSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pr-codeview'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const MAX_EDGE = 2000
const PANEL_W = 760
const SLOT_KEY = 'pr-codeview'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/912'

// ── Fixture ─────────────────────────────────────────────────────────────────

/* Provider payloads are per-file patch BODIES — hunks only, no `diff --git` /
 * `---` / `+++`. That is deliberate: the CodeView wrapper synthesizes those
 * headers (and the `new file mode` / `deleted file mode` line the change type is
 * read from) before parsing, so a fixture that already carried them would not
 * exercise the real path. */

const MODIFIED_PATCH = [
  '@@ -14,7 +14,14 @@',
  ' export function pierreThemeType(isDark: boolean) {',
  "   return isDark ? 'dark' : 'light'",
  ' }',
  ' ',
  '-const WORKER_POOL_SIZE = 1',
  '+/** Highlighter workers shared by every Pierre surface on the page. Four is the',
  '+ *  point past which a 12-surface transcript stops queueing on paint. */',
  '+export const PIERRE_WORKER_POOL_SIZE = 4',
  '+',
  '+export const PIERRE_CODE_VIEW_DEFAULTS = {',
  '+  ...PIERRE_DIFF_DEFAULTS,',
  '+  disableFileHeader: false,',
  '+  stickyHeaders: true,',
  '+}',
  ' ',
  ' export const PIERRE_CODE_DEFAULTS: BaseCodeOptions = {',
  '   theme: PIERRE_THEMES,',
].join('\n')

const NEW_FILE_PATCH = [
  '@@ -0,0 +1,22 @@',
  '+/**',
  ' + * Pierre CodeView: one virtualized viewer holding every file of a change set.',
  '+ */',
  "+import { useMemo, useRef } from 'react'",
  "+import { CodeView } from '@pierre/diffs/react'",
  '+',
  '+export interface PierrePatchFile {',
  '+  path: string',
  '+  patch?: string',
  '+  status?: string',
  '+  collapsed?: boolean',
  '+}',
  '+',
  '+export function PierreCodeViewImpl({ files, options, className }) {',
  '+  const dark = useIsDark()',
  '+  const resolved = useMemo(',
  '+    () => pierreCodeViewOptions({ themeType: pierreThemeType(dark), ...options }),',
  '+    [dark, options],',
  '+  )',
  '+  const items = useMemo(() => files.map(toItem), [files])',
  '+  return <CodeView className={className} items={items} options={resolved} />',
  '+}',
].join('\n')

const DELETED_PATCH = [
  '@@ -1,14 +0,0 @@',
  '-import { memo } from "react"',
  '-',
  '-/** A collapsible row wrapping one file diff. Superseded by CodeView: the',
  '- *  library draws the row, pins it, and virtualizes across files. */',
  '-export default memo(function ChangeRow({ file }) {',
  '-  const [open, setOpen] = useState(false)',
  '-  return (',
  '-    <div className="border-b border-border">',
  '-      <button onClick={() => setOpen(v => !v)}>{file.path}</button>',
  '-      {open && <DiffView patch={file.patch} path={file.path} />}',
  '-    </div>',
  '-  )',
  '-})',
].join('\n')

const PANEL_PATCH = [
  '@@ -388,44 +388,18 @@',
  ' function PullRequestBody({ source, tab }) {',
  '-function DiffView({ patch, path }) {',
  '-  const ready = useDeferredMount()',
  '-  const filePatch = useMemo(() => withUnifiedPatchHeaders(path, patch), [patch, path])',
  '-  if (!ready) return <div>Loading diff…</div>',
  '-  return <PierrePatch patch={filePatch} />',
  '-}',
  '+function ChangesTab({ source }) {',
  '+  const [collapsedPaths, setCollapsedPaths] = useState(NO_COLLAPSED_PATHS)',
  '+  return (',
  '+    <PierreCodeView',
  '+      files={files}',
  '+      className="flex-1 min-h-0 overflow-y-auto pierre-surface"',
  '+      renderHeaderPrefix={renderHeaderPrefix}',
  '+      renderHeaderMetadata={renderHeaderMetadata}',
  '+    />',
  '+  )',
  '+}',
  ' ',
  '   if (tab === \'description\') {',
].join('\n')

const FILES = [
  { path: 'website/src/pierre/config.ts', status: 'modified', additions: 12, deletions: 1, patch: MODIFIED_PATCH },
  { path: 'website/src/pierre/PierreCodeViewImpl.tsx', status: 'added', additions: 22, deletions: 0, patch: NEW_FILE_PATCH },
  { path: 'website/src/components/PullRequestPanel.tsx', status: 'modified', additions: 11, deletions: 6, patch: PANEL_PATCH },
  { path: 'website/src/components/ChangeRow.tsx', status: 'removed', additions: 0, deletions: 14, patch: DELETED_PATCH },
  // Patch withheld, real counts non-zero: over the provider's size ceiling. The
  // header can only derive +0 −0 from zero hunks, so the panel's metadata slot
  // supplies the true numbers.
  { path: 'website/package-lock.json', status: 'modified', additions: 1240, deletions: 318, patch: '' },
  // Patch withheld, and genuinely 0/0: a binary asset. Nothing to add — the
  // header's own +0 −0 is already the truth, so no counts are restated.
  { path: 'website/src/assets/pierre-codeview.png', status: 'modified', additions: 0, deletions: 0, patch: '' },
]

/** Long enough to actually scroll the Description tab, which is what the
 *  chrome-condensing check below needs: a tab whose content fits never scrolls,
 *  so it correctly never condenses and would prove nothing. */
const PR_DESCRIPTION = [
  'One CodeView holds every changed file. The rows are Pierre file headers, pinned while their file scrolls.',
  '',
  '## Why',
  '',
  'The Changes tab drew its own rows: a button per file with a collapsible patch',
  'underneath, so nothing was shared across files and a long change set paid a full',
  'DOM row per line of every expanded diff.',
  '',
  '## What changed',
  '',
  '- Each file is a `CodeViewItem`, and its row is the library\'s own file header.',
  '- Sticky headers pin the current file while its body scrolls past.',
  '- Virtualization spans files as well as lines, so a 200-file PR mounts a window.',
  '- The collapse chevron rides in the header prefix slot.',
  '- A withheld patch still gets an item, labelled so its +0 -0 is not read as a count.',
  '',
  '## Notes',
  '',
  'Pierre reads a file\'s change type from the `new file mode` / `deleted file mode`',
  'lines rather than the `/dev/null` side, so the synthesized headers emit both.',
  'The layout gap and per-file trailing padding are zeroed through the library\'s own',
  'options rather than CSS, because the same numbers feed the virtualizer\'s height',
  'estimates for items it has not rendered yet.',
  '',
  'The chrome above collapses once the reader scrolls in, and is restored at the top.',
  'That is keyed on scroll position rather than direction: CodeView re-anchors its',
  'own scroll when previously-unrendered items get measured, and those corrections',
  'arrive as upward deltas large enough to look like a user scrolling up.',
  '',
  '## Follow-ups',
  '',
  ...Array.from({ length: 14 }, (_, i) => [
    `${i + 1}. A padding line so this description comfortably exceeds the panel height.`,
    '   A tab whose content fits never scrolls, so it never condenses — which is',
    '   correct behaviour but proves nothing, and left the check below vacuous.',
  ]).flat(),
].join('\n')

const PR_SOURCE = {
  provider: 'github',
  url: PR_URL,
  number: 912,
  title: 'Render the Changes tab through Pierre CodeView',
  description: PR_DESCRIPTION,
  state: 'open',
  draft: false,
  mergedAt: '',
  updatedAt: new Date(Date.now() - 18 * 60 * 1000).toISOString(),
  headBranch: 'pierre-codeview-changes',
  baseBranch: 'main',
  headSha: 'c41d7ae90b2f5518aa3e6f0b41d92c7ae550317b',
  author: 'kiro-dev',
  additions: FILES.reduce((s, f) => s + f.additions, 0),
  deletions: FILES.reduce((s, f) => s + f.deletions, 0),
  changedFiles: FILES.length,
  mergeable: 'mergeable',
  mergeStateStatus: 'clean',
  autoMerge: false,
  commits: [{
    sha: 'c41d7ae90b2f5518aa3e6f0b41d92c7ae550317b',
    title: 'Render the Changes tab through Pierre CodeView',
    body: '',
    author: 'kiro-dev',
    date: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
    url: `${PR_URL}/commits/c41d7ae`,
  }],
  checks: [{
    name: 'build', workflow: 'ci', status: 'completed', conclusion: 'success',
    bucket: 'passed', url: `${PR_URL}/checks`, startedAt: '', completedAt: '',
  }],
  comments: [],
  files: FILES,
}

/** One chat slot whose transcript MENTIONS the pull request, which is what makes
 *  the side panel offer a Changes tab (pullRequestLinks.emitChangeSources). */
const t0 = Math.floor(Date.now() / 1000) - 1200
const MESSAGES = [
  { role: 'user', content: 'Move the Changes tab onto Pierre CodeView.', ts: String(t0) },
  {
    role: 'assistant',
    ts: String(t0 + 60),
    content: `Done — opened as [PR #912](${PR_URL}), the Changes tab on one Pierre CodeView.`,
  },
]

// ── Harness ─────────────────────────────────────────────────────────────────

/** PNG width/height straight out of the IHDR chunk — no image dependency. */
function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

/** Chromium to drive. `website/node_modules/playwright` pins one browser
 *  revision, but this machine's `~/.cache/ms-playwright` may only hold builds
 *  fetched by a DIFFERENT playwright, in which case a bare `chromium.launch()`
 *  dies on the pinned path. Same resolution as the sibling capture scripts. */
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
  mkdirSync(OUT, { recursive: true })
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  page.on('console', m => {
    if (m.type() === 'error') console.log('CONSOLE-ERR', m.text().slice(0, 300))
  })
  page.on('requestfailed', r => console.log('REQ-FAIL', r.failure()?.errorText, r.url().slice(-90)))
  page.on('response', r => {
    if (r.status() >= 400) console.log('HTTP', r.status(), r.url().slice(-90))
  })

  const slots = [{
    key: SLOT_KEY,
    title: 'Pierre CodeView changes tab',
    running: false,
    last_message: 'Pull request panel',
    messages: MESSAGES.length,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  }]
  const detail = {
    running: false, has_more: false, total: MESSAGES.length, queue: [], messages: MESSAGES,
  }

  const extra = async (path, route) => {
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, detail), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
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
    if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  const wrote = []
  function record(file) {
    const { w, h } = pngSize(file)
    const over = w > MAX_EDGE || h > MAX_EDGE
    console.log(`wrote ${file}  ${w}x${h}${over ? '  ⚠️ OVER 2000px' : ''}`)
    wrote.push({ file, w, h, over })
  }
  async function shot(locator, name) {
    const file = `${OUT}/${name}.png`
    await locator.screenshot({ path: file })
    record(file)
  }

  await page.addInitScript(([key, panelWidth]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', key)
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
    localStorage.setItem('mc-side-panel-width', String(panelWidth))
    localStorage.setItem(`mc-activity-open:${key}`, 'true')
    localStorage.setItem(`mc-panel-tabs:${key}`, JSON.stringify({ activeId: 'changes', tabs: [] }))
  }, [SLOT_KEY, PANEL_W])

  await page.goto(`${base}/?sid=${encodeURIComponent(SLOT_KEY)}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await page.keyboard.press('Escape')
  const close = page.locator('[aria-label="Close"]')
  if (await close.count()) await close.first().click().catch(() => {})
  await page.waitForTimeout(400)

  /** The side panel's root: the only element with the tab strip as a child. */
  const panel = () => page.locator('div:has(> div.side-panel-strip)').first()
  await panel().waitFor({ state: 'visible', timeout: 15000 })

  // Pierre's file headers live in the custom element's shadow root, which a
  // Playwright CSS locator pierces and a page.evaluate querySelectorAll does
  // NOT — so every wait and diagnostic below goes through locators.
  const headers = page.locator('[data-diffs-header]')
  await headers.first().waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(2500)

  console.log('DIAG headers', await headers.count())
  console.log('DIAG titles', JSON.stringify(await page.locator('[data-title]').allInnerTexts()))
  console.log('DIAG icons', JSON.stringify(
    await page.locator('[data-change-icon]').evaluateAll(
      els => els.map(e => e.getAttribute('data-change-icon')),
    ),
  ))
  console.log('DIAG sticky', await page.locator('[data-diffs-header][data-sticky]').count())
  console.log('DIAG collapse-toggles', await page.locator('button[aria-label="Collapse file"]').count())
  console.log('DIAG metadata-slots', await page.locator('[data-metadata]').count())
  // Row geometry: where the vertical space between two files actually comes from.
  // CodeView flex-gaps its item container and each item reserves its own internal
  // spacing, so a "spacer" between file headers is the sum of the two.
  console.log('DIAG row-geometry', JSON.stringify(await page.evaluate(() => {
    const items = [...document.querySelectorAll('diffs-container')]
    const parent = items[0]?.parentElement
    const rect = (el) => el.getBoundingClientRect()
    return {
      itemCount: items.length,
      containerGap: parent ? getComputedStyle(parent).gap : null,
      containerDisplay: parent ? getComputedStyle(parent).display : null,
      itemMargins: items.slice(0, 3).map(el => {
        const s = getComputedStyle(el)
        return `m:${s.marginTop}/${s.marginBottom} p:${s.paddingTop}/${s.paddingBottom}`
      }),
      // Raw pixel space between consecutive item boxes.
      betweenBoxes: items.slice(1, 4).map((el, i) => Math.round(rect(el).top - rect(items[i]).bottom)),
    }
  })))

  // ── Frame 30: the whole change set, every file expanded ───────────────────
  await shot(panel(), '30-codeview-all-files')

  /** CodeView's scroll element is the div it mounted into — the one carrying the
   *  panel's `pierre-surface` classes. */
  const scroller = page.locator('.pierre-surface').first()

  /** Measured height of the COLLAPSING WRAPPER. It has to be the wrapper, not a
   *  band inside it: the chrome collapses by animating a grid row to 0fr and
   *  clipping with `overflow: hidden`, and a clipped child keeps its intrinsic
   *  height — so measuring the tab strip reports 46px whether or not any of it is
   *  on screen, and `isVisible()` says true for the same reason. */
  const chromeHeight = () => page.locator('[data-pr-chrome]')
    .first()
    .evaluate(el => Math.round(el.getBoundingClientRect().height))

  const chromeHeightAtRest = await chromeHeight()

  // ── Frame 31: scrolled into the change set — chrome condensed away ─────────
  await scroller.evaluate(el => { el.scrollTop = 900 })
  await page.waitForTimeout(1800)
  console.log('DIAG condense', JSON.stringify({
    scrollTop: await scroller.evaluate(el => el.scrollTop),
    chromeHeightAtRest,
    chromeHeightScrolled: await chromeHeight(),
    // The files-changed bar is a pinned panel row BELOW the collapsing chrome, so
    // it must hold its place while the chrome above it goes.
    toolbarStillVisible: await page.locator('text=Files Changed').first().isVisible(),
  }))
  await shot(panel(), '31-codeview-sticky')

  // ── Frame 32: two files collapsed through the prefix chevron ──────────────
  await scroller.evaluate(el => { el.scrollTop = 0 })
  await page.waitForTimeout(800)
  const toggles = page.locator('button[aria-label="Collapse file"]')
  await toggles.nth(1).click()
  await page.waitForTimeout(700)
  await page.locator('button[aria-label="Collapse file"]').nth(1).click()
  await page.waitForTimeout(1600)
  console.log('DIAG collapsed', await page.locator('button[aria-label="Expand file"]').count())
  await shot(panel(), '32-codeview-collapsed')

  // ── Frame 33: the withheld-patch file, scrolled to the end ────────────────
  await scroller.evaluate(el => { el.scrollTop = el.scrollHeight })
  await page.waitForTimeout(1800)
  await shot(panel(), '33-codeview-no-patch')

  // ── Frame 34: Collapse all — the bulk control drives the same per-path state
  //     the header chevrons write, so the whole set becomes a navigable list and
  //     the control flips to Expand all ────────────────────────────────────────
  await scroller.evaluate(el => { el.scrollTop = 0 })
  await page.waitForTimeout(600)
  // Two files are still collapsed from frame 32, so the set is PARTLY collapsed
  // here — which is the case that has to collapse the rest rather than expand.
  await page.locator('button:has-text("Collapse all")').first().click()
  await page.waitForTimeout(1600)
  console.log('DIAG collapse-all', JSON.stringify({
    expandToggles: await page.locator('button[aria-label="Expand file"]').count(),
    collapseToggles: await page.locator('button[aria-label="Collapse file"]').count(),
    bulkLabel: await page.locator('button:has-text("Expand all")').count(),
  }))
  await shot(panel(), '34-codeview-collapse-all')

  // ── Frame 35: Expand all back again ───────────────────────────────────────
  await page.locator('button:has-text("Expand all")').first().click()
  await page.waitForTimeout(2000)
  console.log('DIAG expand-all', JSON.stringify({
    expandToggles: await page.locator('button[aria-label="Expand file"]').count(),
    bulkLabel: await page.locator('button:has-text("Collapse all")').count(),
  }))
  await shot(panel(), '35-codeview-expand-all')

  // ── Frame 36: scrolled back to the top — the chrome returns ────────────────
  // Reset first, then condense, then return: the reveal is keyed on POSITION, so
  // the assertion has to observe a real 0 -> deep -> top round trip.
  await scroller.evaluate(el => { el.scrollTop = 0 })
  await page.waitForTimeout(700)
  await scroller.evaluate(el => { el.scrollTop = 1400 })
  await page.waitForTimeout(900)
  const condensedDeep = await chromeHeight()
  await scroller.evaluate(el => { el.scrollTop = 0 })
  await page.waitForTimeout(1400)
  console.log('DIAG reveal-at-top', JSON.stringify({
    scrollTop: await scroller.evaluate(el => el.scrollTop),
    condensedDeep,
    chromeHeightBack: await chromeHeight(),
    condensedThenRevealed: condensedDeep === 0 && (await chromeHeight()) === chromeHeightAtRest,
  }))
  await shot(panel(), '36-codeview-chrome-returns')

  // ── Frame 37: the SAME collapse on a non-Changes tab. Those tabs scroll the
  //     tabpanel itself rather than CodeView's scroller, so this is the check that
  //     the one handler really is driven by both. ───────────────────────────────
  await page.locator('[role="tab"]:has-text("Description")').first().click()
  await page.waitForTimeout(800)
  const tabpanel = page.locator('#pr-tabpanel')
  const descAtRest = await chromeHeight()
  const scrollable = await tabpanel.evaluate(el => el.scrollHeight - el.clientHeight)
  await tabpanel.evaluate(el => { el.scrollTop = 400 })
  await page.waitForTimeout(1200)
  console.log('DIAG other-tab-condense', JSON.stringify({
    scrollableBy: scrollable,
    chromeAtRest: descAtRest,
    chromeScrolled: await chromeHeight(),
    // The stand-in bar exists in the DOM always but is collapsed to 0 height until
    // condensed, so measure the WRAPPER's height, not the inner bar's.
    barHeight: await page.locator('[data-pr-condensed-bar]').first()
      .evaluate(el => Math.round(el.parentElement.parentElement.getBoundingClientRect().height)),
    barText: (await page.locator('[data-pr-condensed-bar]').first().innerText()).trim().slice(0, 60),
  }))
  await shot(panel(), '37-description-condensed')

  await browser.close()
  srv.close()

  const over = wrote.filter(w => w.over)
  if (over.length) {
    console.error(`\n${over.length} frame(s) over the ${MAX_EDGE}px budget:`)
    for (const w of over) console.error(`  ${w.file} ${w.w}x${w.h}`)
    process.exitCode = 1
  } else {
    console.log(`\nOK: ${wrote.length} frame(s), all within ${MAX_EDGE}px per edge.`)
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
