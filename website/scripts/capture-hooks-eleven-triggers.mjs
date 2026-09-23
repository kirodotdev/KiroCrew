/**
 * Capture the Hooks tab's trigger surfaces: the event picker open on all eleven
 * authorable triggers, and a populated table showing a row per trigger group.
 *
 * Gateway-free, like every other harness in this folder — it serves the real
 * built SPA and answers `/api/**` from `stub-dashboard-api.mjs`, plus the hooks
 * endpoint below. That matters here: a real home would have to be hand-seeded
 * with one hook per trigger before every run, and six of the eleven triggers
 * have no lifecycle moment the gateway could fire to create one.
 *
 *   npm run build && node scripts/capture-hooks-eleven-triggers.mjs <outDir>
 */
import { chromium } from '@playwright/test'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2]
if (!OUT) { console.error('usage: capture-hooks-eleven-triggers.mjs <outDir>'); process.exit(2) }

// `enabled` follows the STORE's own rule rather than a convenient default: a hook
// on one of the six is saved switched off, so seeding all eleven as enabled made
// every shot contradict the marks beside them -- the ENABLED stat said 11 while
// six rows said nothing fires them. A capture that disagrees with the product is
// the same phantom as a wrong caption.
const KAS_ONLY = new Set([
  'PreTaskExecution', 'PostTaskExecution',
  'FileCreated', 'FileEdited', 'FileDeleted', 'UserTriggered',
])
const hook = (id, name, event, command, matcher = '') => ({
  id, name, event, matcher, matcher_mode: 'glob', command, skills: [],
  timeout: 30, enabled: !KAS_ONLY.has(event),
  last_run: 0, last_status: '', last_error: '', run_count: 0,
})

// One row per trigger the picker offers, so the table shows every badge the
// page can render rather than only the five that existed before.
const HOOKS = {
  hooks: [
    hook('h1', 'seed context', 'AgentSpawn', 'cat ~/notes.md'),
    hook('h2', 'lint the prompt', 'UserPromptSubmit', 'prompt-lint'),
    hook('h3', 'gate writes', 'PreToolUse', 'write-gate', 'fs_write'),
    hook('h4', 'log writes', 'PostToolUse', 'write-log', 'fs_write'),
    hook('h5', 'close the turn', 'Stop', 'turn-report'),
    hook('h6', 'stage the task', 'PreTaskExecution', 'task-stage'),
    hook('h7', 'file the task', 'PostTaskExecution', 'task-file'),
    hook('h8', 'format new files', 'FileCreated', 'formatter --new'),
    hook('h9', 'format saved files', 'FileEdited', 'formatter --changed'),
    hook('h10', 'drop the artefact', 'FileDeleted', 'artefact-prune'),
    hook('h11', 'run by hand', 'UserTriggered', 'audit-now'),
  ],
}

// A dormant hook someone has Tested: the result appears BESIDE the mark, which is
// the half of the marking rule a row with no runs cannot show.
HOOKS.hooks[8] = { ...HOOKS.hooks[8], last_run: Date.now() - 60_000, last_status: 'ok', run_count: 1 }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
try {
  for (const scheme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 1100 }, colorScheme: scheme, locale: 'en-US',
    })
    const page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: scheme,
      extra: async (path, route) => {
        if (path === '/api/hooks') { await json(route, HOOKS); return true }
        if (path === '/api/kiro-hooks') { await json(route, { hooks: {} }); return true }
        return false
      },
    })
    await page.goto(`${base}/capabilities?tab=hooks`, { waitUntil: 'networkidle' })
    await page.getByRole('button', { name: '+ New Hook' }).waitFor({ timeout: 20000 })
    // The dormant marks live in the Status column, so wait for one to render rather
    // than for a fixed delay: a shot taken before the query resolves proves nothing.
    await page.getByText('never fires', { exact: true }).first().waitFor({ timeout: 20000 })
    await page.waitForTimeout(400)
    // The Tested dormant row (`format saved files`) must show BOTH its mark and its
    // result. This is the claim the alt text makes; an unprovable shot fails here.
    const tested = page.getByRole('row', { name: /format saved files/ })
    for (const copy of ['never fires', 'OK']) {
      await tested.getByText(copy, { exact: true }).waitFor({ timeout: 20000 })
    }
    // The six long event names widen EVENT and STATUS, and this table is AUTO
    // layout inside an `overflow-x-auto` scroller whose ACTIONS column is
    // `sticky right-0` — so a wider table does not scroll into view here, it
    // slides LAST RUN UNDER the sticky column. Measured: 932px of table in a
    // 914px scroller left "1m ag…" unreadable. Assert the gap is gone.
    const overlap = await page.evaluate(() => {
      const table = document.querySelector('table')
      const heads = [...table.querySelectorAll('thead th')].map(th => th.textContent.trim())
      const cells = [...table.querySelector('tbody tr').querySelectorAll('td')]
      const last = cells[heads.findIndex(h => /Last Run/i.test(h))]
      const actions = cells[heads.findIndex(h => /Actions/i.test(h))]
      return Math.round(last.getBoundingClientRect().right - actions.getBoundingClientRect().left)
    })
    if (overlap > 0) {
      throw new Error(`LAST RUN sits ${overlap}px under the sticky ACTIONS column`)
    }
    await page.screenshot({ path: `${OUT}/hooks-table-${scheme}.png` })

    // The list panel's own "?" carries the decode for the marks in Status, so it is
    // evidence only when open. Indexing by position was wrong and produced a shot
    // whose alt text claimed the mark decode while the pixels showed the PROVIDER
    // card's tooltip: there are three InfoTips on this page with no form open.
    // Scope to the Hooks card's own heading, then assert the decode is on screen —
    // that assertion, not the locator, is what makes the shot evidence.
    await page
      // The InfoTip button sits INSIDE the h3, so it joins the heading's accessible
      // name ("Hooks More information") — an exact match never resolves.
      .getByRole('heading', { name: /^Hooks\b/ })
      .first()
      .getByRole('button', { name: 'More information' })
      .click()
    await page
      .getByText(/is supported by the agent but nothing here fires it yet/)
      .first()
      .waitFor({ timeout: 20000 })
    await page.screenshot({ path: `${OUT}/hooks-table-help-${scheme}.png` })
    await page.keyboard.press('Escape')
    await page.waitForTimeout(200)

    await page.getByRole('button', { name: '+ New Hook' }).click()
    await page.waitForTimeout(400)

    // The card's own help text is what tells a reader the six do not fire, so it is
    // evidence only when it is OPEN — the `?` is a click-to-toggle button, and a
    // review reading a closed tooltip is reading nothing. Same rule as above: the
    // copy is asserted, so the shot cannot show a different bubble.
    await page.getByRole('button', { name: 'More information' }).first().click()
    await page.getByText(/Script hooks fire shell commands/).first().waitFor({ timeout: 20000 })
    await page.screenshot({ path: `${OUT}/hooks-card-help-${scheme}.png` })
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'More information' }).first().click()
    await page.waitForTimeout(200)

    await page.getByLabel('Event').click()
    const last = page.getByRole('option', { name: 'UserTriggered' })
    await last.waitFor({ timeout: 20000 })
    await page.waitForTimeout(400)
    // Two shots, because eleven options do not fit one frame and the marks are the
    // thing under review: the top holds the five unmarked events and the two
    // `not fired yet` task triggers, the bottom the four `never fires` ones.
    await page.screenshot({ path: `${OUT}/hooks-event-picker-${scheme}.png` })

    // End moves Radix's ACTIVE option to the last row, which scrolls the list.
    // scrollIntoViewIfNeeded did not move what the screenshot could see.
    await page.keyboard.press('End')
    await page.waitForTimeout(500)
    // Assert the claim the alt text makes, before the shot is taken: all four
    // `never fires` options inside the viewport. An unprovable capture must fail here
    // rather than reach a reviewer as a phantom claim.
    const view = page.viewportSize()
    for (const name of ['FileCreated', 'FileEdited', 'FileDeleted', 'UserTriggered']) {
      const box = await page.getByRole('option', { name }).boundingBox()
      if (!box || box.y < 0 || box.y + box.height > view.height) {
        throw new Error(`picker capture cannot show ${name}: box=${JSON.stringify(box)}`)
      }
    }
    await page.screenshot({ path: `${OUT}/hooks-event-picker-end-${scheme}.png` })
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)

    // The form with a dormant event selected: no matcher field, Timeout kept. The
    // diff's whole `!dormantMark(event)` branch was visible in no shot.
    await page.getByLabel('Event').click()
    await page.getByRole('option', { name: 'FileEdited' }).click()
    await page.waitForTimeout(400)
    if (await page.getByPlaceholder(/Matcher/).count()) {
      throw new Error('matcher field still present on a dormant trigger')
    }
    // Everything the form owes the reader here, asserted before the shot: what the
    // mark means (as TEXT, since a `title` reaches neither touch nor keyboard), why
    // the fields went, and that the hook will be stored switched off.
    for (const copy of [/never fires on its own/, /No matcher:/, /will be saved turned off/]) {
      await page.getByText(copy).first().waitFor({ timeout: 20000 })
    }
    await page.screenshot({ path: `${OUT}/hooks-form-dormant-${scheme}.png` })

    // The OTHER variant, which no shot covered: a task trigger the agent supports. Its
    // two lines differ from the four above -- "not fired yet" rather than "never fires",
    // and a "yet" the other group must never be given -- so a review that has only seen
    // the `never fires` form has not read this state at all.
    await page.getByLabel('Event').click()
    await page.getByRole('option', { name: 'PostTaskExecution' }).click()
    for (const copy of [/nothing here fires it yet/, /nothing fires this trigger yet/]) {
      await page.getByText(copy).first().waitFor({ timeout: 20000 })
    }
    await page.screenshot({ path: `${OUT}/hooks-form-waiting-${scheme}.png` })
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log('ok')
