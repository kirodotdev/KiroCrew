/**
 * Screenshot harness + assertions for the ARMED-LOOP CYCLE CAP READOUT (#7410).
 *
 * An armed auto-nudge loop used to publish its cycle count as a bare number, so
 * `23` told the user how many cycles had fired but not how many were left before
 * the backstop stopped the loop. The chip, its tooltip, the popover header and
 * the chip's aria-label now all read `23/24` against a finite cap, and a loop
 * armed with `max_cycles: 0` — which means INFINITE — deliberately keeps the bare
 * count, because a loop with no backstop has no denominator to count toward.
 *
 * Three frames are three FIXTURE STATES rather than one click sequence: the
 * readout is a property of the loop the slot is running, and that loop arrives
 * from `GET /api/autonudge/slot/<key>` (ChatPage.tsx:1699). Seeding the response
 * shows exactly what a user sees in each steady state:
 *   1. finite cap    (cycle_count 23, max_cycles 24) -> chip reads 23/24
 *   2. infinite cap  (cycle_count 31, max_cycles 0)  -> chip reads 31, no slash
 *   3. popover open on the finite loop -> header reads `· cycle 23/24` beside the
 *      Max cycles field, which is untouched and still holds the editable 24
 *
 * This ASSERTS as well as photographs, because a PNG cannot fail. It drives the
 * REAL built SPA (website/dist) behind `serveDist` with every /api/** call
 * answered from fixtures by `stubDashboardApi` — no gateway, no dashboard auth,
 * no kiro-cli — and exits non-zero unless each frame's rendered text is the text
 * the PR claims. A stale bundle therefore reds instead of quietly photographing
 * the old copy, and a blank frame cannot be committed as evidence.
 *
 * The TOOLTIP is a native `title` attribute, which no browser paints into a
 * screenshot, so it is verified by reading the attribute and printed in the
 * assertion block rather than photographed. Same for `aria-label`.
 *
 * Usage: node scripts/capture-loop-cycle-cap.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/loop-cycle-cap-7410'
const SLOT = 'chat-loop'
const PROJECT = '/home/user/workspace/uploader'

mkdirSync(OUT, { recursive: true })

const NOW = Math.floor(Date.now() / 1000)
/**
 * Fixed wall-clock instant for the loop's own timestamps, so the popover's
 * "Last fire" line renders the same string on every run. A now-relative value
 * would rewrite the committed PNG's bytes on every re-capture, which turns a
 * re-pin after a rebase into a pointless binary diff.
 */
const FIXED_FIRE_TS = Date.UTC(2026, 8, 5, 12, 20, 0) / 1000

const slots = [{
  key: SLOT,
  title: 'Clear the flaky uploader retries',
  running: false,
  last_message: 'Cycle 23 done — two retry paths left to fold.',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: NOW,
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: NOW - 900,
      content: 'Keep working the uploader retry ladder until the flakes are gone.',
    },
    {
      role: 'assistant',
      ts: NOW - 120,
      content: 'Cycle 23 done — two retry paths left to fold into the shared backoff helper.',
    },
  ],
}

/**
 * One armed loop, shaped like the backend's `asdict(loop)`.
 *
 * `next_due_ts` is 0 on purpose: a live countdown would put a per-second value
 * in the chip's tooltip, so two runs of this harness would never produce the
 * same bytes and the committed PNGs would churn on every re-capture.
 */
const makeLoop = over => ({
  id: 'loop-7410',
  slot_key: SLOT,
  message: 'Fold every uploader retry path into the shared backoff helper.',
  idle_secs: 300,
  max_cycles: 24,
  cycle_count: 23,
  active: true,
  last_fire_ts: FIXED_FIRE_TS,
  next_due_ts: 0,
  ...over,
})

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  // The chip is 11px type inside a 24px control; 1x renders "23/24" soft enough
  // on GitHub that a reviewer cannot read the denominator.
  deviceScaleFactor: 2,
})

/** Boot the chat page with `loop` seeded as this slot's armed loop. */
async function load(loop) {
  const page = await context.newPage()
  logPageProblems(page)

  /** Each branch AWAITS `json()` then returns true; a falsy return means "not handled". */
  const extra = async (path, route) => {
    if (path === `/api/autonudge/slot/${SLOT}`) { await json(route, { loop }); return true }
    // The cold seed for the sidebar's own "Loop N/M" subtitle. Kept consistent
    // with the chip's loop so the two surfaces in one frame cannot contradict
    // each other, even though stubDashboardApi swallows the websocket that
    // normally triggers this read.
    if (path === '/api/autonudge') {
      await json(route, { enabled: true, loops: [loop] })
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  await stubDashboardApi(page, {
    slots,
    extra,
    // Pin the locale: without it the SPA negotiates one from the environment and
    // the frame comes out in whatever language the runner happens to pick.
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-lang': 'en' },
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  return page
}

/** The composer's goal chip, addressed by the accessible name the PR changed. */
const chipOf = page => page.getByRole('button', { name: /^Goal active \(cycle / }).first()

/**
 * Crop the composer control row around the chip. A tight crop of the chip alone
 * is 40px wide and proves nothing about where the reader would find it, so the
 * band runs from the composer's leading control through the chip's whole row.
 */
async function shootBand(page, chip, name) {
  const box = await chip.boundingBox()
  if (!box) throw new Error(`${name}: chip has no bounding box`)
  const out = join(OUT, name)
  await page.screenshot({
    path: out,
    clip: {
      // 56px of lead-in: enough to include the attach (+) control to the chip's
      // left, so the frame shows the chip in its row rather than floating.
      x: Math.max(0, box.x - 56),
      y: Math.max(0, box.y - 58),
      width: 780,
      height: box.height + 96,
    },
  })
  console.log('wrote', out)
}

const results = []
const check = (name, ok, detailText) => {
  results.push({ name, ok, detail: detailText })
  if (!ok) console.error(`FAIL ${name}: ${detailText}`)
}

// 1 — finite cap: the chip carries the denominator.
{
  const page = await load(makeLoop())
  const chip = chipOf(page)
  await chip.waitFor({ state: 'visible', timeout: 15000 })
  const text = (await chip.innerText()).trim()
  const title = await chip.getAttribute('title')
  const aria = await chip.getAttribute('aria-label')
  await shootBand(page, chip, '01-chip-finite-cap-23-of-24.png')
  check('01 chip text', text === '23/24', `chip reads ${JSON.stringify(text)}, want "23/24"`)
  check('01 tooltip', title === 'Goal active (cycle 23/24)', `title=${JSON.stringify(title)}`)
  check('01 aria-label', aria === 'Goal active (cycle 23/24)', `aria-label=${JSON.stringify(aria)}`)
  await page.close()
}

// 2 — max_cycles 0 means infinite, so there is no denominator to show.
{
  const page = await load(makeLoop({ cycle_count: 31, max_cycles: 0 }))
  const chip = chipOf(page)
  await chip.waitFor({ state: 'visible', timeout: 15000 })
  const text = (await chip.innerText()).trim()
  const title = await chip.getAttribute('title')
  await shootBand(page, chip, '02-chip-infinite-cap-bare-31.png')
  check('02 chip text', text === '31', `chip reads ${JSON.stringify(text)}, want "31"`)
  check('02 no slash', !text.includes('/'), `chip reads ${JSON.stringify(text)} — must carry no slash`)
  check('02 tooltip', title === 'Goal active (cycle 31)', `title=${JSON.stringify(title)}`)
  await page.close()
}

// 3 — the popover header carries the same readout, beside the untouched field
//     the cap is actually edited in.
{
  const page = await load(makeLoop())
  const chip = chipOf(page)
  await chip.waitFor({ state: 'visible', timeout: 15000 })
  await chip.click()
  const popover = page.getByRole('dialog').filter({ hasText: 'Set a goal' }).first()
  await popover.waitFor({ state: 'visible', timeout: 10000 })
  // Radix plays a zoom/fade entry animation; shoot after it settles.
  await page.waitForTimeout(700)

  const headerText = (await popover.innerText()).split('\n').slice(0, 2).join(' ')
  const capField = popover.getByRole('spinbutton', { name: 'Max cycles (0 = infinite)' })
  const capValue = await capField.inputValue()

  const out = join(OUT, '03-popover-header-and-cap-field.png')
  await popover.screenshot({ path: out })
  console.log('wrote', out)

  check('03 popover header', /·\s*cycle\s*23\/24/.test(await popover.innerText()),
    `popover head reads ${JSON.stringify(headerText)}, want "· cycle 23/24"`)
  check('03 cap field untouched', capValue === '24', `Max cycles field holds ${JSON.stringify(capValue)}, want "24"`)
  await page.close()
}

await browser.close()
srv.close()

console.log('--- assertions (each frame must render the readout the PR claims) ---')
for (const r of results) console.log(JSON.stringify(r))

if (!results.every(r => r.ok)) {
  console.error('FAIL: a frame did not render the cycle-cap readout — fix the fixture, do not commit the PNG')
  process.exit(1)
}
console.log('OK')
