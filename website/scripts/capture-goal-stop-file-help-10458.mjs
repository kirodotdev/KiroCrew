/**
 * Screenshot harness for the goal textarea's `{{STOP_FILE}}` help line (#10458).
 *
 * The default goal template ends with `To halt the loop, create {{STOP_FILE}}`.
 * The server substitutes that token with the loop's stop-sentinel path when each
 * nudge is SENT, so the stored template must keep it -- but the human reading
 * the textarea only ever saw the raw token, and could not tell how to stop the
 * loop. The fix is display-only: a help line under the field says what the
 * token becomes. That line is visible nowhere except the rendered panel, which
 * is why this exists rather than a DOM test alone.
 *
 * Three states, one frame each (dark and light):
 *
 *   - `after`  (default): nothing armed, the default template. Asserts the
 *     textarea still carries the token (the substitution contract), the generic
 *     help line is present and names the token, and it is wired to the textarea
 *     through aria-describedby.
 *   - `--armed-empty-sentinel`: an ACTIVE loop whose record carries an
 *     explicitly empty `stop_sentinel_path`. Asserts the empty-sentinel help
 *     line is the one shown (and the generic one is not).
 *   - `--expect-missing`: the pre-change state, token present and NO help line.
 *     This is how the "before" frame of a pair is taken honestly.
 *
 * It ASSERTS as well as photographs, against the REAL built SPA in
 * website/dist, so a regression is a non-zero exit, not a photo nobody compares.
 *
 * Nothing in CI runs this file; it is a manual guard and the source of the PR's
 * evidence. The CI-enforced half is src/test/AutoNudgePopover.test.tsx.
 *
 * Gateway-free: serveDist + stubDashboardApi, so it never dials a live
 * instance and needs no token.
 *
 * Usage: node scripts/capture-goal-stop-file-help-10458.mjs [outDir] [--expect-missing | --armed-empty-sentinel]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { goalPopoverSlots, goalPopoverDetail } from './lib/goal-popover-fixture.mjs'

const FLAGS = new Set(['--expect-missing', '--armed-empty-sentinel'])
const args = process.argv.slice(2).filter(a => !FLAGS.has(a))
const MODE = process.argv.includes('--expect-missing')
  ? 'before'
  : process.argv.includes('--armed-empty-sentinel') ? 'armed-empty-sentinel' : 'after'
const OUT = args[0] || '../temp-screenshots/goal-stop-file-help-10458'
const SLOT = 'chat-goal'
const TOKEN = '{{STOP_FILE}}'
const GENERIC_HELP = 'is filled in when each nudge is sent'
const EMPTY_HELP = 'armed without a stop file'

mkdirSync(OUT, { recursive: true })

const slots = goalPopoverSlots(SLOT)
const detail = goalPopoverDetail()

/** The loop record the slot route answers with in the armed-empty-sentinel
 *  mode: the REST shape (`asdict(loop)`), which is the one that carries
 *  `stop_sentinel_path` at all. */
const armedEmptySentinelLoop = {
  id: 'loop-empty-sentinel',
  slot_key: SLOT,
  message: `Keep the flaky-test backlog shrinking. Post a blocker ONCE if stuck. To halt the loop, create ${TOKEN}`,
  idle_secs: 300,
  max_cycles: 12,
  max_runtime_secs: 0,
  cycle_count: 2,
  active: true,
  last_fire_ts: Date.now() / 1000 - 120,
  next_due_ts: Date.now() / 1000 + 180,
  stopped_reason: '',
  banner: '',
  stop_sentinel_path: '',
}

/** Per-mode stubs for the automation routes. Each branch awaits `json()` and
 *  returns true, because the shared stub treats a falsy return as "not handled"
 *  and fulfils the route itself -- returning `json(...)` alone double-fulfils. */
async function extra(path, route) {
  if (path === `/api/autonudge/slot/${SLOT}`) {
    await json(route, { enabled: true, loop: MODE === 'armed-empty-sentinel' ? armedEmptySentinelLoop : null })
    return true
  }
  if (path === '/api/autonudge') {
    await json(route, { enabled: true, loops: MODE === 'armed-empty-sentinel' ? [armedEmptySentinelLoop] : [] })
    return true
  }
  if (path === `/api/monitors/slot/${SLOT}`) {
    await json(route, { enabled: true, monitor: null })
    return true
  }
  if (path === '/api/monitors') {
    await json(route, { enabled: true, monitors: [] })
    return true
  }
  if (path.startsWith('/api/chat/slots/')) {
    await json(route, detail)
    return true
  }
  return false
}

/** What the composer trigger is called in each mode: the goal chip renames
 *  itself while a loop is active. */
function triggerName() {
  return MODE === 'armed-empty-sentinel' ? /Goal active/ : 'Set a goal'
}

/** Read the help-line facts off the open popover. */
async function inspect(page) {
  const goalField = page.getByRole('textbox', { name: 'Goal description' }).first()
  const tokenInTextarea = await goalField.inputValue().then(v => v.includes(TOKEN)).catch(() => false)
  const describedBy = await goalField.getAttribute('aria-describedby').catch(() => null)
  const lineFacts = async (needle) => {
    const line = page.getByText(needle, { exact: false }).first()
    const shown = await line.count().then(n => n > 0).catch(() => false)
    if (!shown) return { shown: false, namesToken: false, wired: false }
    const text = (await line.textContent().catch(() => '')) || ''
    const id = await line.getAttribute('id').catch(() => null)
    return { shown: true, namesToken: text.includes(TOKEN), wired: !!describedBy && !!id && describedBy === id }
  }
  return { tokenInTextarea, generic: await lineFacts(GENERIC_HELP), empty: await lineFacts(EMPTY_HELP) }
}

/** The per-mode verdict over those facts. */
function verdict(f) {
  switch (MODE) {
    case 'before':
      return f.tokenInTextarea && !f.generic.shown && !f.empty.shown
    case 'armed-empty-sentinel':
      return f.tokenInTextarea && f.empty.shown && f.empty.namesToken && f.empty.wired && !f.generic.shown
    default:
      return f.tokenInTextarea && f.generic.shown && f.generic.namesToken && f.generic.wired && !f.empty.shown
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The panel is 11-12px type; 1x renders it soft on GitHub.
    deviceScaleFactor: 2,
    /* Assertions address controls by their ENGLISH accessible name, so the
       render has to be English. Two pins: this fixes what the browser reports,
       `mc-lang` below fixes what the app resolves from it. */
    locale: 'en-US',
  })

  const results = []

  async function shoot(theme) {
    const name = `${MODE}-${theme}`
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { slots, theme, extra, localStorageEntries: { 'mc-lang': 'en' } })
    await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    const trigger = page.getByRole('button', { name: triggerName() }).first()
    if (!(await trigger.count().then(n => n > 0).catch(() => false))) {
      results.push({ name, ok: false, why: `composer trigger ${String(triggerName())} not found` })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      await page.close()
      return
    }
    await trigger.click()
    await page.waitForTimeout(700)

    const box = await page.locator('[data-side]').first().boundingBox().catch(() => null)
    if (!box) {
      results.push({ name, ok: false, why: 'popover panel did not render' })
      await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      await page.close()
      return
    }

    const facts = await inspect(page)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: Math.max(0, box.x - 12), y: Math.max(0, box.y - 12), width: box.width + 24, height: box.height + 24 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
    results.push({ name, ok: verdict(facts), ...facts })
    await page.close()
  }

  await shoot('dark')
  await shoot('light')

  await browser.close()
  srv.close()

  const want = {
    before: 'the raw token with NO help line (pre-change)',
    'armed-empty-sentinel': 'the empty-sentinel help line only, naming the token, wired via aria-describedby',
    after: 'the raw token kept in the textarea, plus the generic help line naming it, wired via aria-describedby',
  }[MODE]
  console.log(`--- assertions (the goal editor must show ${want}) ---`)
  for (const r of results) console.log(JSON.stringify(r))

  if (!results.every(r => r.ok)) {
    console.error('FAIL: the goal editor did not match the expected state')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
