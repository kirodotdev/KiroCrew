/**
 * Screenshot harness for the SessionLaneChanged picker option: the Hooks page
 * event picker offers SessionLaneChanged with a gloss beside it, and its saved
 * row carries the same gloss, so the wire value never has to explain itself.
 *
 * Two frames:
 *   hooks-event-picker-open.png   — the New Hook form with the event list OPEN,
 *                                   SessionLaneChanged carrying its gloss
 *   hooks-lane-row-gloss.png      — a saved SessionLaneChanged hook in the table,
 *                                   its badge followed by the gloss
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures — gateway-free. Same technique as capture-hooks-actions-overflow.mjs.
 * Labels are read from the CATALOGS, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * LD_LIBRARY_PATH is overridden for the browser: a mise-installed Node exports
 * its own bundled libstdc++ to children, which is older than the one
 * /lib64/libgallium demands, so Chromium dies on GLIBCXX_3.4.29 before it opens
 * a page. The system library satisfies both.
 *
 * Usage: node scripts/capture-hooks-lane-event.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/hooks-lane-event'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8')).pages.hooksPage
const auto = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8')).pages.hooksPage

const GLOSS = manual.matcher_lane_pill_gloss
const NEW_HOOK = auto.new_hook
const MARK = manual.awaiting_agent
if (!GLOSS) throw new Error('catalog key matcher_lane_pill_gloss missing — renamed?')
if (!NEW_HOOK) throw new Error('catalog key new_hook missing — renamed?')
if (!MARK) throw new Error('catalog key awaiting_agent missing — renamed?')

const now = Math.floor(Date.now() / 1000)
const HOOKS = [
  {
    id: 'hk-1', name: 'log prompts', event: 'UserPromptSubmit', matcher: '', matcher_mode: 'glob',
    command: 'echo prompt >> /tmp/log.txt', skills: [], timeout: 30, enabled: true,
    last_run: now - 3600, last_status: 'ok', run_count: 42,
  },
  {
    // matcher EMPTY and the switch OFF, because that is the only shape the shipped
    // form and store can produce: a dormant trigger hides the matcher field and saves
    // `''`, and a hook created against one is saved switched off. A fixture with a
    // matcher depicted a capability this change deliberately refuses.
    id: 'hk-2', name: 'announce lane move', event: 'SessionLaneChanged', matcher: '',
    matcher_mode: 'glob', command: '~/.kiro/hooks/announce-lane.sh', skills: [], timeout: 15,
    enabled: false, last_run: now - 120, last_status: 'ok', run_count: 7,
  },
]

const stub = page => stubDashboardApi(page, {
  extra: async (path, route) => {
    if (path === '/api/hooks') { await json(route, { hooks: HOOKS }); return true }
    return false
  },
})

async function main() {
  const { srv, base } = await serveDist()
  // See the header note: the browser must not inherit Node's bundled libstdc++.
  const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
  // 1440 wide, matching capture-hooks-eleven-triggers.mjs, because this harness now
  // carries that one's sticky-column overlap assertion and the two must measure the
  // same layout. At 1280 the hooks table does not fit at ALL -- measured 42px of
  // overlap with no glossed row present, i.e. before anything this PR adds -- so the
  // assertion would have failed on the base layout rather than on a regression.
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stub(page)

  await page.goto(base + '/hooks', { waitUntil: 'domcontentloaded' })
  await page.getByRole('table').waitFor({ timeout: 20000 })
  await page.getByText('announce lane move').first().waitFor()
  await page.waitForTimeout(400)

  // Frame 2 first: the saved row's gloss is on the page as loaded, and opening
  // the form pushes the table down.
  const pill = page.getByTestId('lane-pill-gloss')
  await pill.waitFor({ timeout: 5000 })
  // Parenthesised on every surface, so two bare words beside the pill cannot read as a
  // second label. The brackets are part of what this frame is evidence FOR.
  const expectedGloss = `(${GLOSS})`
  if ((await pill.innerText()).trim() !== expectedGloss) {
    throw new Error(`row gloss reads ${JSON.stringify(await pill.innerText())}, expected ${JSON.stringify(expectedGloss)}`)
  }
  // This is the ONLY harness whose fixture renders a glossed EVENT cell, and it was
  // the one without the sibling's overlap assertion -- so a gloss that widened the
  // auto-layout table could slide LAST RUN under the `sticky right-0` ACTIONS column
  // and still produce a frame that looked fine. Same check as
  // capture-hooks-eleven-triggers.mjs, on the row that can actually trigger it.
  const overlap = await page.evaluate(() => {
    const table = document.querySelector('table')
    const heads = [...table.querySelectorAll('thead th')].map(th => th.textContent.trim())
    const cells = [...table.querySelector('tbody tr').querySelectorAll('td')]
    const last = cells[heads.findIndex(h => /Last Run/i.test(h))]
    const actions = cells[heads.findIndex(h => /Actions/i.test(h))]
    return Math.round(last.getBoundingClientRect().right - actions.getBoundingClientRect().left)
  })
  if (overlap > 0) {
    throw new Error(`LAST RUN sits ${overlap}px under the sticky ACTIONS column on the glossed row`)
  }
  await page.screenshot({ path: `${OUT}/hooks-lane-row-gloss.png` })

  // Frame 1: open the form, then the event list. The gloss rides the LABEL, so
  // it is only visible with the list open — which is the point of the frame.
  await page.getByRole('button', { name: NEW_HOOK, exact: true }).click()
  const trigger = page.getByRole('combobox').first()
  await trigger.waitFor({ timeout: 5000 })
  await trigger.click()
  // The accessible name is the BARE wire value: the gloss beside it is
  // aria-hidden so an exact-name lookup keeps working. The gloss is asserted from
  // the option's own text instead, so a lost gloss, or a stray mark, fails the
  // capture rather than shipping a frame that contradicts the page.
  const option = page.getByRole('option', { name: 'SessionLaneChanged', exact: true })
  await option.waitFor({ timeout: 5000 })
  const optionText = await option.innerText()
  if (!optionText.includes(`(${GLOSS})`) || optionText.includes(MARK)) {
    throw new Error(`picker option reads ${JSON.stringify(optionText)}, expected the gloss and no ${JSON.stringify(MARK)} mark`)
  }
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/hooks-event-picker-open.png` })

  await browser.close()
  srv.close()
  console.log(`wrote 2 frames to ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
