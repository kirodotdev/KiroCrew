/**
 * Screenshot harness for the folder modal's project-agent SCAN-ERROR state.
 *
 * UX Review blocks on an evidence gap rather than on a defect: the shipped
 * "(can't verify)" trigger appears in no attachment, and the older frames show a
 * trigger reading "Inherit (default)" — the pre-fix build, where a failed scan
 * dropped the pick from the options and left Save dead with nothing naming the
 * cause. This captures the state as it now ships.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 * One directory answers with a roster, a second fails, and the re-scope between
 * them is what produces a real render of the error state rather than a mock.
 *
 * Two frames:
 *   01 picked    — a project agent selected against a directory that scanned
 *   02 scan-error — the same pick after re-scoping to a directory whose scan
 *                  fails: trigger reads "repo-dev (can't verify)", Save is
 *                  disabled, and the error row + Retry is IN VIEW
 *
 * The frames cannot lie. Frame 2 asserts three things, any of which failing exits
 * non-zero so the PNGs are not citable:
 *   - the trigger names the pick and does NOT read "Inherit"  (the UX finding)
 *   - Save is disabled                                        (the dead-Save claim)
 *   - the error row sits inside the modal's visible box       (the clipping Watch)
 *
 * Usage: node scripts/capture-folder-agent-roster-scan-error.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-agent-scan-error'
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
const ERROR_ROW = '[data-testid="folder-config-agent-roster-error"]'
const ORPHAN_NOTICE = '[data-testid="folder-config-agent-notice"]'
const SUBMIT = '[data-testid="folder-config-submit"]'
const PROJECT_DIR = '[data-testid="folder-config-project-dir"]'
// The picker has no testid on purpose (the tests drive the shipped Radix
// combobox by ROLE); address it the same way so this harness cannot pass
// against a stub the users never see.

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

  // One directory scans, the other fails. Registered AFTER stubDashboardApi so
  // this wins for the scoped call; the unscoped /api/agents keeps the fixture's
  // global roster. 503 is what the handler now returns when the audit write
  // fails, so the failing directory models the real refusal rather than a
  // generic network error.
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
    if (url.includes('%2Frepo%2Fother') || url.includes('/repo/other')) {
      // Scans fine, but does NOT declare repo-dev: that is what makes the pick an
      // orphan whose absence the roster can actually assert.
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          agents: [
            { name: 'other-dev', source: 'project', scope: 'project' },
            { name: 'kirocrew', source: 'builtin' },
          ],
          default_agent: '',
        }),
      })
      return
    }
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'project roster scan refused: audit write failed' }),
    })
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  await page.click('[aria-label="More create options"]')
  await page.click('text=New folder')
  await page.fill('[data-testid="folder-config-name"]', 'Payments rewrite')
  await page.fill(PROJECT_DIR, '/repo/ok')

  // ── 01: a project agent picked against a directory that scanned ──
  const agent = page.getByRole('combobox', { name: 'Default agent' })
  await agent.waitFor({ state: 'visible', timeout: 8000 })
  await agent.click()
  await page.getByRole('option', { name: 'repo-dev' }).click()
  await page.waitForTimeout(400)
  const picked = await agent.textContent()
  if (!picked || !picked.includes('repo-dev')) {
    throw new Error(`expected the trigger to name the pick, got: ${picked}`)
  }
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-01-agent-picked.png` })
  console.log('wrote', `${OUT}/${PREFIX}-01-agent-picked.png`)

  // ── 02: re-scope to a directory whose scan fails ──
  await page.fill(PROJECT_DIR, '/repo/err')
  await page.waitForSelector(ERROR_ROW, { timeout: 8000 })
  await page.waitForTimeout(600) // let the scroll-into-view and spring settle

  const trigger = await agent.textContent()
  if (!trigger || !trigger.includes('repo-dev')) {
    throw new Error(`the trigger dropped the pick on a scan error, got: ${trigger}`)
  }
  if (trigger.includes('Inherit')) {
    throw new Error(`the trigger claims "Inherit" for a picked agent: ${trigger}`)
  }
  if (!(await page.isDisabled(SUBMIT))) {
    throw new Error('Save is enabled on an unverifiable pick — the frame would document the wrong state')
  }
  // The clipping Watch: the error row is the only thing naming why Save is dead,
  // so it must sit INSIDE the modal's visible box, not below its scroll fold.
  const rowBox = await page.locator(ERROR_ROW).boundingBox()
  const modalBox = await page.locator(MODAL).boundingBox()
  if (!rowBox || !modalBox) throw new Error('could not measure the error row against the modal')
  if (rowBox.y + rowBox.height > modalBox.y + modalBox.height + 1) {
    throw new Error(
      `the error row is clipped at the modal fold: row ends at ${rowBox.y + rowBox.height}, `
      + `modal ends at ${modalBox.y + modalBox.height}`,
    )
  }
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-02-agent-roster-scan-error.png` })
  console.log('wrote', `${OUT}/${PREFIX}-02-agent-roster-scan-error.png`)

  // ── 03: rescope orphan, Save blocked ──
  // The second stale attachment: it showed "Enter to submit" beside a dead Save
  // and the muted default hint where the warning notice belongs. Re-scoping to a
  // directory that SCANS but lacks the pick is the state, and it is distinct from
  // frame 2 — here the roster is known, so the label can say the agent is absent
  // rather than unverified, and the notice must AGREE with that label.
  await page.fill(PROJECT_DIR, '/repo/other')
  await page.waitForSelector(ORPHAN_NOTICE, { timeout: 8000 })
  await page.waitForTimeout(400)
  const orphanTrigger = await agent.textContent()
  if (!orphanTrigger || !orphanTrigger.includes('not in this project')) {
    throw new Error(`expected the rescope-orphan label, got: ${orphanTrigger}`)
  }
  const notice = await page.textContent(ORPHAN_NOTICE)
  if (!notice || /isn.t installed/i.test(notice)) {
    throw new Error(`the notice contradicts the label by claiming the agent is not installed: ${notice}`)
  }
  if (!(await page.isDisabled(SUBMIT))) {
    throw new Error('Save is enabled on a rescope orphan — the frame would document the wrong state')
  }
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-03-agent-rescope-orphan.png` })
  console.log('wrote', `${OUT}/${PREFIX}-03-agent-rescope-orphan.png`)

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
