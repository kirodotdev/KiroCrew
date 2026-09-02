/**
 * Screenshot harness + behavior check for the BOUNDED /-menu command fetch.
 *
 * `api.slashCommands` had no deadline and no AbortSignal, so a wedged
 * /api/slash-commands never settled and `isFetching` never cleared. On a slash
 * prefix that matched nothing yet, SlashCommandMenu rendered NOTHING while
 * useListKeyboardNav's document listener kept preventDefaulting Enter — so the
 * composer was deadlocked with no menu on screen to explain why. The fix binds
 * a 15s deadline inside api.slashCommands, NAMES the in-flight window with a
 * "Loading commands…" row instead of rendering nothing, and renders a settled
 * error as a failure instead of as a confident "No matching commands".
 *
 * This asserts as well as photographs, against the REAL built SPA
 * (website/dist): the timeout scenes let the route HANG so the production
 * withDeadline timer is what fires, and each scene exits non-zero unless the
 * expected copy rendered. Nothing in CI runs this file — the CI-enforced half
 * is SlashCommandMenu.timeout.test.tsx and apiSlashCommandsDeadline.test.ts.
 *
 * Usage: node scripts/capture-slash-command-fetch-timeout.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/slash-command-fetch-timeout'
const SLOT = 'chat-slash-timeout'
const PROJECT = '/home/user/workspace/notes'

// The production deadline is SLASH_COMMANDS_TIMEOUT_MS (15s); wait past it so
// the real timer is what produces the failure, not a simulated rejection.
const DEADLINE_WAIT_MS = 17_000

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Sprint wrap-up',
  running: false,
  last_message: 'Ready when you are.',
  messages: 2,
  agent: 'default',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
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
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'How do I compact this session?' },
    { role: 'assistant', ts: Date.now() / 1000 - 590, content: 'Type `/` in the composer to browse commands.' },
  ],
}

/** The shape GET /api/slash-commands returns when the gateway is healthy. */
const COMMANDS = [
  { name: '/compact', description: 'Compact the conversation' },
  { name: '/context', description: 'Show context usage' },
  { name: '/model', description: 'Switch the model' },
  { name: '/todos', description: 'Show the todo list' },
  { name: '/usage', description: 'Show token usage' },
]

/**
 * One fresh page per scene.
 *
 * `commands` is either the literal string 'hang' — the route is marked handled
 * and never fulfilled, so the request stays in flight until the production
 * deadline aborts it — or a payload to serve. `sendMode` seeds the composer's
 * send binding through the same localStorage key ChatSettings reads.
 */
async function scene(context, base, { commands, sendMode, typed }) {
  const extra = async (path, route) => {
    if (path === '/api/slash-commands') {
      if (commands === 'hang') return true
      await json(route, commands)
      return true
    }
    if (path.startsWith('/api/skills')) { await json(route, []); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(([slot, mode]) => {
    localStorage.setItem('mc-active-slot', slot)
    if (mode) localStorage.setItem('mc-chat-config', JSON.stringify({ sendOnEnter: mode }))
  }, [SLOT, sendMode || ''])
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const composer = page.locator('textarea').first()
  await composer.click()
  await composer.pressSequentially(typed, { delay: 15 })
  await page.waitForTimeout(400)
  return page
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const failures = []
  // Apostrophe matched loosely: the catalog ships a straight quote, but a
  // typographic one would still be the same string to a reader.
  const FAILED = /Couldn.t load commands/
  const LOADING = /Loading commands…/
  const FAILED_ENTER = /Couldn.t load commands — Enter sends the message/
  const FAILED_CTRL = /Couldn.t load commands — Ctrl\+Enter sends the message/

  /* ── Scene 1: healthy gateway → the menu lists the served commands ── */
  let page = await scene(context, base, { commands: COMMANDS, typed: '/' })
  await page.locator('[role="listbox"]').waitFor({ timeout: 10000 })
  if (await page.getByText('/compact', { exact: false }).count() < 1) {
    failures.push('scene 1: the served command list did not render')
  }
  if (await page.getByText(FAILED).count() !== 0) {
    failures.push('scene 1: failure copy shown on a healthy fetch')
  }
  await page.screenshot({ path: `${OUT}/1-menu-loaded.png` })
  console.log('wrote', `${OUT}/1-menu-loaded.png`)
  await page.close()

  /* ── Scene 2: fetch in flight, zero-match prefix → the wait is NAMED ──
     Enter is still held here (a half-typed command must not send), but the
     menu says so rather than rendering nothing. Before-state of scene 3. */
  page = await scene(context, base, { commands: 'hang', typed: '/xyz' })
  await page.locator('[role="listbox"]').getByText(LOADING).waitFor({ timeout: 10000 })
  if (await page.getByText(LOADING).count() !== 1) {
    failures.push('scene 2: the in-flight row did not render while the fetch hung')
  }
  if (await page.getByText(FAILED).count() !== 0) {
    failures.push('scene 2: failure copy shown before the deadline elapsed')
  }
  await page.screenshot({ path: `${OUT}/2-in-flight-loading-row.png` })
  console.log('wrote', `${OUT}/2-in-flight-loading-row.png`)
  await page.close()

  /* ── Scene 3: same scene past the deadline → the failure names itself ── */
  page = await scene(context, base, { commands: 'hang', typed: '/xyz' })
  await page.locator('[role="status"]').filter({ hasText: FAILED }).waitFor({ timeout: DEADLINE_WAIT_MS })
  if (await page.locator('[role="status"]').filter({ hasText: FAILED_ENTER }).count() !== 1) {
    failures.push('scene 3: timed-out copy missing or not in a role="status" live region')
  }
  if (await page.getByText(/No matching commands/).count() !== 0) {
    failures.push('scene 3: claimed "No matching commands" for a list that never loaded')
  }
  await page.screenshot({ path: `${OUT}/3-timed-out-enter-sends.png` })
  console.log('wrote', `${OUT}/3-timed-out-enter-sends.png`)
  await page.close()

  /* ── Scene 4: same, but Ctrl+Enter is the send binding ── */
  page = await scene(context, base, { commands: 'hang', sendMode: 'ctrl-enter', typed: '/xyz' })
  await page.locator('[role="status"]').filter({ hasText: FAILED }).waitFor({ timeout: DEADLINE_WAIT_MS })
  if (await page.locator('[role="status"]').filter({ hasText: FAILED_CTRL }).count() !== 1) {
    failures.push('scene 4: Ctrl+Enter variant of the timed-out copy missing')
  }
  if (await page.getByText(FAILED_ENTER).count() !== 0) {
    failures.push('scene 4: promised bare Enter under a ctrl-enter binding')
  }
  await page.screenshot({ path: `${OUT}/4-timed-out-ctrl-enter-sends.png` })
  console.log('wrote', `${OUT}/4-timed-out-ctrl-enter-sends.png`)
  await page.close()

  await browser.close()
  srv.close()

  if (failures.length) {
    for (const f of failures) console.error('FAIL:', f)
    process.exit(1)
  }
  console.log('PASS: the /-menu bounds its fetch and renders the timeout as a named failure')
}

main().catch(err => { console.error(err); process.exit(1) })
