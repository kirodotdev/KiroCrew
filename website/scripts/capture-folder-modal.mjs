/**
 * Screenshot harness for the folder create/settings modal (FolderConfigModal).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 * Captures the four states that carry the design decisions:
 *   01 create, top level        → breadcrumb shows just "Top level"
 *   02 create, nested subfolder → breadcrumb restates the fixed destination,
 *                                 project dir shows the INHERITED path as a
 *                                 placeholder (empty value = still inherited)
 *   03 create, emoji panel open → the inline picker grid + custom-emoji field
 *   04 edit (Folder settings)   → every field prefilled from the folder
 *   05 edit, orphan agent       → the "(not installed)" label plus the inline
 *                                 aria-describedby notice under the field
 *
 * Usage: node scripts/capture-folder-modal.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-modal'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

// f1 carries a project_dir so the nested-create shot can show inheritance;
// f1a deliberately has none of its own.
const folders = [
  { id: 'f1', name: 'Kiro', icon: '🚀', order: 0, collapsed: false, project_dir: '/Volumes/workplace/KiroCrew' },
  { id: 'f1a', name: 'Backend', icon: '🧩', order: 0, collapsed: false, parent_id: 'f1' },
  // f2's default agent IS in the stubbed /api/agents roster ('kirocrew'), so its
  // Folder settings render the INSTALLED-agent else-branch: the plain helper hint
  // and no orphan notice. This is the counterpart to f3's orphan state below, and
  // the frame that demonstrates the notice is shown only when the agent is missing.
  { id: 'f2', name: 'Payments', icon: '🎯', order: 1, collapsed: true, project_dir: '/repo/payments', default_agent: 'kirocrew' },
  // f3's default agent is not in the roster (see the /api/agents override in
  // main()), so its Folder settings render the orphan state: the
  // "(not installed)" option label AND the inline notice bound to the field.
  { id: 'f3', name: 'Retired', icon: '📦', order: 2, collapsed: true, project_dir: '/repo/retired', default_agent: 'retired-agent' },
]

const slot = (key, title, folder_id, last_ts) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew',
  created: '2026-07-20T01:00:00Z', last_ts, folder_id,
})

const slots = [
  slot('s1', 'Folder config modal', 'f1', '2026-08-01T20:00:00Z'),
  slot('s2', 'Create-time settings', 'f1a', '2026-08-01T19:00:00Z'),
  slot('s3', 'Ungrouped session', '', '2026-08-01T21:00:00Z'),
]

const MODAL = '[role="dialog"]'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2, // 11-13px modal type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  // The shared stub answers /api/agents with a bare ARRAY, but useAgents (via
  // api.kirocrewAgents) reads `d.agents` / `d.default_agent` off an OBJECT, so
  // under the array shape `d.agents` is undefined, the roster is empty, and
  // every folder's default agent reads as an orphan -- which made the
  // installed-agent (no-notice) state unreachable in a shot. Override the shape
  // HERE rather than in the shared stub, which 200+ other harnesses depend on.
  const agentsRoster = {
    agents: [{ name: 'kirocrew', source: 'builtin' }, { name: 'oncall', source: 'aim' }],
    default_agent: 'kirocrew',
  }
  const extra = async (path, route) => {
    if (path === '/api/agents' || path === '/api/chat/agents') {
      json(route, agentsRoster)
      return true
    }
    return false
  }

  await stubDashboardApi(page, { folders, slots, extra })
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  async function shotModal(name) {
    await page.waitForSelector(MODAL, { timeout: 5000 })
    await page.waitForTimeout(400)   // let the spring settle
    await page.locator(MODAL).screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  async function closeModal() {
    // Click the X, not Escape: once the draft is dirty the modal deliberately
    // ignores the accidental-dismissal paths (backdrop + Escape) so a grazed
    // click cannot destroy a part-filled form. The X is the explicit path.
    await page.click(`${MODAL} button[aria-label="Close"]`)
    await page.waitForSelector(MODAL, { state: 'detached', timeout: 5000 })
  }

  // ── 01: create at the top level, via the sidebar's create menu ──
  await page.click('[aria-label="More create options"]')
  await page.click('text=New folder')
  await page.fill('[data-testid="folder-config-name"]', 'Payments rewrite')
  await shotModal(`${PREFIX}-01-create-top-level`)
  await closeModal()

  // ── 02: create a subfolder under Kiro › Backend (inherited project dir) ──
  await page.hover('[data-testid="folder-collapse-f1a"]')
  await page.click('[data-testid="folder-menu-f1a"]')
  await page.click('text=New subfolder')
  await page.fill('[data-testid="folder-config-name"]', 'Ledger')
  await shotModal(`${PREFIX}-02-create-nested-inherited-dir`)

  // ── 03: same modal with a palette color picked ──
  // (There is no emoji panel to reveal: folders carry a color, not an icon.)
  // Adjacent sibling, not `~`: the palette renders 12 sibling buttons after the
  // "no color" cell, so `~ button` matches all of them and only survives on
  // page.click's non-strict first-match. `+ button` is exactly the first swatch.
  await page.click('[data-testid="folder-config-color-reset"] + button')
  await page.waitForTimeout(200)
  await shotModal(`${PREFIX}-03-create-color-picked`)
  await closeModal()

  // ── 04: edit an existing folder via ⋯ → Folder settings ──
  // f2's default agent ('kirocrew') IS installed, so this frame demonstrates the
  // orphanAgent-toggle's else-branch: the plain Default agent hint with NO notice.
  // Assert the notice is absent so the frame proves the installed state rather
  // than merely not erroring.
  await page.hover('[data-testid="folder-collapse-f2"]')
  await page.click('[data-testid="folder-menu-f2"]')
  await page.click('[data-testid="folder-settings-f2"]')
  await page.waitForSelector(MODAL, { timeout: 5000 })
  await page.waitForTimeout(400)
  if (await page.locator('[data-testid="folder-config-agent-notice"]').count() !== 0) {
    throw new Error('frame 04 expected an INSTALLED default agent (no orphan notice), but the notice rendered')
  }
  await shotModal(`${PREFIX}-04-edit-folder-settings`)
  await closeModal()

  // ── 05: edit a folder whose default agent is an orphan ──
  // The Default agent field shows the "(not installed)" option and, below it,
  // the inline notice bound to the control via aria-describedby — so the reason
  // the selected agent won't run is announced WITH the field, not left floating.
  await page.hover('[data-testid="folder-collapse-f3"]')
  await page.click('[data-testid="folder-menu-f3"]')
  await page.click('[data-testid="folder-settings-f3"]')
  await page.waitForSelector('[data-testid="folder-config-agent-notice"]', { timeout: 5000 })
  await shotModal(`${PREFIX}-05-edit-orphan-agent-notice`)
  await closeModal()

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
