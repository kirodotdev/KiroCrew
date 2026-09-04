/**
 * Narrow-viewport (320px) screenshot for the file-change card's split toggle
 * (#6024 follow-up). The card header wraps (`flex-wrap`) instead of clipping
 * the toggle under the card's `overflow-hidden` — worst case exercised here:
 * German labels („2 Dateien geändert · Ergänzungen/Entfernungen") at 320px.
 *
 * Frames:
 *   05-card-narrow-320-de   the card at a 320px viewport, German locale, one
 *                           row expanded: the split toggle is fully visible
 *                           (wrapped onto the header's second line) and lit in
 *                           the split default.
 *   06-card-narrow-320-de-unified  the same card after CLICKING that toggle —
 *                           proof it is hit-testable at 320px, not just
 *                           painted: the row re-renders unified and
 *                           `mc-diff-split=0` is persisted.
 *
 * Usage: node scripts/capture-diff-split-narrow.mjs [outDir]
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

const BEFORE = `export function isParked(item: Item): boolean {
  if (item.phase !== 'snoozed') return false
  return true
}`

const AFTER = `export function isParked(item: Item): boolean {
  if (item.phase !== 'snoozed') return false
  if (!item.snooze_until) return true
  return new Date(item.snooze_until).getTime() > Date.now()
}`

const FILE_CHANGES = [
  { path: 'website/src/state/parked.ts', before: BEFORE, after: AFTER },
  { path: 'website/src/state/groups.ts', before: 'const a = 1\n', after: 'const a = 2\n' },
]
const EXPAND_PATH = 'website/src/state/parked.ts'

const t0 = Math.floor(Date.now() / 1000) - 900
const SURFACE = {
  key: 'chat-split-narrow',
  title: 'Split toggle at 320px',
  messages: [
    { role: 'user', content: 'Fix the snooze grouping.', ts: String(t0) },
    {
      role: 'assistant',
      ts: String(t0 + 120),
      content: 'Done — parked items now leave the groups until their wake-up time.',
      meta: { file_changes: FILE_CHANGES },
    },
  ],
}

const slots = [{
  key: SURFACE.key,
  title: SURFACE.title,
  running: false,
  last_message: SURFACE.title,
  messages: SURFACE.messages.length,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detailByKey = {
  [SURFACE.key]: { running: false, has_more: false, total: SURFACE.messages.length, queue: [], messages: SURFACE.messages },
}

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({
    viewport: { width: 320, height: 740 },
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

  // German locale: the longest header labels of the shipped catalogs.
  await page.addInitScript(([key]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-lang', 'de')
    localStorage.setItem('mc-active-slot-chat', key)
    localStorage.setItem('mc-chat-config', JSON.stringify({
      pinLastPrompt: false,
      fileChipStyle: 'expanded',
      streamMode: 'immediate',
    }))
  }, [SURFACE.key])
  await page.goto(base + '/?sid=' + encodeURIComponent(SURFACE.key), { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  await page.keyboard.press('Escape')
  const close = page.locator('[aria-label="Close"], [aria-label="Schließen"]')
  if (await close.count()) await close.first().click().catch(() => {})
  await page.waitForTimeout(400)

  const card = page.locator('div.ft-block-reveal:has([data-testid^="fcc-row-"])').first()
  await card.waitFor({ state: 'visible', timeout: 15000 })
  await page.locator(`[data-testid="fcc-toggle-${EXPAND_PATH}"]`).click()
  await page.waitForTimeout(1600)

  // The toggle must be VISIBLE at 320px (wrapped, not clipped by overflow-hidden).
  const toUnified = page.locator('[aria-label="Zur einheitlichen Ansicht wechseln"]')
  if (!(await toUnified.count())) throw new Error('expected the card toggle present in split mode (default)')
  const box = await toUnified.boundingBox()
  const cardBox = await card.boundingBox()
  if (!box) throw new Error('toggle has no bounding box — clipped or hidden at 320px')
  if (box.x < cardBox.x || box.x + box.width > cardBox.x + cardBox.width + 0.5) {
    throw new Error(`toggle overflows the card horizontally at 320px: toggle=${JSON.stringify(box)} card=${JSON.stringify(cardBox)}`)
  }
  console.log(`toggle box at 320px: x=${box.x.toFixed(1)} w=${box.width} (card w=${cardBox.width}) — inside the card`)
  await shot(card, '05-card-narrow-320-de')

  // And CLICKABLE — Playwright refuses clicks on covered/clipped targets.
  await toUnified.click()
  await page.waitForTimeout(1600)
  const toSplit = page.locator('[aria-label="Zur geteilten Ansicht wechseln"]')
  if (!(await toSplit.count())) throw new Error('expected the toggle dimmed in unified mode after the click')
  const persisted = await page.evaluate(() => localStorage.getItem('mc-diff-split'))
  if (persisted !== '0') throw new Error(`expected mc-diff-split=0 after toggling, got ${persisted}`)
  await shot(card, '06-card-narrow-320-de-unified')

  await browser.close()
  srv.close()

  const over = wrote.filter(x => x.over)
  if (over.length) throw new Error('frames over the 2000px budget: ' + over.map(x => x.file).join(', '))
  console.log(`done — ${wrote.length} frames in ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
