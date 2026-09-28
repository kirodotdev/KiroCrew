/**
 * Evidence harness for issue #13430 — the oversized diff card must keep its
 * expand/collapse control after the reader opts into the line-by-line diff.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via route interception
 * (gateway-free — no kiro-cli, no live backend, no token). The client code under
 * test is unmodified: `FileChangeChips` renders `meta.file_changes`, and one of
 * those files is deliberately over `PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE`, so its
 * row takes the oversized plain fallback with the "Show line-by-line diff"
 * opt-in instead of mounting Pierre on the whole pair.
 *
 * Frames:
 *   01-oversized-fallback     the expanded oversized row BEFORE opting in: the
 *                             plain two-side fallback, the chevron in the header,
 *                             and the opt-in button in its own strip
 *   02-opted-in-control       the same row AFTER opting in: the computed
 *                             line-by-line diff beneath the card's own header
 *                             row, chevron included (this is the regression
 *                             frame — on the unfixed build the header the
 *                             chevron lives in is Pierre's, which is absent in
 *                             every state where Pierre has nothing to draw)
 *   03-opted-in-collapsed     the opted-in row collapsed with that chevron, back
 *                             to the lightweight header
 *   04-reexpanded-reopted-in  re-expanded (the opt-in is component-local, so the
 *                             plain fallback and its button return) and opted in
 *                             again: the line-by-line diff is back, served from
 *                             the diff cache without a second computation, and
 *                             the chevron is in its header
 *
 * Every frame is an ELEMENT screenshot (`locator.screenshot()`), not a full-page
 * one, and dimensions are asserted after each write against the 2000px-per-edge
 * budget for PR media.
 *
 * Header parity gate: before the frames, the within-budget sibling row is
 * opened and the header Pierre draws for it is read through its shadow root
 * (computed background, inline padding, metadata-group width, count width and
 * alignment). The oversized row's own header must read the same before the
 * swap (fallback) and after it (opted in) — the four rules `FileChangeChips`
 * injects into Pierre's header cannot reach a light-DOM row, so the row
 * applies the same values inline, and this is where that is proven on screen.
 *
 * Usage: node scripts/capture-diff-card-line-by-line.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/diff-card-line-by-line'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
/** Hard ceiling for a PR-attached PNG, on BOTH edges. */
const MAX_EDGE = 2000

mkdirSync(OUT, { recursive: true })

/* Over PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE (400), so the row is oversized and
 * takes the opt-in fallback rather than diffing the whole pair on the renderer
 * thread. The edit sits deep in the file so the fallback's change region — and
 * then the computed hunk — are both about the same lines. */
const BIG_LINES = 520
const EDIT_AT = 430
const bigBefore = Array.from({ length: BIG_LINES }, (_, i) => `  const value${i} = compute(${i})`).join('\n')
const bigAfterLines = Array.from({ length: BIG_LINES }, (_, i) => `  const value${i} = compute(${i})`)
bigAfterLines[EDIT_AT] = `  const value${EDIT_AT} = compute(${EDIT_AT}, { retries: 3 })`
const bigAfter = bigAfterLines.join('\n')

const OVERSIZED_PATH = 'website/src/pierre/generatedPipeline.ts'
/* Within budget, so Pierre draws its own header for this row — the reference
 * the oversized row's kept header is measured against below. */
const SIBLING_PATH = 'website/src/components/FileChangeChips.tsx'

const SMALL_BEFORE = `export function toggleDiff(open: boolean) {
  return !open
}`
const SMALL_AFTER = `export function toggleDiff(open: boolean) {
  // The chevron is the one control; every card state shows it.
  return !open
}`

const FILE_CHANGES = [
  { path: OVERSIZED_PATH, before: bigBefore, after: bigAfter },
  { path: SIBLING_PATH, before: SMALL_BEFORE, after: SMALL_AFTER },
]

const t0 = Math.floor(Date.now() / 1000) - 900
const SLOT_KEY = 'chat-diff-card-line-by-line'

const messages = [
  { role: 'user', content: 'Regenerate the pipeline module and tidy the chips helper.', ts: String(t0) },
  {
    role: 'assistant',
    ts: String(t0 + 120),
    content: 'Done — the generated pipeline module is large, so its row offers the line-by-line diff on demand.',
    meta: { file_changes: FILE_CHANGES },
  },
]

const slots = [{
  key: SLOT_KEY,
  title: 'Oversized diff card opt-in',
  running: false,
  last_message: 'Oversized diff card opt-in',
  messages: messages.length,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

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
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) {
      return json(route, { running: false, has_more: false, total: messages.length, queue: [], messages }), true
    }
    if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
    if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }

  // Seeds ride INSIDE the stub's own init script: Playwright leaves the order
  // of separately registered init scripts undefined, so a second addInitScript
  // would race the stub's localStorage.clear(). `pinLastPrompt` off keeps the
  // pinned-prompt banner off the card; `expanded` is the card style these
  // frames are about; `immediate` avoids racing a reveal animation.
  await stubDashboardApi(page, {
    slots,
    extra,
    localStorageEntries: {
      'mc-active-slot-chat': SLOT_KEY,
      'mc-chat-config': JSON.stringify({ pinLastPrompt: false, fileChipStyle: 'expanded', streamMode: 'immediate' }),
    },
  })
  logPageProblems(page)

  /** Count elements matching `sel` ANYWHERE, Pierre's shadow roots included:
   *  every Pierre surface renders into a shadow root, so a raw
   *  `document.querySelectorAll` misses painted headers entirely. */
  const deepCount = (sel, root = 'body') => page.evaluate(([sel, rootSel]) => {
    let n = 0
    const walk = node => {
      if (!node) return
      n += node.querySelectorAll(sel).length
      for (const el of node.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot)
    }
    walk(document.querySelector(rootSel))
    return n
  }, [sel, root])

  const wrote = []
  async function shot(locator, name) {
    const file = `${OUT}/${name}.png`
    await locator.screenshot({ path: file })
    const { w, h } = pngSize(file)
    const over = w > MAX_EDGE || h > MAX_EDGE
    console.log(`wrote ${file}  ${w}x${h}${over ? '  ⚠️ OVER 2000px' : ''}`)
    wrote.push({ file, w, h, over })
  }

  /** The computed look of the first header row inside `rootSel` — light DOM or
   *  Pierre's shadow root alike — on the four rules `FileChangeChips` injects
   *  into Pierre's header (background, inline padding, count width, metadata
   *  group width). The kept row must read the same as the header Pierre draws
   *  on the within-budget row beside it. */
  const headerStyle = rootSel => page.evaluate((rootSel) => {
    const deep = (sel, root) => {
      const out = []
      const walk = node => {
        if (!node) return
        out.push(...node.querySelectorAll(sel))
        for (const el of node.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot)
      }
      walk(root)
      return out
    }
    const header = deep('[data-diffs-header]', document.querySelector(rootSel))[0]
    if (!header) return null
    const cs = getComputedStyle(header)
    const meta = deep('[data-metadata]', header)[0]
    return {
      inShadow: header.getRootNode() !== document,
      background: cs.backgroundColor,
      paddingLeft: cs.paddingLeft,
      paddingRight: cs.paddingRight,
      height: Math.round(header.getBoundingClientRect().height),
      metaFlexBasis: meta ? getComputedStyle(meta).flexBasis : null,
      metaJustify: meta ? getComputedStyle(meta).justifyContent : null,
      countMinWidths: deep('[data-deletions-count],[data-additions-count]', header).map(el => getComputedStyle(el).minWidth),
      countTextAlign: deep('[data-deletions-count],[data-additions-count]', header).map(el => getComputedStyle(el).textAlign),
    }
  }, rootSel)
  const PARITY_KEYS = ['background', 'paddingLeft', 'paddingRight', 'metaFlexBasis', 'metaJustify']
  const sameLook = (a, b) => PARITY_KEYS.every(k => a[k] === b[k])

  await page.goto(base + '/?sid=' + encodeURIComponent(SLOT_KEY), { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await page.keyboard.press('Escape')
  const close = page.locator('[aria-label="Close"]')
  if (await close.count()) await close.first().click().catch(() => {})
  await page.waitForTimeout(400)

  const card = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])').first()
  await card.waitFor({ state: 'visible', timeout: 15000 })
  const row = page.locator(`[data-testid="fcc-row-${OVERSIZED_PATH}"]`)
  const toggle = () => page.locator(`[data-testid="fcc-toggle-${OVERSIZED_PATH}"]`)

  // ── Reference: the header Pierre draws on the within-budget row ───────────
  // Opened, measured through its shadow root, closed again so the frames below
  // show the same card state as before. This is the look the oversized row's
  // own header rows — fallback and opted-in alike — must match.
  const siblingToggle = page.locator(`[data-testid="fcc-toggle-${SIBLING_PATH}"]`)
  await siblingToggle.click()
  await page.waitForTimeout(2500)
  const pierreHeader = await headerStyle(`[data-testid="fcc-row-${SIBLING_PATH}"]`)
  console.log('DIAG Pierre header (sibling row, shadow):', JSON.stringify(pierreHeader))
  if (!pierreHeader?.inShadow || pierreHeader.countMinWidths.length === 0) {
    throw new Error(`reference header gate failed: expected Pierre's own header with a count on the sibling row: ${JSON.stringify(pierreHeader)}`)
  }
  await siblingToggle.click()
  await page.waitForTimeout(600)

  // ── Frame 1: expanded, oversized fallback, not yet opted in ───────────────
  await toggle().click()
  await page.waitForTimeout(1200)
  const optIn = row.getByRole('button', { name: 'Show line-by-line diff' })
  await optIn.waitFor({ state: 'visible', timeout: 15000 })
  const before = {
    toggles: await toggle().count(),
    plainFallback: await row.locator('[data-pierre-plain-file-pair]').count(),
    plainSides: await row.locator('[data-pierre-plain-side]').count(),
    optInButtons: await optIn.count(),
    pierreHeaders: await deepCount('[data-diffs-header]', `[data-testid="fcc-row-${OVERSIZED_PATH}"]`),
  }
  console.log('DIAG before opt-in:', JSON.stringify(before))
  if (before.toggles !== 1 || before.plainSides !== 2 || before.optInButtons !== 1) {
    throw new Error(`fallback state gate failed: ${JSON.stringify(before)}`)
  }
  const fallbackHeader = await headerStyle(`[data-testid="fcc-row-${OVERSIZED_PATH}"]`)
  console.log('DIAG kept header BEFORE the swap (light DOM):', JSON.stringify(fallbackHeader))
  if (!fallbackHeader || fallbackHeader.inShadow || !sameLook(pierreHeader, fallbackHeader)) {
    throw new Error(`header parity gate failed before the swap: ${JSON.stringify({ pierreHeader, fallbackHeader })}`)
  }
  await shot(row, '01-oversized-fallback')

  // ── Frame 2: opted in — the control MUST still be there ───────────────────
  // Sample the row's height through the swap. Measured on the base build and on
  // this branch alike, the row dips for ONE sample to Pierre's own ~44px header
  // band before its rows paint (the hold's measurement is satisfied by that
  // band): pre-existing, not introduced here, and not this change's concern.
  // What this change must never add is a dip BELOW one header band — a header
  // that vanished with nothing standing in for it — so that is the gate.
  const heightBefore = (await row.boundingBox()).height
  const heights = []
  const headerCounts = []
  await optIn.click()
  for (let i = 0; i < 40; i++) {
    heights.push(Math.round((await row.boundingBox()).height))
    headerCounts.push(await deepCount('[data-diffs-header]', `[data-testid="fcc-row-${OVERSIZED_PATH}"]`))
    await page.waitForTimeout(50)
  }
  await page.waitForTimeout(1000)
  const minHeight = Math.min(...heights)
  const maxHeaders = Math.max(...headerCounts)
  const minHeaders = Math.min(...headerCounts)
  console.log('DIAG swap heights (px, 50ms apart):', JSON.stringify(heights), 'min:', minHeight, 'before:', Math.round(heightBefore))
  console.log('DIAG swap header rows per sample:', JSON.stringify(headerCounts), 'min:', minHeaders, 'max:', maxHeaders)
  if (minHeight < 32) {
    throw new Error(`the row lost its header mid-swap: collapsed to ${minHeight}px`)
  }
  // Exactly one header at every sample: never two (the card's own row next to
  // one of Pierre's) and never zero (a header that left with nothing standing
  // in for it, which is #13430 itself) — website/AGENTS.md: a persistent
  // element that changes form stays ONE element.
  if (maxHeaders !== 1 || minHeaders !== 1) {
    throw new Error(`header rows on screen mid-swap must be exactly one at every sample: ${JSON.stringify(headerCounts)}`)
  }
  const after = {
    toggles: await toggle().count(),
    toggleVisible: await toggle().count() ? await toggle().first().isVisible() : false,
    plainFallback: await row.locator('[data-pierre-plain-file-pair]').count(),
    optInButtons: await row.getByRole('button', { name: 'Show line-by-line diff' }).count(),
    pierreHeaders: await deepCount('[data-diffs-header]', `[data-testid="fcc-row-${OVERSIZED_PATH}"]`),
    pierreCode: await deepCount('[data-code]', `[data-testid="fcc-row-${OVERSIZED_PATH}"]`),
    hunkRows: await deepCount('[data-line-type]', `[data-testid="fcc-row-${OVERSIZED_PATH}"]`),
  }
  console.log('DIAG after opt-in:', JSON.stringify(after))
  await shot(row, '02-opted-in-control')
  if (after.toggles !== 1 || !after.toggleVisible) {
    throw new Error(`REPRODUCED #13430: the collapse control is missing after opting in: ${JSON.stringify(after)}`)
  }
  // The frame must show the real state it is named for: the plain two-side
  // view gone, the opt-in button gone, exactly one header row (the card's
  // own — Pierre draws the body only), and highlighted hunk rows on screen.
  if (after.plainFallback !== 0 || after.optInButtons !== 0 || after.pierreHeaders !== 1 || after.pierreCode < 1 || after.hunkRows < 1) {
    throw new Error(`opted-in body state gate failed: ${JSON.stringify(after)}`)
  }
  // The kept header must read the same after the swap as before it, and both
  // the same as the header Pierre draws — background, inline padding, metadata
  // group width, and now that it carries counts, their width and alignment.
  const optedHeader = await headerStyle(`[data-testid="fcc-row-${OVERSIZED_PATH}"]`)
  console.log('DIAG kept header AFTER the swap (light DOM):', JSON.stringify(optedHeader))
  const countsMatch = optedHeader?.countMinWidths.length === 2
    && optedHeader.countMinWidths.every(w => w === pierreHeader.countMinWidths[0])
    && optedHeader.countTextAlign.every(a => a === pierreHeader.countTextAlign[0])
  if (!optedHeader || optedHeader.inShadow || !sameLook(pierreHeader, optedHeader) || !sameLook(fallbackHeader, optedHeader) || !countsMatch) {
    throw new Error(`header parity gate failed after the swap: ${JSON.stringify({ pierreHeader, fallbackHeader, optedHeader })}`)
  }

  // ── Frame 3: collapse the opted-in row with that same control ─────────────
  await toggle().first().click()
  await page.waitForTimeout(900)
  const collapsed = {
    toggles: await toggle().count(),
    lightHeader: await page.locator(`[data-testid="fcc-header-${OVERSIZED_PATH}"]`).count(),
    plainSides: await row.locator('[data-pierre-plain-side]').count(),
    pierreCode: await deepCount('[data-code]', `[data-testid="fcc-row-${OVERSIZED_PATH}"]`),
  }
  console.log('DIAG collapsed:', JSON.stringify(collapsed))
  await shot(row, '03-opted-in-collapsed')
  if (collapsed.toggles !== 1 || collapsed.lightHeader !== 1 || collapsed.plainSides !== 0 || collapsed.pierreCode !== 0) {
    throw new Error(`collapsed state gate failed: ${JSON.stringify(collapsed)}`)
  }

  // ── Frame 4: re-expand, then opt in again ─────────────────────────────────
  await toggle().first().click()
  await page.waitForTimeout(1200)
  const reexpanded = {
    toggles: await toggle().count(),
    optInButtons: await row.getByRole('button', { name: 'Show line-by-line diff' }).count(),
    plainSides: await row.locator('[data-pierre-plain-side]').count(),
  }
  console.log('DIAG re-expanded:', JSON.stringify(reexpanded))
  if (reexpanded.toggles !== 1 || reexpanded.optInButtons !== 1) {
    throw new Error(`re-expanded fallback gate failed: ${JSON.stringify(reexpanded)}`)
  }
  await row.getByRole('button', { name: 'Show line-by-line diff' }).click()
  await page.waitForTimeout(3000)
  const reopted = {
    toggles: await toggle().count(),
    toggleVisible: await toggle().count() ? await toggle().first().isVisible() : false,
    optInButtons: await row.getByRole('button', { name: 'Show line-by-line diff' }).count(),
    pierreCode: await deepCount('[data-code]', `[data-testid="fcc-row-${OVERSIZED_PATH}"]`),
    hunkRows: await deepCount('[data-line-type]', `[data-testid="fcc-row-${OVERSIZED_PATH}"]`),
  }
  console.log('DIAG re-opted-in:', JSON.stringify(reopted))
  await shot(row, '04-reexpanded-reopted-in')
  if (reopted.toggles !== 1 || !reopted.toggleVisible || reopted.optInButtons !== 0 || reopted.pierreCode < 1 || reopted.hunkRows < 1) {
    throw new Error(`re-opted-in state gate failed: ${JSON.stringify(reopted)}`)
  }

  console.log('\n── SUMMARY ─────────────────────────────')
  for (const w of wrote) console.log(`${w.over ? 'OVER' : ' ok '}  ${w.w}x${w.h}  ${w.file}`)
  const over = wrote.filter(w => w.over)
  await browser.close()
  srv.close()
  if (over.length) throw new Error(`${over.length} frame(s) exceed ${MAX_EDGE}px on an edge`)
  console.log(`all ${wrote.length} frames within ${MAX_EDGE}px`)
}

main().catch(err => { console.error(err); process.exit(1) })
