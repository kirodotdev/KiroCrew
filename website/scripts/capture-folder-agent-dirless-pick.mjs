/**
 * Screenshot harness for the folder modal's EXPLICIT-pick dir-less orphan
 * state.
 *
 * UX Review blocks on an evidence gap: attachment-06 in the PR description
 * renders the retired `(not available)` wording, which HEAD's `en.manual.json`
 * no longer contains — last round changed `agent_not_available` to
 * `{{agent}} (needs a project directory)`. The description's uploaded images
 * cannot be edited in place, so the fix is a committed frame showing the
 * current render.
 *
 * This is the EXPLICIT-pick dir-less orphan, NOT the inherited one. The
 * inherited variant (`inherit_named_not_available`) already has frame 03 in
 * `temp-screenshots/10107-inherited-agent-flag/` — this script does not touch
 * that state or that folder.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is
 * needed. A single TOP-LEVEL folder (no `parent_id`, so it inherits nothing)
 * carries an explicit `default_agent: 'repo-dev'` and starts seeded under a
 * directory that scans and declares that agent. Opening it in EDIT mode via
 * the sidebar's own "Folder settings" menu item, then clearing its project
 * directory field reaches `orphanDirless`: `orphanIsRescopeOnly &&
 * !effectiveProjectDir`. With no parent, `effectiveProjectDir` has nowhere
 * else to come from, so clearing the folder's own field drives it to `''`
 * directly — no ancestor directory needs suppressing.
 *
 * Two frames:
 *   01 pick-with-directory — explicit pick against a directory that scanned
 *                       and declares it; trigger names the agent plainly, no
 *                       flag, Save enabled. Establishes the before-state so
 *                       frame 02's flag is provably caused by clearing the
 *                       directory, not present all along.
 *   02 pick-dirless-flagged — the SAME pick after the project directory is
 *                       CLEARED; trigger reads
 *                       "repo-dev (needs a project directory)", the notice is
 *                       visible and unclipped, Save disabled.
 *
 * The frames cannot lie. Frame 02 asserts, any of which failing exits
 * non-zero so the PNGs are not citable:
 *   - the trigger contains "needs a project directory"
 *   - the trigger does NOT contain "not available" — the retired string this
 *     frame exists to disprove, asserted absent explicitly
 *   - the trigger does NOT contain "Inherit" — this is the explicit-pick
 *     state; a scene that accidentally produced the inherited variant would
 *     otherwise pass under the wrong name
 *   - the trigger does NOT contain another state's cause text ("not in this
 *     project", "not installed", "can't verify")
 *   - Save is disabled, and the footer hint carries the dimmed `text-muted`
 *     token, NOT `text-muted-strong` — the enabled token would mean this
 *     frame was taken before the footer-dimming copy change landed
 *   - the notice is present and sits inside the modal's visible box (the
 *     `boundingBox()` clipping check the sibling harness uses)
 * Frame 01 asserts the trigger names the pick WITHOUT the dir-less flag, Save
 * enabled, and the enabled footer token `text-muted-strong` — proving the
 * flag in frame 02 is caused by the clear, not present all along.
 *
 * Usage: node scripts/capture-folder-agent-dirless-pick.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { MODAL, ORPHAN_NOTICE, SUBMIT, PROJECT_DIR, assertFooterToken } from './lib/folder-agent-orphan-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/10107-explicit-dirless-flag'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

// A single TOP-LEVEL folder (no parent_id — nothing to inherit from), with an
// explicit own pick seeded under a directory that scans and declares it.
const folders = [
  { id: 'f1', name: 'Payments', icon: '💳', order: 0, collapsed: false, default_agent: 'repo-dev', project_dir: '/repo/ok' },
]

const slots = [
  {
    key: 's1', title: 'Explicit dir-less pick', messages: 4, running: false,
    agent: 'kirocrew', created: '2026-07-20T01:00:00Z',
    last_ts: '2026-08-01T20:00:00Z', folder_id: 'f1',
  },
]

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

  // /repo/ok scans fine and declares repo-dev, matching the folder's seeded
  // project_dir. Registered AFTER stubDashboardApi so this wins for the scoped
  // call; the unscoped /api/agents keeps the fixture's global roster (which
  // deliberately lacks repo-dev — kirocrew/oncall only — so clearing the
  // directory turns a valid explicit pick into an orphan against the global
  // fallback).
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
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'project roster scan refused: audit write failed' }),
    })
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Open the folder's own settings — not "New folder" — via the sidebar's
  // "Folder settings" menu item, the real click path a user takes to edit an
  // existing folder.
  await page.click('[data-testid="folder-menu-f1"]')
  await page.click('[data-testid="folder-settings-f1"]')
  await page.waitForSelector(MODAL, { timeout: 8000 })

  const agent = page.getByRole('combobox', { name: 'Default agent' })
  await agent.waitFor({ state: 'visible', timeout: 8000 })
  await page.waitForTimeout(700) // debounce + scan settle on the seeded /repo/ok

  // ── 01: explicit pick named, no flag, directory in effect ──
  const named = await agent.textContent()
  if (!named || !named.includes('repo-dev')) {
    throw new Error(`expected the trigger to name the explicit pick, got: ${named}`)
  }
  if (named.includes('Inherit')) {
    throw new Error(`the trigger reads as an inherited pick, not an explicit one: ${named}`)
  }
  if (named.includes('needs a project directory')) {
    throw new Error(`the trigger is already flagged dir-less before any clear: ${named}`)
  }
  if (await page.isDisabled(SUBMIT)) {
    throw new Error('Save is disabled on a valid explicit pick — the frame would document the wrong state')
  }
  await assertFooterToken(page, '01', false)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-01-pick-with-directory.png` })
  console.log('wrote', `${OUT}/${PREFIX}-01-pick-with-directory.png`)

  // ── 02: clear the folder's own project directory. This folder has no
  // parent_id, so there is no ancestor directory to fall back to —
  // effectiveProjectDir goes straight to '' and orphanDirless fires. ──
  await page.fill(PROJECT_DIR, '')
  await page.waitForSelector(ORPHAN_NOTICE, { timeout: 8000 })
  await page.waitForTimeout(400)

  const flagged = await agent.textContent()
  if (!flagged || !flagged.includes('needs a project directory')) {
    throw new Error(`expected the dir-less explicit-orphan label, got: ${flagged}`)
  }
  if (flagged.includes('not available')) {
    throw new Error(`the trigger uses the retired "not available" wording: ${flagged}`)
  }
  if (flagged.includes('Inherit')) {
    throw new Error(`the trigger reads as the inherited variant, not the explicit-pick one: ${flagged}`)
  }
  if (
    flagged.includes('not in this project')
    || flagged.includes('not installed')
    || flagged.includes("can't verify")
  ) {
    throw new Error(`the trigger carries another state's cause text: ${flagged}`)
  }
  if (!(await page.isDisabled(SUBMIT))) {
    throw new Error('Save is enabled on a dir-less explicit orphan — the frame would document the wrong state')
  }
  const notice = await page.textContent(ORPHAN_NOTICE)
  if (!notice) throw new Error('the dir-less orphan notice is empty')
  // The clipping watch: the notice is the only thing naming why Save is dead,
  // so it must sit INSIDE the modal's visible box, not below its scroll fold.
  const noticeBox = await page.locator(ORPHAN_NOTICE).boundingBox()
  const modalBox = await page.locator(MODAL).boundingBox()
  if (!noticeBox || !modalBox) throw new Error('could not measure the notice against the modal')
  if (noticeBox.y + noticeBox.height > modalBox.y + modalBox.height + 1) {
    throw new Error(
      `the notice is clipped at the modal fold: notice ends at ${noticeBox.y + noticeBox.height}, `
      + `modal ends at ${modalBox.y + modalBox.height}`,
    )
  }
  await assertFooterToken(page, '02', true)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-02-pick-dirless-flagged.png` })
  console.log('wrote', `${OUT}/${PREFIX}-02-pick-dirless-flagged.png`)

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
