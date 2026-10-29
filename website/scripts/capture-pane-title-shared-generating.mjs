/**
 * Evidence: the session title control's Auto-title state is shared
 * by every pane showing the slot, and a narrow pane hides the Auto-title label.
 *
 * Runs the REAL built SPA (website/dist) with all /api/** answered from
 * fixtures, like capture-pane-header-autotitle.mjs.
 *
 * Frames:
 *   1. shared-generating.png -- one slot open in BOTH panes of a split; the
 *      generate-title POST is held. Both panes show "Generating title…", and the
 *      harness asserts the POST was made once even after pressing the other
 *      pane's button. (Before this change only the pressed pane spun, and the other
 *      pane's button fired a second POST.)
 *   2. narrow-pane-hover.png -- a 4:1 side-by-side split at 1440px; pointer on
 *      the narrow pane's title bar. Below 20rem of pane width the Pen and the
 *      regenerate button hide and the title keeps the space (before: the label
 *      clipped against the pane's own buttons).
 *
 * Usage: node scripts/capture-pane-title-shared-generating.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, json } from './lib/stub-dashboard-api.mjs'
import { stubSplitPanes } from './lib/split-pane-fixture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pane-title-shared-generating'
mkdirSync(OUT, { recursive: true })

const LEFT = 'chat-left'
const RIGHT = 'chat-right'

const slots = [
  {
    key: LEFT, title: 'Release checklist review', messages: 2, running: false,
    agent: 'kirocrew', created: '2026-06-01T09:00:00Z', last_ts: '2026-08-13T10:00:00Z', folder_id: '',
  },
  {
    key: RIGHT, title: 'Pipeline triage', messages: 2, running: false,
    agent: 'oncall', created: '2026-08-12T09:00:00Z', last_ts: '2026-08-13T09:30:00Z', folder_id: '',
  },
]

const msgs = [
  { role: 'user', content: 'Which checklist items are still open?', ts: '2026-08-13T09:56:00Z', meta: { mid: 'm-1' } },
  { role: 'assistant', content: 'The changelog entry and the smoke run.', ts: '2026-08-13T09:57:00Z', meta: { mid: 'm-2' } },
]
const TRANSCRIPTS = { [LEFT]: msgs, [RIGHT]: msgs }

// `dir: 'col'` lays the panes side by side; `'row'` stacks them.
const split = (left, right, sizes, dir = 'col') => ({
  [LEFT]: {
    type: 'split', id: 'sp-1', dir, sizes,
    children: [
      { type: 'leaf', id: 'lf-1', kind: 'session', slot: left },
      { type: 'leaf', id: 'lf-2', kind: 'session', slot: right },
    ],
  },
})

const LABEL = 'Regenerate title'
const BUSY = 'Generating title'

async function openSplit(browser, base, { layout, width, extra }) {
  const context = await browser.newContext({ viewport: { width, height: 700 }, deviceScaleFactor: 2, locale: 'en' })
  const page = await context.newPage()
  await stubSplitPanes(page, { slots, transcripts: TRANSCRIPTS, layout, extra })
  await page.addInitScript(() => { localStorage.setItem('mc-lang', 'en') })
  logPageProblems(page)
  await page.goto(`${base}/chat/${LEFT}`, { waitUntil: 'domcontentloaded' })
  await page.locator('.group\\/header').nth(1).waitFor({ timeout: 20_000 })
  await page.waitForTimeout(1500)
  return { context, page, bars: page.locator('[data-pane-title-row]') }
}

async function sharedGenerating(browser, base) {
  let posts = 0
  let release = null
  const { context, page, bars } = await openSplit(browser, base, {
    layout: split(LEFT, LEFT, [0.5, 0.5]),
    width: 1440,
    extra: async (path, route) => {
      if (path === `/api/chat/slots/${LEFT}/generate-title`) {
        posts += 1
        await new Promise(resolve => { release = resolve })
        await json(route, { ok: true, title: 'Thursday cut checklist' })
        return true
      }
      return false
    },
  })
  if ((await bars.count()) < 2) throw new Error('expected two panes')
  await bars.nth(0).hover()
  await bars.nth(0).getByRole('button', { name: /Regenerate title/ }).click()
  await page.waitForTimeout(300)
  const otherButton = bars.nth(1).getByRole('button', { name: /Regenerate title/ })
  if (await otherButton.count()) await otherButton.click({ force: true })
  await page.waitForTimeout(300)
  const busy = await page.getByText(BUSY).count()
  await page.screenshot({ path: `${OUT}/shared-generating.png` })
  console.log(`wrote ${OUT}/shared-generating.png (panes spinning: ${busy}, generate POSTs: ${posts})`)
  release?.()
  await context.close()
  return { busy, posts }
}

async function narrowHover(browser, base) {
  const { context, page, bars } = await openSplit(browser, base, {
    layout: split(LEFT, RIGHT, [0.8, 0.2]),
    width: 1440,
  })
  await bars.nth(1).hover()
  await page.waitForTimeout(400)
  const label = bars.nth(1).getByText(LABEL, { exact: true })
  const labelShown = (await label.count()) > 0 && (await label.isVisible())
  const glyph = bars.nth(1).getByRole('button', { name: /Regenerate title/ })
  const glyphShown = (await glyph.count()) > 0 && (await glyph.isVisible())
  await page.screenshot({ path: `${OUT}/narrow-pane-hover.png` })
  console.log(`wrote ${OUT}/narrow-pane-hover.png (label visible: ${labelShown}, button visible: ${glyphShown})`)
  await context.close()
  return { labelShown, glyphShown }
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  const shared = await sharedGenerating(browser, base)
  const narrow = await narrowHover(browser, base)
  if (process.env.EXPECT_FIXED === '1') {
    if (shared.busy !== 2 || shared.posts !== 1) throw new Error(`shared state not fixed: ${JSON.stringify(shared)}`)
    if (narrow.labelShown || narrow.glyphShown) throw new Error(`narrow label not fixed: ${JSON.stringify(narrow)}`)
  }
} finally {
  await browser.close()
  srv.close()
}
