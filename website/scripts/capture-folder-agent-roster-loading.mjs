/**
 * Screenshot harness for the folder modal's project-agent ROSTER STATES.
 *
 * UX Review blocks a diff-added user-visible state that no attachment shows, and
 * the in-flight state is the one that needs a frame: while the roster for a
 * newly-typed directory is being fetched, Save is disabled, and without a cue
 * the button simply goes dead. The hint under Default agent is what explains it,
 * matching the sibling Tags field's own loading hint in this same modal.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 * The roster route is held open deliberately so the in-flight frame is a real
 * render of that state rather than a mock of it.
 *
 * Two frames:
 *   01 loading   — "Loading agents…" under Default agent, Save disabled
 *   02 loaded    — the same modal once the roster arrives: hint gone, the
 *                  directory's own project agents offered
 *
 * The frames cannot lie: the script ASSERTS the loading hint is present and Save
 * is disabled in frame 1, and that the hint has cleared and Save has recovered in
 * frame 2. Either failing exits non-zero and the PNGs are not citable.
 *
 * Usage: node scripts/capture-folder-agent-roster-loading.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-agent-roster'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const folders = [
  { id: 'f1', name: 'Kiro', icon: '🚀', order: 0, collapsed: false },
]

const slots = [
  {
    key: 's1', title: 'Folder roster states', messages: 4, running: false,
    agent: 'kirocrew', created: '2026-07-20T01:00:00Z',
    last_ts: '2026-08-01T20:00:00Z', folder_id: 'f1',
  },
]

const MODAL = '[role="dialog"]'
const LOADING = '[data-testid="folder-config-agent-roster-loading"]'
const SUBMIT = '[data-testid="folder-config-submit"]'
const PROJECT_DIR = '[data-testid="folder-config-project-dir"]'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2, // 11-13px modal type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  await stubDashboardApi(page, { folders, slots })
  logPageProblems(page)

  // Hold the project-scoped roster open until released, so the in-flight state
  // is a real render. Registered AFTER stubDashboardApi so this wins for the
  // scoped call; the unscoped /api/agents keeps the fixture's global roster.
  let release = null
  const held = new Promise(resolve => { release = resolve })
  await page.route('**/api/agents?*project_path=*', async route => {
    await held
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      // The scoped roster is an OBJECT, not a bare array: a bare list leaves the
      // picker with only "None" and the frames would document nothing.
      body: JSON.stringify({
        agents: [
          { name: 'repo-dev', source: 'project', scope: 'project' },
          { name: 'kirocrew', source: 'builtin' },
        ],
        default_agent: '',
      }),
    })
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Open the create-folder modal and type a directory, which is what triggers
  // the debounced re-scope fetch the hint belongs to.
  await page.click('[aria-label="More create options"]')
  await page.click('text=New folder')
  await page.fill('[data-testid="folder-config-name"]', 'Payments rewrite')
  await page.fill(PROJECT_DIR, '/repo/payments')

  // ── 01: in flight ──
  await page.waitForSelector(LOADING, { timeout: 8000 })
  if (!(await page.isDisabled(SUBMIT))) {
    throw new Error('Save is enabled while the roster is in flight — the hint would be explaining nothing')
  }
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-01-agent-roster-loading.png` })
  console.log('wrote', `${OUT}/${PREFIX}-01-agent-roster-loading.png`)

  // ── 02: settled ──
  release()
  await page.waitForSelector(LOADING, { state: 'detached', timeout: 8000 })
  await page.waitForTimeout(400) // let the spring settle
  if (await page.isDisabled(SUBMIT)) {
    throw new Error('Save is still disabled after the roster settled — the hint would never clear')
  }
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-02-agent-roster-loaded.png` })
  console.log('wrote', `${OUT}/${PREFIX}-02-agent-roster-loaded.png`)

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
