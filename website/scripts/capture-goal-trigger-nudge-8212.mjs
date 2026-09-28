/**
 * Screenshot harness + assertions for the GOAL-LOOP MANUAL TRIGGER (#8212).
 *
 * The goal popover gains one button. Three frames, because a still of a button
 * proves the least interesting third of the change:
 *
 *   1. armed loop  -> "Trigger nudge" sits on the SCHEDULE line, beside the
 *                     countdown it acts on, and the Stop/Save action row below
 *                     is back to two controls (website/AUTOSDE.yaml:230 holds a
 *                     row to two and names "leaves the row" as the escape).
 *   2. PAUSED loop -> the button is ABSENT. The control frame: without it,
 *                     frame 1 proves only that a button can render, not that it
 *                     renders when it should. It is gated on `active` because
 *                     every terminal bound leaves the loop inactive and the
 *                     server refuses to fire one, so a button there could only
 *                     ever produce a 409.
 *   3. refusal     -> the fire route answers 409 `session_busy` and the popover
 *                     shows the reason inline and STAYS OPEN. This is the frame
 *                     that carries the answer to the issue's queue-vs-disable
 *                     question: a press during a live turn is refused visibly
 *                     rather than queued silently. No still of a button can show
 *                     that, which is why it is photographed rather than argued.
 *
 * This ASSERTS as well as photographs, because a PNG cannot fail. It drives the
 * REAL built SPA (website/dist) behind `serveDist` with every /api/** call
 * answered from fixtures by `stubDashboardApi` -- no gateway, no dashboard auth,
 * no kiro-cli -- and exits non-zero unless each frame renders what the PR
 * claims. A stale bundle therefore reds instead of quietly photographing the old
 * copy, and a blank frame cannot be committed as evidence.
 *
 * Labels are read from the CATALOG, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-goal-trigger-nudge-8212.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/goal-trigger-nudge-8212'
const SLOT = 'chat-loop'
const PROJECT = '/home/user/workspace/uploader'
const LOOP_ID = 'loop-8212'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const gen = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const TRIGGER = manual.components.autoNudgePopover.trigger_nudge
const SAVE = manual.components.autoNudgePopover.save
const STOP = gen.components.autoNudgePopover.stop_loop
if (!TRIGGER || !SAVE || !STOP) {
  throw new Error('components.autoNudgePopover trigger/save/stop keys missing -- renamed?')
}

const NOW = Math.floor(Date.now() / 1000)
/**
 * Fixed wall-clock instant for the loop's timestamps so the popover's "Last
 * fire" line renders the same string on every run. A now-relative value would
 * rewrite the committed PNG's bytes on every re-capture, turning a re-pin after
 * a rebase into a pointless binary diff.
 */
const FIXED_FIRE_TS = Date.UTC(2026, 8, 6, 9, 40, 0) / 1000

const slots = [{
  key: SLOT,
  title: 'Drive PR 8188 to green',
  running: false,
  last_message: 'Cycle 3 done -- waiting on the Windows shard.',
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
    { role: 'user', ts: NOW - 900, content: 'Keep checking PR 8188 until the board settles.' },
    { role: 'assistant', ts: NOW - 120, content: 'Cycle 3 done -- the Windows shard is still running.' },
  ],
}

/**
 * One loop, shaped like the backend's `asdict(loop)`.
 *
 * `next_due_ts` is 0 on purpose: a live countdown puts a per-second value in the
 * popover's own footer line, so two runs would never produce the same bytes.
 */
const makeLoop = over => ({
  id: LOOP_ID,
  slot_key: SLOT,
  message: 'Check https://github.com/kirodotdev/KiroCrew/pull/8188 for new CI results and review comments.',
  idle_secs: 300,
  max_cycles: 24,
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

/**
 * Boot the chat page with `loop` seeded as this slot's loop.
 *
 * `fireReply` is the answer the fire route gives. Null means the route is not
 * stubbed at all, which is the right default: a frame that does not press the
 * button must not depend on a response that would never be requested.
 */
async function load(loop, fireReply = null) {
  const page = await context.newPage()
  logPageProblems(page)

  const extra = async (path, route) => {
    if (fireReply && path === `/api/autonudge/${LOOP_ID}/fire`) {
      await json(route, fireReply.body, fireReply.status)
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
  return page
}

/** Open the goal popover from the composer chip and return it. */
async function openPopover(page) {
  // Addressed by accessible name, which differs by armed state: an active loop
  // reads "Goal active (cycle N/M)", an inactive one falls back to "Set a goal".
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

// 1 -- armed loop: the button is offered, beside Stop loop and Save.
{
  const page = await load(makeLoop())
  const popover = await openPopover(page)
  const trigger = popover.getByRole('button', { name: TRIGGER })
  const count = await trigger.count()
  await shoot(popover, '01-armed-loop-trigger-offered.png')
  check('01 button present', count === 1, `found ${count} "${TRIGGER}" button(s), want exactly 1`)
  // Its neighbours, so the frame is proven to show the whole popover foot rather
  // than a button floating on its own.
  check('01 row shows Stop loop', (await popover.getByRole('button', { name: STOP }).count()) === 1,
    `"${STOP}" not found in the same frame`)
  check('01 row shows Save', (await popover.getByRole('button', { name: SAVE }).count()) === 1,
    `"${SAVE}" not found in the same frame`)
  // The blocking rule is about SIBLINGS IN ONE horizontal group, so assert the
  // action row itself rather than the popover's total button count.
  const rowLabels = await popover.evaluate(el => {
    const save = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Save')
    return Array.from(save.parentElement.querySelectorAll('button')).map(b => b.textContent)
  })
  check('01 action row holds two', rowLabels.length === 2,
    `action row holds ${rowLabels.length} buttons (${rowLabels.join(', ')}), cap is 2`)
  await page.close()
}

// 2 -- paused loop: the control frame. The button must be absent, and the
// schedule line must SAY why rather than leaving an unexplained gap.
{
  const page = await load(makeLoop({ active: false, stopped_reason: 'manual' }))
  const popover = await openPopover(page)
  const count = await popover.getByRole('button', { name: TRIGGER }).count()
  await shoot(popover, '02-paused-loop-trigger-absent.png')
  check('02 button absent', count === 0, `found ${count} "${TRIGGER}" button(s) on a paused loop, want 0`)
  // Complement: absent anywhere in the popover, not merely under that exact
  // accessible name -- a stale render could leave it somewhere else.
  check('02 absent anywhere', !(await popover.innerText()).includes(TRIGGER),
    `popover text still contains "${TRIGGER}"`)
  // And the popover really did render the loop, so the negative is about the
  // gating rather than about an empty frame.
  check('02 popover shows the loop', (await popover.getByRole('button', { name: STOP }).count()) === 1,
    `"${STOP}" missing -- the frame did not render a loop at all, so the negative proves nothing`)
  // The absence must be EXPLAINED. Without this the frame is a mystery: a
  // reader cannot tell a paused loop from a broken render.
  check('02 the paused state is named',
    (await popover.getByTestId('auto-nudge-loop-paused').count()) === 1,
    'nothing on the schedule line says the loop is paused, so the missing button has no reason')
  await page.close()
}

// 3 -- refusal: a press during a live turn is shown, not swallowed.
{
  const REFUSAL = 'nudge not sent: the agent is still working, so try again when it finishes'
  const page = await load(makeLoop(), {
    status: 409,
    body: { error: REFUSAL, code: 'session_busy' },
  })
  const popover = await openPopover(page)
  // Type into the goal box first: a press must not be a silent way to lose an
  // unsaved edit, so the frame has to show the edit still there afterwards.
  await popover.getByLabel('Goal description').fill('edited but not saved')
  await popover.getByRole('button', { name: TRIGGER }).click()
  await page.waitForTimeout(900)
  await shoot(popover, '03-refused-while-a-turn-is-in-flight.png')
  const text = await popover.innerText()
  check('03 refusal is visible', text.includes(REFUSAL),
    `popover does not show the refusal; it reads ${JSON.stringify(text.slice(0, 200))}`)
  check('03 popover stayed open', await popover.isVisible(),
    'popover closed on a refusal -- it holds unsaved fields and must not be torn down')
  check('03 the unsaved edit survived',
    (await popover.getByLabel('Goal description').inputValue()) === 'edited but not saved',
    'the typed goal was lost -- a refusal must not discard the edit')
  await page.close()
}

// 4 -- the SUCCESS state, which the PR claims and nothing photographed. A press
// that works flips the schedule line to "due" in place, and the popover stays
// open so the change is visible where the press happened. Without this frame the
// feedback claim rests on prose; with it, a reviewer can see what a working press
// looks like. `next_due_ts` is a FIXED past timestamp, not `Date.now()`, so the
// line renders the settled "due" wording rather than a per-second countdown that
// would make every run produce different bytes.
{
  const DUE = 'Next cycle due'
  // The route returns the loop UNCHANGED -- it no longer moves the deadline -- so
  // this fixture must not hand the frame a moved one. If the line still reads
  // "due" below, the CLIENT produced that, which is the behaviour under test.
  const page = await load(makeLoop(), { status: 200, body: { ok: true, loop: makeLoop() } })
  const popover = await openPopover(page)
  // Same unsaved edit as frame 3: on SUCCESS the edit must survive too, which is
  // the whole reason this press does not close the popover.
  await popover.getByLabel('Goal description').fill('edited but not saved')
  const before = await popover.innerText()
  check('04 started from a non-due line', !before.includes(DUE),
    `the line already read "${DUE}" before the press, so the change proves nothing`)
  await popover.getByRole('button', { name: TRIGGER }).click()
  await page.waitForTimeout(900)
  await shoot(popover, '04-pressed-schedule-line-reads-due.png')
  const after = await popover.innerText()
  check('04 the line now reads due', after.includes(DUE),
    `after a successful press the line reads ${JSON.stringify(after.slice(0, 200))}`)
  check('04 popover stayed open', await popover.isVisible(),
    'the popover closed on success -- the feedback would be invisible and the edit lost')
  check('04 the unsaved edit survived',
    (await popover.getByLabel('Goal description').inputValue()) === 'edited but not saved',
    'a successful press discarded the typed goal')
  check('04 no error is shown', !after.includes('nudge not sent'),
    'a successful press rendered a refusal notice')
  // The press must ACKNOWLEDGE itself. Before this the button re-enabled unchanged
  // and a reader could not tell whether pressing again would double the nudge.
  check('04 the trigger is now disabled',
    await popover.getByRole('button', { name: TRIGGER }).isDisabled(),
    'the trigger is still pressable after a successful press, so nothing says the cycle is already armed')
  await page.close()
}

// 5 -- the 320px floor. MEASURED, not asserted from class names: the rule is
// about what renders, and a class-string check would pass on a layout that still
// overflows. Reads real bounding boxes and fails if the popover or the new action
// extends past the viewport's right edge.
{
  const NARROW = 320
  const page = await context.newPage()
  logPageProblems(page)
  await page.setViewportSize({ width: NARROW, height: 900 })
  const extra = async (path, route) => {
    if (path === `/api/autonudge/slot/${SLOT}`) { await json(route, { loop: makeLoop() }); return true }
    if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [makeLoop()] }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }
  await stubDashboardApi(page, { slots, extra })
  await page.goto(base)
  const popover = await openPopover(page)
  await shoot(popover, '05-narrow-320px-nothing-clipped.png')

  const box = await popover.boundingBox()
  check('05 popover fits the viewport',
    box !== null && box.x >= 0 && box.x + box.width <= NARROW + 1,
    `popover spans x=${box && box.x}..${box && (box.x + box.width)} in a ${NARROW}px viewport`)

  const btn = popover.getByRole('button', { name: TRIGGER })
  check('05 the trigger is present at 320px', (await btn.count()) === 1,
    'the trigger vanished at 320px -- wrapping must move it, not remove it')
  const bb = await btn.boundingBox()
  check('05 the trigger fits the viewport',
    bb !== null && bb.x >= 0 && bb.x + bb.width <= NARROW + 1,
    `trigger spans x=${bb && bb.x}..${bb && (bb.x + bb.width)} in a ${NARROW}px viewport`)
  // Clipping is not only horizontal overflow: a zero-width or zero-height box is
  // an element the user cannot press either.
  check('05 the trigger is actually pressable',
    bb !== null && bb.width > 8 && bb.height > 8,
    `trigger box is ${bb && bb.width}x${bb && bb.height}`)
  await page.close()
}

await browser.close()
srv.close()

console.log('--- assertions (each frame must render what the PR claims) ---')
for (const r of results) console.log(JSON.stringify(r))

if (!results.every(r => r.ok)) {
  console.error('FAIL: a frame did not render the trigger affordance -- fix the fixture, do not commit the PNG')
  process.exit(1)
}
console.log('OK')
