/**
 * Screenshot harness for the side panel's STRUCTURED Office preview: a .docx
 * rendered as headings / lists / a table / bold and italic runs.
 *
 * Same house pattern as `capture-pierre-files-tab.mjs`: the REAL built SPA
 * (`website/dist`) behind the shared in-process static server, with every
 * `/api/**` answered from fixtures via Playwright route interception —
 * gateway-free (no kiro-cli, no live backend, no token). The client code under
 * test is unmodified.
 *
 * NEW FIXTURES (no sibling script stubbed these before):
 *   /api/file-office-preview?format=blocks → the structured block list
 *   /api/file-office-preview               → the plaintext fallback shape
 *
 * `.docx` is a RICH file type (`RICH_FILE_TYPES` in
 * MarkdownPanel.tsx), so the viewer never queries `/api/file-read` for them and
 * there is no cold-tab content hydration to fixture. The panel state is still
 * pre-seeded through localStorage, because the tab strip and the panel's open
 * flag are PERSISTED stores rather than props:
 *   `mc-activity-open:<slot>`  chatSlice's per-slot panel open flag
 *   `mc-panel-tabs:<slot>`     usePanelTabs' bucket ({activeId, tabs})
 *
 * Frames:
 *   10-docx-blocks-dark      a report: H1, body paragraph, bulleted list,
 *   11-docx-blocks-light     numbered list, 4x3 table, and a closing paragraph
 *                            with bold + italic runs
 *   15-docx-text-fallback-dark  blocks came back EMPTY (a container the
 *                            structured extractor could not read), so the
 *                            viewer re-requests text mode and renders the flat
 *                            preview — never worse than before this change —
 *                            behind a marker saying so, so a table this preview
 *                            flattened is not mistaken for the document's own
 *   19-docx-table-cols-truncated-dark  a table wider than the column cap: the
 *                            trim is reported AT the table ("Additional columns
 *                            not shown"), not as a document-level truncation
 *
 * Every frame is an ELEMENT screenshot of the side panel. Dimensions are
 * asserted after each write against the 2000px-per-edge PR-media budget, and
 * each frame's own content is probed BEFORE the write so a blank or wrong
 * surface throws instead of being saved as evidence.
 *
 * Usage: node scripts/capture-office-blocks.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/office-blocks'
/** Repo root, derived from this script's own location: the fixture paths show a
 *  real project path in the panel header without pinning frames to one worktree. */
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-office-blocks'

/** Hard ceiling for a PR-attached PNG, on BOTH edges. */
const MAX_EDGE = 2000
/** Milli-bytes per pixel below which a PNG is almost certainly a blank surface. */
const MIN_MBPP = 15

const DOCX = `${PROJECT}/reports/Q3-platform-review.docx`
/** A container the structured extractor cannot read, so `blocks` comes back
 *  empty and the viewer falls back to the text shape. */
const ODD_DOCX = `${PROJECT}/reports/legacy-export.docx`
/** A document whose table is wider than the column cap, so the backend trims it
 *  and says so ON the table. */
const WIDE_DOCX = `${PROJECT}/reports/regional-matrix.docx`

mkdirSync(OUT, { recursive: true })

// ── Fixtures ────────────────────────────────────────────────────────────────

/** The block list `format=blocks` returns for a .docx. Shape and field names
 *  come from `kiro_crew.doc_blocks.extract_blocks`; this is one real document's
 *  worth of every block type the renderer handles. */
const DOCX_BLOCKS = [
  { type: 'heading', level: 1, text: 'Q3 platform review' },
  { type: 'paragraph', runs: [{
    text: 'Adoption grew across every region this quarter. The detail below is the '
      + 'same data the weekly dashboard reports, restated once so it can be read offline.',
    bold: false, italic: false,
  }] },
  { type: 'heading', level: 2, text: 'Highlights' },
  { type: 'list', ordered: false, items: [
    'Weekly active workspaces passed the 10,000 mark in August.',
    'Median time-to-first-agent-run fell from 14 minutes to under 4.',
    'Two regions are now above the retention target for the first time.',
  ] },
  { type: 'heading', level: 2, text: 'Rollout order' },
  { type: 'list', ordered: true, items: [
    'Enable for internal workspaces.',
    'Extend to design partners.',
    'General availability.',
  ] },
  { type: 'heading', level: 2, text: 'Signups by region' },
  { type: 'table', rows: [
    ['Region', 'Signups', 'Change'],
    ['EMEA', '4,182', '+18%'],
    ['Americas', '3,904', '+11%'],
    ['APAC', '2,671', '+27%'],
  ] },
  { type: 'paragraph', runs: [
    { text: 'Note: ', bold: true, italic: false },
    { text: "APAC's jump follows the regional launch and is not expected to repeat. ", bold: false, italic: false },
    { text: 'Figures are provisional.', bold: false, italic: true },
  ] },
]

/** A table the backend trimmed at the column cap: `truncated_cols: true` on the
 *  block, and NOT on the document -- the document-level flag would render as
 *  "only the beginning of this document", which describes a different loss. */
const WIDE_BLOCKS = [
  { type: 'heading', level: 1, text: 'Regional matrix' },
  { type: 'paragraph', runs: [{
    text: 'The source sheet carries one column per week; the preview keeps the first few.',
    bold: false, italic: false,
  }] },
  { type: 'table', truncated_cols: true, rows: [
    ['Region', 'W1', 'W2', 'W3', 'W4'],
    ['EMEA', '412', '438', '455', '471'],
    ['Americas', '390', '401', '399', '420'],
    ['APAC', '267', '281', '302', '333'],
  ] },
]

const FALLBACK_TEXT = [
  'Legacy export',
  '',
  'This container came from an older writer, so the structured extractor found '
    + 'nothing it could resolve and the preview falls back to flat text.',
  '',
  'Region\tSignups\tChange',
  'EMEA\t4,182\t+18%',
  'Americas\t3,904\t+11%',
].join('\n')

const slots = [{
  key: SLOT,
  title: 'Q3 platform review',
  running: false,
  last_message: 'Q3 platform review',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const t0 = Math.floor(Date.now() / 1000) - 900
const slotDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Open the Q3 review and the deck that goes with it.', ts: String(t0) },
    { role: 'assistant', content: 'Both are in the side panel.', ts: String(t0 + 60) },
  ],
}

// ── Panel state seeds (persisted stores, not props) ─────────────────────────

const fileTab = (path, title) => ({
  id: `file:${path}`,
  kind: 'file',
  title,
  path,
  slot: SLOT,
  diffMode: false,
})

/** Bucket shape `usePanelTabs` rehydrates from `mc-panel-tabs:<slot>`. */
const bucket = (tabs, activeId) => JSON.stringify({ activeId, tabs })

// ── Harness ─────────────────────────────────────────────────────────────────

/** PNG width/height straight out of the IHDR chunk — no image dependency. */
function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })

  const wrote = []

  /** Assert each probe is visible with non-empty text; return what was found.
   *  A probe may name an ATTRIBUTE instead (`attr`) — an icon-only control
   *  carries no innerText, so demanding text there would fail on a perfectly
   *  rendered node. A failed probe THROWS rather than saving a lookalike: the
   *  point of these frames is evidence, and one nobody can trust is worse than
   *  a missing one. */
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

  /** Record a written PNG: edge budget + blank-frame density gate. A PNG of a
   *  blank surface compresses to almost nothing, so bytes-per-pixel catches the
   *  frame that exists on disk and shows nothing. */
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

  const probe = (selector, locator, opts = {}) => ({ selector, locator, min: opts.min, attr: opts.attr })

  /**
   * Open one document in the side panel on one theme, and hand back the page.
   *
   * A fresh context per frame rather than one shared page: the theme is read
   * from localStorage at boot, so switching it in place would need a reload
   * anyway, and a clean context is what keeps one frame's expanded notes fold
   * out of the next frame.
   *
   * `?sid=` selects the slot deterministically (the restore key is per-MODE,
   * `mc-active-slot-chat`, so a bare `mc-active-slot` does nothing). The other
   * seeds keep the frame clean:
   *   mc-activity-open:<slot>          the side panel is OPEN
   *   mc-panel-tabs:<slot>             the tab strip and which tab is active
   *   mc-files-rail-open               rail closed: this feature is the VIEWER
   *   mc-side-panel-width              wide enough to read a table
   *   mc-chat-config.pinLastPrompt     the pinned-prompt banner steals height
   *   mc-git-panel-opened:<slot>:<dir> ChatPage creates and FOCUSES a Git tab
   *                                    for any repo project dir; this marker is
   *                                    the "already did this" flag that effect
   *                                    checks, without which every frame
   *                                    captures the Git panel instead
   */
  async function open(path, title, theme) {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      // Table type and the 11px slide labels render soft at 1x on GitHub.
      deviceScaleFactor: 2,
    })
    const page = await context.newPage()

    const extra = async (apiPath, route) => {
      const url = new URL(route.request().url())
      const q = url.searchParams.get('path') || ''
      const format = url.searchParams.get('format') || 'text'

      if (apiPath === '/api/chat/slots') return json(route, slots), true
      if (/^\/api\/chat\/slots\/[^/]+/.test(apiPath)) return json(route, slotDetail), true

      // repo:false so ChatPage creates no Git tab and the rail needs no
      // status fixture — this harness is about the viewer column.
      if (apiPath === '/api/project/tree') {
        return json(route, { root: PROJECT, paths: [], repo: false, truncated: false }), true
      }
      if (apiPath === '/api/project/git') {
        return json(route, { path: PROJECT, repo: false }), true
      }
      if (apiPath === '/api/project/git/status') return json(route, { repo: false, files: [] }), true
      if (apiPath === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true

      if (apiPath === '/api/file-office-preview') {
        if (format === 'blocks') {
          if (q === DOCX) return json(route, { blocks: DOCX_BLOCKS, truncated: false }), true
          if (q === WIDE_DOCX) return json(route, { blocks: WIDE_BLOCKS, truncated: false }), true
          // The fallback document: the endpoint answers 200 with an EMPTY list,
          // which is the backend saying "nothing structured here".
          return json(route, { blocks: [], truncated: false }), true
        }
        return json(route, { text: FALLBACK_TEXT, truncated: false }), true
      }
      return false
    }

    await stubDashboardApi(page, {
      slots,
      theme,
      extra,
      localStorageEntries: {
        'mc-active-slot-chat': SLOT,
        [`mc-activity-open:${SLOT}`]: 'true',
        [`mc-panel-tabs:${SLOT}`]: bucket([fileTab(path, title)], `file:${path}`),
        // '1'/'0', NOT 'true'/'false': these are read through `usePersistedBool`,
        // whose getter is `v === '1'`.
        'mc-files-rail-open': '0',
        'mc-side-panel-width': '860',
        'kirocrew:comment-hint-dismissed': '1',
        [`mc-git-panel-opened:${SLOT}:${PROJECT}`]: '1',
        'mc-chat-config': JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }),
      },
    })
    logPageProblems(page)

    await page.goto(`${base}/?sid=${encodeURIComponent(SLOT)}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    return { context, page }
  }

  /** The side panel's own root: the only element with `.side-panel-strip` as a
   *  direct child (see SidePanel's render). */
  const panelOf = page => page.locator('div:has(> .side-panel-strip)').last()

  /** Open, wait for the surface, assert, shoot, close. */
  async function frame(name, { path, title, theme, probes, before }) {
    const { context, page } = await open(path, title, theme)
    const panel = panelOf(page)
    await panel.waitFor({ state: 'visible', timeout: 20000 })
    if (before) await before(page, panel)
    await page.waitForTimeout(900)
    const evidence = await assertRendered(name, probes(page, panel))
    const file = `${OUT}/${name}.png`
    await panel.screenshot({ path: file })
    record(file, evidence)
    await context.close()
  }

  // ── Frames 10/11: the docx report, dark and light ──────────────────────────
  for (const [name, theme] of [['10-docx-blocks-dark', 'dark'], ['11-docx-blocks-light', 'light']]) {
    await frame(name, {
      path: DOCX, title: 'Q3-platform-review.docx', theme,
      before: async page => {
        await page.getByRole('heading', { name: 'Q3 platform review' }).first()
          .waitFor({ state: 'visible', timeout: 20000 })
      },
      probes: (page, panel) => [
        probe('h1 "Q3 platform review"', panel.locator('h1', { hasText: 'Q3 platform review' })),
        probe('h2 headings', panel.locator('h2'), { min: 3 }),
        probe('unordered list items', panel.locator('ul > li'), { min: 3 }),
        probe('ordered list items', panel.locator('ol > li'), { min: 3 }),
        probe('table header cells', panel.locator('th'), { min: 3 }),
        probe('table body cells', panel.locator('td'), { min: 9 }),
        probe('bold run "Note:"', panel.locator('strong', { hasText: 'Note:' })),
        probe('italic run', panel.locator('em', { hasText: 'provisional' })),
        probe('download-original affordance', panel.getByText('Download original')),
      ],
    })
  }

  // ── Frame 15: the text fallback ───────────────────────────────────────────
  await frame('15-docx-text-fallback-dark', {
    path: ODD_DOCX, title: 'legacy-export.docx', theme: 'dark',
    before: async page => {
      await page.getByText('came from an older writer', { exact: false }).first()
        .waitFor({ state: 'visible', timeout: 20000 })
    },
    probes: (page, panel) => [
      probe('flat text preview (<pre>)', panel.locator('pre')),
      probe('fallback body text', panel.getByText('came from an older writer', { exact: false })),
      probe('plain-text marker', panel.locator('[data-testid="office-plain-text-notice"]')),
      probe('download-original affordance', panel.getByText('Download original')),
    ],
  })

  // ── Frame 19: a table trimmed at the column cap ──────────────────────────
  // The trim is reported AT the table, reusing the sheet viewer's string, and the
  // document-level "only the beginning" notice must NOT appear: that one describes
  // running out of budget part-way through, which this document did not.
  await frame('19-docx-table-cols-truncated-dark', {
    path: WIDE_DOCX, title: 'regional-matrix.docx', theme: 'dark',
    before: async page => {
      await page.locator('[data-testid="office-table-cols-truncated"]').first()
        .waitFor({ state: 'visible', timeout: 20000 })
      const docLevel = page.getByText('only the beginning of this document', { exact: false })
      if (await docLevel.count()) {
        throw new Error('frame 19: the document-level truncation notice is showing for a width trim — wrong notice, do not save the frame')
      }
    },
    probes: (page, panel) => [
      probe('table header cells', panel.locator('th'), { min: 5 }),
      probe('table body cells', panel.locator('td'), { min: 12 }),
      probe('column-trim notice at the table', panel.locator('[data-testid="office-table-cols-truncated"]')),
      probe('notice copy', panel.getByText('Additional columns not shown')),
      probe('download-original affordance', panel.getByText('Download original')),
    ],
  })

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
  if (bad.length) process.exit(1)
}

main().catch(err => { console.error(err); process.exit(1) })
