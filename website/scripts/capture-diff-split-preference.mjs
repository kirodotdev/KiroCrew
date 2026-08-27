/**
 * Screenshot harness for the shared split/unified diff preference (#6024).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, every /api/** answered from fixtures (gateway-free). Two chat
 * surfaces are exercised: the tool-derived file-change card (FileChangeChips)
 * and a fenced ```diff block (DiffBlock).
 *
 * Frames:
 *   01-card-split-default    the file-change card with one row expanded and NO
 *                            persisted preference: the row renders SPLIT (the
 *                            shared default) and the new card-header toggle is
 *                            lit. Before this change the card had no toggle and
 *                            was pinned unified.
 *   02-card-unified-toggled  the same card after clicking its toggle: the row
 *                            re-renders unified, the toggle dims, and the
 *                            choice lands in `mc-diff-split`.
 *   03-fence-follows-pref    the chat ```diff fence loaded with the preference
 *                            the card wrote (`mc-diff-split=0`): it starts
 *                            UNIFIED — the two chat surfaces now share one
 *                            preference instead of each holding its own state.
 *   04-fence-default-split   the fence with no persisted preference: SPLIT by
 *                            default, matching the side panel's default.
 *
 * Element screenshots only (the transcript is taller than the 2000px-per-edge
 * budget at deviceScaleFactor 2); dimensions are asserted after each write.
 *
 * Usage: node scripts/capture-diff-split-preference.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/diff-split-preference'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const MAX_EDGE = 2000

mkdirSync(OUT, { recursive: true })

// ── Fixtures ────────────────────────────────────────────────────────────────

/* Real before/after text: the row's ±counts are computed client-side. Short
 * lines on purpose — split halves a chat-width column, so the frame must stay
 * readable at half width. */
const BEFORE = `export function isParked(item: Item): boolean {
  if (item.phase !== 'snoozed') return false
  return true
}

export function groups(items: Item[]): Group[] {
  return build(items)
}`

const AFTER = `export function isParked(item: Item): boolean {
  if (item.phase !== 'snoozed') return false
  if (!item.snooze_until) return true
  const until = new Date(item.snooze_until)
  return until.getTime() > Date.now()
}

export function groups(items: Item[]): Group[] {
  return build(items.filter(i => !isParked(i)))
}`

const FILE_CHANGES = [
  { path: 'website/src/state/parked.ts', before: BEFORE, after: AFTER },
  { path: 'website/src/state/groups.ts', before: 'const a = 1\n', after: 'const a = 2\n' },
]
const EXPAND_PATH = 'website/src/state/parked.ts'

const FENCE_DIFF = [
  '```diff',
  '--- a/website/src/state/parked.ts',
  '+++ b/website/src/state/parked.ts',
  '@@ -1,4 +1,6 @@',
  ' export function isParked(item: Item): boolean {',
  "   if (item.phase !== 'snoozed') return false",
  '-  return true',
  '+  if (!item.snooze_until) return true',
  '+  return new Date(item.snooze_until).getTime() > Date.now()',
  ' }',
  '```',
].join('\n')

const t0 = Math.floor(Date.now() / 1000) - 900

const SURFACES = {
  card: {
    key: 'chat-split-card',
    title: 'Shared split preference — card',
    messages: [
      { role: 'user', content: 'Fix the snooze grouping.', ts: String(t0) },
      {
        role: 'assistant',
        ts: String(t0 + 120),
        content: 'Done — parked items now leave the groups until their wake-up time.',
        meta: { file_changes: FILE_CHANGES },
      },
    ],
  },
  fence: {
    key: 'chat-split-fence',
    title: 'Shared split preference — fence',
    messages: [
      { role: 'user', content: 'Show me the patch.', ts: String(t0) },
      { role: 'assistant', ts: String(t0 + 60), content: 'Here it is:\n\n' + FENCE_DIFF },
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

// ── Harness ─────────────────────────────────────────────────────────────────

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

/** Chromium resolution lives in ./lib/chromium-executable.mjs (shared across the capture harnesses). */


async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/chat/slots') return json(route, slots), true
    const m = /^\/api\/chat\/slots\/([^/]+)/.exec(path)
    if (m) {
      const d = detailByKey[decodeURIComponent(m[1])]
      if (d) return json(route, d), true
    }
    // DiffBlock HEAD-probes before showing Open; 200 keeps the header controls
    // complete in the fence frames.
    if (path === '/api/file-read') return route.fulfill({ status: 200, body: '' }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  const wrote = []
  async function shot(locator, name) {
    const file = `${OUT}/${name}.png`
    await locator.screenshot({ path: file })
    const { w, h } = pngSize(file)
    const flag = w > MAX_EDGE || h > MAX_EDGE ? '  ⚠️ OVER 2000px' : ''
    console.log(`wrote ${file}  ${w}x${h}${flag}`)
    wrote.push({ file, w, h, over: !!flag })
  }

  /** Seed localStorage and navigate. `diffSplit` is the persisted preference
   *  under test: undefined leaves `mc-diff-split` ABSENT (the unseeded default
   *  path), '0'/'1' plant a prior choice. */
  async function load(surface, { diffSplit } = {}) {
    await page.addInitScript(([key, split]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', key)
      if (split !== null) localStorage.setItem('mc-diff-split', split)
      localStorage.setItem('mc-chat-config', JSON.stringify({
        pinLastPrompt: false,
        fileChipStyle: 'expanded',
        streamMode: 'immediate',
      }))
    }, [surface.key, diffSplit ?? null])
    await page.goto(base + '/?sid=' + encodeURIComponent(surface.key), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    await page.keyboard.press('Escape')
    const close = page.locator('[aria-label="Close"]')
    if (await close.count()) await close.first().click().catch(() => {})
    await page.waitForTimeout(400)
  }

  // ── Frames 1-2: the file-change card ──────────────────────────────────────
  await load(SURFACES.card)
  const card = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])').first()
  await card.waitFor({ state: 'visible', timeout: 15000 })
  await page.locator(`[data-testid="fcc-toggle-${EXPAND_PATH}"]`).click()
  await page.waitForTimeout(1600)

  // The unseeded default is split: the card toggle must offer the way back.
  const toUnified = page.locator('[aria-label="Switch to unified view"]')
  if (!(await toUnified.count())) throw new Error('expected the card toggle lit in split mode (default)')
  await shot(card, '01-card-split-default')

  await toUnified.click()
  await page.waitForTimeout(1600)
  const toSplit = page.locator('[aria-label="Switch to split view"]')
  if (!(await toSplit.count())) throw new Error('expected the card toggle dimmed in unified mode after the click')
  const persisted = await page.evaluate(() => localStorage.getItem('mc-diff-split'))
  if (persisted !== '0') throw new Error(`expected mc-diff-split=0 after toggling, got ${persisted}`)
  await shot(card, '02-card-unified-toggled')

  // ── Frame 3: the fence follows the preference the card just wrote ─────────
  await load(SURFACES.fence, { diffSplit: '0' })
  const fence = page.locator('.diff-block').first()
  await fence.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1200)
  // Hover-revealed controls: the button offering split proves it sits unified.
  await fence.hover()
  const fenceToSplit = page.locator('[aria-label="Switch to split view"]')
  if (!(await fenceToSplit.count())) throw new Error('expected the fence unified under mc-diff-split=0')
  await shot(fence, '03-fence-follows-pref')

  // ── Frame 4: the fence with nothing persisted — split by default ──────────
  await load(SURFACES.fence)
  const fence2 = page.locator('.diff-block').first()
  await fence2.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1200)
  await fence2.hover()
  const fenceToUnified = page.locator('[aria-label="Switch to unified view"]')
  if (!(await fenceToUnified.count())) throw new Error('expected the fence split by default (unseeded)')
  await shot(fence2, '04-fence-default-split')

  await browser.close()
  srv.close()

  const over = wrote.filter(x => x.over)
  if (over.length) throw new Error('frames over the 2000px budget: ' + over.map(x => x.file).join(', '))
  console.log(`done — ${wrote.length} frames in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
