/**
 * Screenshot harness + assertions for the AUTONUDGE STATUS-LINE dark-contrast
 * fix (#21335).
 *
 * The goal-loop popover's status line (`<p data-testid="auto-nudge-status">`)
 * renders in three tones through one ternary: a running loop paints
 * `bg-ok-subtle`, a finished one `bg-info-subtle`, and a paused/pending one
 * `bg-warn-subtle`. Before this fix the running (ok) and pending (warn) branches
 * inked their text with `text-ok-fg` / `text-warn-fg` — the on-solid-fill
 * foregrounds, which resolve to black in every dark palette and vanish over the
 * translucent tint. The fix swaps both to the tint-meant `text-ok-text` /
 * `text-warn-text` hues; the info branch already used `text-info-text`.
 *
 * Two loop fixtures cover the two fixed branches:
 *   1. armed + running (active, not held, not waiting) -> ok-subtle status box
 *   2. paused (inactive, user-paused) -> warn-subtle status box
 * Each is shot in dark and light. The dark frames are the evidence that the
 * token swap restored legibility; the light frames confirm no regression.
 *
 * Gateway-free: serveDist + stubDashboardApi drive the REAL built SPA in
 * website/dist with every /api/** answered from fixtures — no gateway, no
 * dashboard auth, no token. It ASSERTS the status box carries the fixed token
 * (never the old `-fg` one), so a stale bundle reds instead of photographing the
 * old copy.
 *
 * Usage: node scripts/capture-21335-autonudge-status-contrast.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { goalPopoverSlots, goalPopoverDetail } from './lib/goal-popover-fixture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/autonudge-status-contrast-21335'
const SLOT = 'chat-goal'
const PROJECT = '/home/user/workspace/notes'

mkdirSync(OUT, { recursive: true })

const slots = goalPopoverSlots(SLOT, PROJECT)
const detail = goalPopoverDetail(PROJECT)

/** Fixed instant so the countdown string, and thus the committed PNG, is stable. */
const FIXED_FIRE_TS = Date.UTC(2026, 9, 20, 12, 20, 0) / 1000

/** An armed loop shaped like the backend's `asdict(loop)`. */
const makeLoop = over => ({
  id: 'loop-21335',
  slot_key: SLOT,
  message: 'Fold every uploader retry path into the shared backoff helper.',
  idle_secs: 300,
  max_cycles: 24,
  cycle_count: 7,
  active: true,
  waiting_on_person: false,
  last_fire_ts: FIXED_FIRE_TS,
  next_due_ts: 0,
  ...over,
})

/** 1 = running -> ok-subtle box; 2 = paused -> warn-subtle box. */
const RUNNING = makeLoop()
const PAUSED = makeLoop({ active: false, stopped_reason: 'user_paused' })

const { srv, base } = await serveDist()
const browser = await chromium.launch(
  process.env.PW_CHROME_BIN ? { executablePath: process.env.PW_CHROME_BIN } : {},
)
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  deviceScaleFactor: 2,
  locale: 'en-US',
})

async function load(loop, theme) {
  const page = await context.newPage()
  logPageProblems(page)

  const extra = async (path, route) => {
    if (path === `/api/autonudge/slot/${SLOT}`) { await json(route, { enabled: true, loop }); return true }
    if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [loop] }); return true }
    if (path === `/api/monitors/slot/${SLOT}`) { await json(route, { enabled: true, monitor: null }); return true }
    if (path === '/api/monitors') { await json(route, { enabled: true, monitors: [] }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, {
    slots,
    theme,
    extra,
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  return page
}

const results = []
const check = (name, ok, detailText) => {
  results.push({ name, ok, detail: detailText })
  if (!ok) console.error(`FAIL ${name}: ${detailText}`)
}

/** Open the goal popover and shoot the status box plus a band of context. */
async function shoot(loop, theme, name, wantToken, wantTone) {
  const page = await load(loop, theme)
  // An active loop renders a "Goal active (cycle …)" chip; an inactive/paused
  // loop collapses back to the "Set a goal" chip — either opens the popover
  // whose status line is the box under test.
  const chip = page.getByRole('button', { name: /^(Goal (active|paused)|Set a goal)/ }).first()
  await chip.waitFor({ state: 'visible', timeout: 15000 })
  await chip.click()
  await page.waitForTimeout(700)

  const status = page.getByTestId('auto-nudge-status').first()
  await status.waitFor({ state: 'visible', timeout: 10000 })
  const cls = (await status.getAttribute('class')) || ''

  const box = await status.boundingBox()
  if (!box) throw new Error(`${name}: status box has no bounding box`)
  const out = join(OUT, name)
  await page.screenshot({
    path: out,
    clip: {
      x: Math.max(0, box.x - 16),
      y: Math.max(0, box.y - 44),
      width: Math.min(560, box.width + 48),
      height: box.height + 72,
    },
  })
  console.log('wrote', out)

  check(`${name} tone`, cls.includes(wantTone), `class=${JSON.stringify(cls)} missing ${wantTone}`)
  check(`${name} token`, cls.includes(wantToken), `class=${JSON.stringify(cls)} missing ${wantToken}`)
  check(`${name} no -fg`, !/\btext-(ok|warn)-fg\b/.test(cls), `class=${JSON.stringify(cls)} still carries an on-solid -fg token`)
  await page.close()
}

// Running loop -> ok-subtle status box, both themes.
await shoot(RUNNING, 'dark', '01-running-ok-dark.png', 'text-ok-text', 'bg-ok-subtle')
await shoot(RUNNING, 'light', '02-running-ok-light.png', 'text-ok-text', 'bg-ok-subtle')
// Paused loop -> warn-subtle status box, both themes.
await shoot(PAUSED, 'dark', '03-paused-warn-dark.png', 'text-warn-text', 'bg-warn-subtle')
await shoot(PAUSED, 'light', '04-paused-warn-light.png', 'text-warn-text', 'bg-warn-subtle')

await browser.close()
srv.close()

console.log('--- assertions (each status box must carry the fixed tint token, never -fg) ---')
for (const r of results) console.log(JSON.stringify(r))

if (!results.every(r => r.ok)) {
  console.error('FAIL: a status box did not render the fixed token — fix the fixture/build, do not commit the PNG')
  process.exit(1)
}
console.log('OK')
