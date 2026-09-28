/**
 * Screenshot harness for the folder modal's "Additional steering" section.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 * Captures the two states the section adds to FolderConfigModal:
 *   01 edit, filled list   → a folder that declares two steering directories:
 *                            each row shows the path plus its × remove button
 *   02 create, child       → a subfolder under that folder: its own list is
 *                            empty and the parent's directories appear under
 *                            "Inherited from parent folders", read-only
 *
 * Usage: node scripts/capture-folder-steering.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-steering'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

// f1 declares two steering roots so its settings render the FILLED list; f1a
// is its child with none of its own, so a create-under-f1a shot shows the
// inherited group (the resolver walks every ancestor, root-first).
const folders = [
  {
    id: 'f1', name: 'Platform', icon: '🚀', order: 0, collapsed: false,
    project_dir: '/srv/repos/kirocrew',
    steering_dirs: ['/srv/org-standards/steering', '/srv/security-rules'],
  },
  { id: 'f1a', name: 'Backend', icon: '🧩', order: 0, collapsed: false, parent_id: 'f1' },
]

const slot = (key, title, folder_id, last_ts) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew',
  created: '2026-07-20T01:00:00Z', last_ts, folder_id,
})

const slots = [
  slot('s1', 'Steering rows', 'f1', '2026-08-01T20:00:00Z'),
  slot('s2', 'Inherited steering', 'f1a', '2026-08-01T19:00:00Z'),
]

const MODAL = '[role="dialog"]'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1000 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  const agentsRoster = {
    agents: [{ name: 'kirocrew', source: 'builtin' }],
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
    await page.waitForTimeout(400)
    await page.locator(MODAL).screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  async function closeModal() {
    await page.click(`${MODAL} button[aria-label="Close"]`)
    await page.waitForSelector(MODAL, { state: 'detached', timeout: 5000 })
  }

  // ── 01: Folder settings on f1 -- two rows, each with its remove button ──
  await page.hover('[data-testid="folder-collapse-f1"]')
  await page.click('[data-testid="folder-menu-f1"]')
  await page.click('[data-testid="folder-settings-f1"]')
  await page.waitForSelector('[data-testid="folder-config-steering-dir-1"]', { timeout: 5000 })
  if (await page.locator('[data-testid="folder-config-steering-dir-remove-0"]').count() !== 1) {
    throw new Error('frame 01 expected a remove button on the first steering row')
  }
  await shotModal(`${PREFIX}-01-edit-steering-rows`)
  await closeModal()

  // ── 02: New subfolder under Backend -- inherited group from Platform ──
  await page.hover('[data-testid="folder-collapse-f1a"]')
  await page.click('[data-testid="folder-menu-f1a"]')
  await page.click('text=New subfolder')
  await page.fill('[data-testid="folder-config-name"]', 'Ledger')
  await page.waitForSelector('[data-testid="folder-config-steering-inherited"]', { timeout: 5000 })
  if (await page.locator('[data-testid="folder-config-steering-dirs"] [data-testid^="folder-config-steering-dir-"]').count() !== 0) {
    throw new Error('frame 02 expected the child folder to declare no steering dirs of its own')
  }
  await shotModal(`${PREFIX}-02-create-child-inherited-steering`)
  await closeModal()

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
