/**
 * Screenshot harness for the no-backdrop-filter glass fallback (issue #1817).
 *
 * A feature query cannot be exercised by vitest/jsdom, and desktop Chromium
 * always parses backdrop-filter, so `@supports not (...)` never matches in the
 * capture browser itself. The fallback branch is therefore proven on a SCRATCH
 * COPY of the built CSS with the feature-query condition rewritten to a
 * tautology (`(color: red)`), exactly as the PR body states. The block's BODY
 * is byte-identical to what ships; only the guard is forced on.
 *
 * Frames (all against the REAL built SPA in website/dist, gateway-free):
 *   01-glass-normal    unmodified dist: the bell panel over busy chat content,
 *                      backdrop-filter working -> translucent glass cards.
 *                      ASSERTS the row card's background-color is NOT opaque.
 *   02-fallback-solid  scratch dist (query forced): same panel, cards solid.
 *                      ASSERTS the row card's computed background-color is a
 *                      fully opaque rgb() -- the #1817 symptom is gone.
 *   03-fallback-light  same forced build in light theme, proving the fallback
 *                      follows the theme variables rather than hardcoding.
 *
 * Usage: node scripts/capture-glass-surface-fallback.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, mkdtempSync, cpSync, readFileSync, writeFileSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/glass-surface-fallback'
mkdirSync(OUT, { recursive: true })

const NOTES = [
  {
    kind: 'cron', source: 'system', channel: 'system.cron', priority: 'default',
    title: 'Nightly registry sweep', body: 'Checked 41 entries, nothing to do.',
    ts: '2026-08-10T06:05:00.000000+00:00', acked: false,
  },
  {
    kind: 'subagent', source: 'system', channel: 'system.subagent', priority: 'default',
    title: 'Research agent finished', body: 'Summarized 12 sources into the draft. Two need citations.',
    ts: '2026-08-10T05:40:00.000000+00:00', acked: true,
  },
]

const SLOTS = [{
  key: 'chat-glass',
  title: 'Glass fallback demo',
  running: false,
  last_message: 'The notifications panel floats over this content.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const DETAIL = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: '',
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 90, content: 'Show me the notifications panel over some busy chat content, please — enough text that transparency would be obvious.' },
    { role: 'assistant', ts: Date.now() / 1000 - 60, content: 'Here is a healthy block of prose so the area behind the bell panel is visually busy. If the panel were transparent, these words would bleed straight through the notification cards and make them unreadable — which is exactly the defect reported on the Linux AppImage build in issue #1817.' },
  ],
}

const apiExtra = async (path, route) => {
  if (path === '/api/notifications') {
    await json(route, { notifications: NOTES, unread: 1 })
    return true
  }
  if (path.startsWith('/api/chat/slot/')) {
    await json(route, DETAIL)
    return true
  }
  return false
}

/** Copy dist to a scratch dir and rewrite the fallback's feature-query guard
 *  to a tautology so the (otherwise unreachable-in-Chromium) branch applies.
 *  The rule body is untouched — this forces the guard, not the behavior. */
function makeForcedDist() {
  const scratch = mkdtempSync(join(tmpdir(), 'kc-1817-forced-dist-'))
  cpSync('dist', scratch, { recursive: true })
  const assets = join(scratch, 'assets')
  let rewrote = 0
  for (const f of readdirSync(assets)) {
    if (!f.endsWith('.css')) continue
    const p = join(assets, f)
    const css = readFileSync(p, 'utf8')
    // Minifier may order the two prefixed conditions either way; match both.
    const re = /@supports not \(\((?:-webkit-)?backdrop-filter:blur\(1px\)\) or \((?:-webkit-)?backdrop-filter:blur\(1px\)\)\)/
    if (re.test(css)) {
      writeFileSync(p, css.replace(re, '@supports (color: red)'))
      rewrote++
    }
  }
  if (rewrote !== 1) throw new Error(`expected to rewrite exactly 1 CSS asset, rewrote ${rewrote}`)
  return scratch
}

async function openBellPanel(page, base, { theme } = {}) {
  // stubDashboardApi seeds localStorage itself (and clears it first), so the
  // mode preference must go through its `theme` option, not a second init script.
  await stubDashboardApi(page, { slots: SLOTS, extra: apiExtra, ...(theme ? { theme } : {}) })
  // The home hero's large title extends under the right-side bell panel, so
  // there is high-contrast content BEHIND the cards in every frame — where
  // transparency (and its fix) is visible in pixels.
  await page.goto(`${base}/`, { waitUntil: 'networkidle' })
  const bell = page.locator('button[aria-label*="otification"], button:has(svg.lucide-bell)').first()
  await bell.click()
  await page.getByText('Nightly registry sweep').first().waitFor()
  // Let the open animation settle so the frame is representative.
  await page.waitForTimeout(400)
}

/** Computed background-color of the first notification row card. */
async function cardBg(page) {
  return page.evaluate(() => {
    const el = document.querySelector('[data-notif-row].notif-material, .notif-material[data-notif-row]')
    if (!el) throw new Error('no .notif-material notification row found')
    return getComputedStyle(el).backgroundColor
  })
}

const isOpaque = bg => /^rgb\(/.test(bg) // rgba(...) or color-mix result with alpha would serialize with alpha
const PANEL_CLIP = { x: 850, y: 48, width: 430, height: 340 } // right-side bell panel region
const shot = async (page, name) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, animations: 'disabled' })
  await page.screenshot({ path: `${OUT}/${name}-closeup.png`, animations: 'disabled', clip: PANEL_CLIP })
}

async function main() {
  const browser = await chromium.launch()

  // ── 01: unmodified dist — glass look must be unchanged ──
  {
    const { srv, base } = await serveDist()
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await openBellPanel(page, base)
    const bg = await cardBg(page)
    if (isOpaque(bg)) throw new Error(`01-glass-normal: expected translucent card, got opaque ${bg}`)
    await shot(page, '01-glass-normal')
    console.log(`01-glass-normal OK (card bg: ${bg})`)
    await page.close(); srv.close()
  }

  // ── 02 + 03: forced-fallback scratch dist — cards must be solid ──
  const forced = makeForcedDist()
  {
    const { srv, base } = await serveDist(forced)
    const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(page)
    await openBellPanel(page, base)
    const bg = await cardBg(page)
    if (!isOpaque(bg)) throw new Error(`02-fallback-solid: expected opaque card, got ${bg}`)
    await shot(page, '02-fallback-solid')
    console.log(`02-fallback-solid OK (card bg: ${bg})`)
    await page.close()

    const light = await browser.newPage({ viewport: { width: 1280, height: 860 } })
    logPageProblems(light)
    await openBellPanel(light, base, { theme: 'light' })
    const lbg = await cardBg(light)
    if (!isOpaque(lbg)) throw new Error(`03-fallback-light: expected opaque card, got ${lbg}`)
    await shot(light, '03-fallback-light')
    console.log(`03-fallback-light OK (card bg: ${lbg})`)
    await light.close(); srv.close()
  }

  await browser.close()
  console.log('all frames captured + asserted')
}

main().catch(err => { console.error(err); process.exit(1) })
