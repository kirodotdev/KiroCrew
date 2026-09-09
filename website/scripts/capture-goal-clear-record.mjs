/**
 * Screenshot harness + assertions for CLEARING a stopped automation record.
 *
 * The goal popover's left button did two different things under one label. On a
 * live loop it stops the loop. On a loop that is ALREADY stopped there is
 * nothing left to stop: the press removes the record, which is the only way a
 * session whose structured monitor was stopped can ever watch a different
 * subject (a retained stop refuses a re-arm). Labelled "Stop loop" in both
 * states, the second press read as a no-op -- and until this PR it WAS one, so
 * the label was accidentally honest about a bug.
 *
 * Two frames, because one of them is the control:
 *
 *   1. live loop    -> "Stop loop". Unchanged behaviour, photographed so the
 *                      new label is proven to be conditional rather than a
 *                      rename of the only state.
 *   2. stopped loop -> "Clear record", and pressing it really issues the DELETE
 *                      and tears the popover down. A still of a label cannot
 *                      show that the press does anything, so the press is
 *                      performed and its request observed.
 *
 * This ASSERTS as well as photographs, because a PNG cannot fail. It drives the
 * REAL built SPA (website/dist) behind `serveDist` with every /api/** call
 * answered from fixtures by `stubDashboardApi` -- no gateway, no dashboard auth,
 * no kiro-cli -- and exits non-zero unless each frame renders what the PR
 * claims. Labels are read from the CATALOG, so a key rename breaks the capture
 * loudly instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-goal-clear-record.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/goal-clear-record'
const SLOT = 'chat-loop'
const PROJECT = '/home/user/workspace/uploader'
const LOOP_ID = 'mon-9615'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const gen = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const STOP = gen.components.autoNudgePopover.stop_loop
const CLEAR = manual.components.autoNudgePopover.clear_record
const SAVE = manual.components.autoNudgePopover.save
const START = manual.components.autoNudgePopover.start_loop
if (!STOP || !CLEAR || !SAVE || !START) {
  throw new Error('components.autoNudgePopover stop/clear/save/start keys missing -- renamed?')
}

const NOW = Math.floor(Date.now() / 1000)
/** Fixed instant so the "Last fire" line renders identical bytes on every run. */
const FIXED_FIRE_TS = Date.UTC(2026, 8, 9, 14, 11, 0) / 1000

const slots = [{
  key: SLOT,
  title: 'Watch PR 9615 to review-ready',
  running: false,
  last_message: 'Monitor stopped. Nothing is watching the PR now.',
  messages: 6,
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
    { role: 'user', ts: NOW - 900, content: 'Stop watching 9615 for now.' },
    { role: 'assistant', ts: NOW - 120, content: 'Stopped. The record is kept for inspection.' },
  ],
}

const makeLoop = over => ({
  id: LOOP_ID,
  slot_key: SLOT,
  message: 'Check https://github.com/kirodotdev/KiroCrew/pull/9615 for new CI results and review comments.',
  idle_secs: 420,
  max_cycles: 8,
  cycle_count: 3,
  active: true,
  last_fire_ts: FIXED_FIRE_TS,
  next_due_ts: 0,
  ...over,
})

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1500, height: 950 },
  // The action row is 12px type; 1x renders the button label soft enough on
  // GitHub that a reviewer cannot read it.
  deviceScaleFactor: 2,
})

/** Boot the chat page with `loop` seeded, recording any DELETE the popover sends. */
async function load(loop) {
  const page = await context.newPage()
  logPageProblems(page)
  const deletes = []

  const extra = async (path, route) => {
    if (path === `/api/autonudge/${LOOP_ID}` && route.request().method() === 'DELETE') {
      deletes.push(path)
      await json(route, { ok: true, cleared: true })
      return true
    }
    if (path === `/api/autonudge/slot/${SLOT}`) { await json(route, { loop }); return true }
    if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [loop] }); return true }
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
  return { page, deletes }
}

/** Open the goal popover from the composer chip and return it. */
async function openPopover(page) {
  const chip = page
    .getByRole('button', { name: /^(Goal active \(cycle |Set a goal$)/ })
    .first()
  await chip.waitFor({ state: 'visible', timeout: 15000 })
  await chip.click()
  const popover = page.getByRole('dialog').filter({ hasText: 'Set a goal' }).first()
  await popover.waitFor({ state: 'visible', timeout: 10000 })
  // Radix plays a zoom/fade entry animation; shoot after it settles.
  await page.waitForTimeout(700)
  return popover
}

const results = []
const check = (name, ok, detailText) => {
  results.push({ name, ok, detail: detailText })
  if (!ok) console.error(`FAIL ${name}: ${detailText}`)
}

async function shoot(popover, name) {
  const out = join(OUT, name)
  await popover.screenshot({ path: out })
  console.log('wrote', out)
}

// 1 -- live loop: the label is unchanged. The control frame.
{
  const { page } = await load(makeLoop())
  const popover = await openPopover(page)
  await shoot(popover, '01-live-loop-stop-loop.png')
  check('01 reads Stop loop', (await popover.getByRole('button', { name: STOP }).count()) === 1,
    `"${STOP}" not found on a live loop`)
  check('01 does not read Clear record', (await popover.getByRole('button', { name: CLEAR }).count()) === 0,
    `"${CLEAR}" offered on a LIVE loop, where the press stops it and keeps the record`)
  check('01 primary reads Save', (await popover.getByRole('button', { name: SAVE }).count()) === 1,
    `"${SAVE}" not found in the same frame`)
  await page.close()
}

// 2 -- stopped loop: the label names the removal, and the press performs it.
{
  const { page, deletes } = await load(makeLoop({ active: false, stopped_reason: 'user_stop' }))
  const popover = await openPopover(page)
  await shoot(popover, '02-stopped-loop-clear-record.png')
  check('02 reads Clear record', (await popover.getByRole('button', { name: CLEAR }).count()) === 1,
    `"${CLEAR}" not found on a stopped loop`)
  check('02 does not read Stop loop', (await popover.getByRole('button', { name: STOP }).count()) === 0,
    `"${STOP}" still offered on a loop that is already stopped`)
  // The frame must be of a real stopped loop, so the label is proven conditional
  // rather than photographed on an empty popover.
  check('02 the loop rendered as stopped',
    (await popover.getByTestId('auto-nudge-loop-paused').count()) === 1,
    'the popover did not render a stopped loop, so the label proves nothing')
  check('02 primary offers the way back', (await popover.getByRole('button', { name: START }).count()) === 1,
    `"${START}" missing -- a stopped loop must still show how to resume`)

  // A label is not the fix. Press it and observe the request.
  await popover.getByRole('button', { name: CLEAR }).click()
  await page.waitForTimeout(900)
  check('02 the press issues the DELETE', deletes.length === 1,
    `the press sent ${deletes.length} DELETE(s) to /api/autonudge/${LOOP_ID}, want 1`)
  check('02 the popover closed on success', !(await popover.isVisible()),
    'the popover stayed open after a successful clear, so nothing says the record is gone')
  await page.close()
}

await browser.close()
srv.close()

console.log('--- assertions (each frame must render what the PR claims) ---')
for (const r of results) console.log(JSON.stringify(r))

if (!results.every(r => r.ok)) {
  console.error('FAIL: a frame did not render the clear affordance -- fix the fixture, do not commit the PNG')
  process.exit(1)
}
console.log('OK')
