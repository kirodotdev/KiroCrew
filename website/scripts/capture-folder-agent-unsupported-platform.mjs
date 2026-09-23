/**
 * Screenshot harness for the folder modal's UNSUPPORTED-PLATFORM state.
 *
 * UX Review blocks on an evidence gap, not a defect: three strings render only
 * where the platform cannot pin a directory descriptor, and no attachment shows
 * any of them. This captures that state as it ships.
 *
 * The state is reached the way the product reaches it: the roster endpoint
 * answers 503 carrying `code: "scan_unsupported_platform"`, which the modal
 * keys on to render a row with NO Retry control — a retry could never succeed
 * where the capability is absent, so offering one would invite a pointless
 * loop. That missing Retry is the difference from the sibling scan-error
 * harness, whose 503 carries no code and DOES offer one.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 *
 * Three frames:
 *   01 roster-unsupported  — the roster row, "Folder-level project agents
 *                            aren't supported on this platform.", no Retry
 *   02 trigger-unsupported — an explicit pick flagged "repo-dev (not supported
 *                            on this platform)", Save disabled
 *   03 inherit-unsupported — a child INHERITING that agent, flagged
 *                            "Inherit (repo-dev — not supported on this
 *                            platform)", which is a distinct string from 02
 *
 * The frames cannot lie. Each asserts its own claims before writing, and any
 * failure exits non-zero so the PNGs are never citable:
 *   - the roster row carries the unsupported copy and NO Retry control
 *   - the trigger names the pick with the unsupported suffix, never "Inherit"
 *     on frame 02 and always "Inherit" on frame 03
 *   - Save is disabled, and the row sits inside the modal's visible box
 *
 * Usage: node scripts/capture-folder-agent-unsupported-platform.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/10107-unsupported-platform'
const PREFIX = process.argv[3] || 'unsupported'

mkdirSync(OUT, { recursive: true })

// f1 declares the agent and a directory; f2 inherits BOTH from it, which is what
// makes frame 03's "Inherit (…)" label reachable without a pick of its own.
const folders = [
  { id: 'f1', name: 'Payments', icon: '💳', order: 0, collapsed: false, default_agent: 'repo-dev', project_dir: '/repo/ok' },
  { id: 'f2', name: 'Backend', icon: '⚙️', order: 0, collapsed: false, parent_id: 'f1' },
]

const slots = [
  {
    key: 's1', title: 'Folder roster states', messages: 4, running: false,
    agent: 'kirocrew', created: '2026-07-20T01:00:00Z',
    last_ts: '2026-08-01T20:00:00Z', folder_id: 'f1',
  },
]

const MODAL = '[role="dialog"]'
const UNSUPPORTED_ROW = '[data-testid="folder-config-agent-roster-unsupported"]'
const ERROR_ROW = '[data-testid="folder-config-agent-roster-error"]'
const SUBMIT = '[data-testid="folder-config-submit"]'
const PROJECT_DIR = '[data-testid="folder-config-project-dir"]'

const UNSUPPORTED_COPY = "aren't supported on this platform"

/** Assert the row is present, carries the copy, offers no Retry, and is in view. */
async function assertUnsupportedRow(page, frame) {
  await page.waitForSelector(UNSUPPORTED_ROW, { timeout: 8000 })
  const copy = await page.textContent(UNSUPPORTED_ROW)
  if (!copy || !copy.includes(UNSUPPORTED_COPY)) {
    throw new Error(`${frame}: the roster row lacks the unsupported copy, got: ${copy}`)
  }
  // A Retry here would never succeed, so its ABSENCE is the claim under test.
  if (/retry/i.test(copy)) {
    throw new Error(`${frame}: the unsupported row offers a Retry, which cannot succeed: ${copy}`)
  }
  if (await page.locator(ERROR_ROW).count()) {
    throw new Error(`${frame}: the retryable scan-error row rendered instead of the unsupported row`)
  }
  const rowBox = await page.locator(UNSUPPORTED_ROW).boundingBox()
  const modalBox = await page.locator(MODAL).boundingBox()
  if (!rowBox || !modalBox) throw new Error(`${frame}: could not measure the row against the modal`)
  if (rowBox.y + rowBox.height > modalBox.y + modalBox.height + 1) {
    throw new Error(
      `${frame}: the row is clipped at the modal fold: row ends at ${rowBox.y + rowBox.height}, `
      + `modal ends at ${modalBox.y + modalBox.height}`,
    )
  }
}

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

  // `/repo/ok` scans so an agent can be PICKED first; every other directory
  // answers the capability refusal. Registered AFTER stubDashboardApi so this
  // wins for the scoped call while the unscoped /api/agents keeps the global
  // roster the modal unions back in.
  await page.route('**/api/agents?*project_path=*', async route => {
    const url = route.request().url()
    if (url.includes('%2Frepo%2Fok') || url.includes('/repo/ok')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          agents: [
            { name: 'repo-dev', source: 'project', scope: 'project' },
            { name: 'kirocrew', source: 'builtin' },
          ],
          default_agent: '',
        }),
      })
      return
    }
    // The real refusal shape: the code is what distinguishes "this platform
    // cannot answer" from "this scan failed and may succeed on retry".
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        error: 'folder-level project-agent selection is not supported on this platform',
        code: 'scan_unsupported_platform',
      }),
    })
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // ── 01 + 02: an explicit pick, then re-scoped onto the unsupported platform ──
  await page.click('[aria-label="More create options"]')
  await page.click('text=New folder')
  await page.fill('[data-testid="folder-config-name"]', 'Payments rewrite')
  await page.fill(PROJECT_DIR, '/repo/ok')

  const agent = page.getByRole('combobox', { name: 'Default agent' })
  await agent.waitFor({ state: 'visible', timeout: 8000 })
  await agent.click()
  await page.getByRole('option', { name: 'repo-dev' }).click()
  await page.waitForTimeout(400)

  await page.fill(PROJECT_DIR, '/repo/unsupported')
  await assertUnsupportedRow(page, 'frame 01')
  await page.waitForTimeout(600) // let scroll-into-view and the spring settle

  const trigger = await agent.textContent()
  if (!trigger || !trigger.includes('not supported on this platform')) {
    throw new Error(`frame 01: expected the unsupported trigger label, got: ${trigger}`)
  }
  if (!trigger.includes('repo-dev')) {
    throw new Error(`frame 01: the trigger dropped the pick, got: ${trigger}`)
  }
  if (trigger.includes('Inherit')) {
    throw new Error(`frame 01: the trigger claims "Inherit" for an explicit pick: ${trigger}`)
  }
  if (!(await page.isDisabled(SUBMIT))) {
    throw new Error('frame 01: Save is enabled on an unverifiable pick — the frame would document the wrong state')
  }

  // ONE frame carries both the roster row and the explicit trigger: they render
  // together, so a second screenshot of the same state would be redundant rather
  // than additional evidence.
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-01-roster-row-and-trigger.png` })
  console.log('wrote', `${OUT}/${PREFIX}-01-roster-row-and-trigger.png`)

  // ── 02: the INHERITING child — a different string from frame 01 ──
  // Close the create modal first; this frame edits an EXISTING folder. Cancel,
  // not Escape: the overlay keeps intercepting pointer events after a bare
  // Escape, so the sidebar click below would never land.
  await page.getByRole('button', { name: 'Cancel' }).click()
  await page.waitForSelector(MODAL, { state: 'detached', timeout: 8000 })

  // f2 declares no agent of its own, so its label is the inherit form. Opened
  // through the sidebar's own "Folder settings" item, the real click path for
  // editing an existing folder. Seed its dir to the directory that HAS repo-dev
  // so the inherited pick is valid first, then re-scope onto the unsupported
  // platform — which is what flags a name that cannot be confirmed either way.
  await page.click('[data-testid="folder-menu-f2"]')
  await page.click('[data-testid="folder-settings-f2"]')
  await page.waitForSelector(MODAL, { timeout: 8000 })

  const child = page.getByRole('combobox', { name: 'Default agent' })
  await child.waitFor({ state: 'visible', timeout: 8000 })
  await page.fill(PROJECT_DIR, '/repo/ok')
  await page.waitForTimeout(700) // debounce + scan settle

  const named = await child.textContent()
  if (!named || !named.includes('Inherit') || !named.includes('repo-dev')) {
    throw new Error(`frame 02 setup: expected the inherited agent named first, got: ${named}`)
  }

  await page.fill(PROJECT_DIR, '/repo/unsupported')
  await page.waitForTimeout(700)

  await assertUnsupportedRow(page, 'frame 02')
  const inherited = await child.textContent()
  if (!inherited || !inherited.includes('Inherit')) {
    throw new Error(`frame 02: expected the INHERIT form of the label, got: ${inherited}`)
  }
  if (!inherited.includes('not supported on this platform')) {
    throw new Error(`frame 02: the inherited label lacks the unsupported suffix, got: ${inherited}`)
  }
  if (!inherited.includes('repo-dev')) {
    throw new Error(`frame 02: the inherited label dropped the agent name, got: ${inherited}`)
  }
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-02-inherit-unsupported.png` })
  console.log('wrote', `${OUT}/${PREFIX}-02-inherit-unsupported.png`)

  await browser.close()
  srv.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
