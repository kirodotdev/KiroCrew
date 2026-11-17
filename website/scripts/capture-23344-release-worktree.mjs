/**
 * Screenshots of Dev Fleet's "Create worktree from latest stable" action, in the
 * toolbar's More actions menu.
 *
 * Drives the ISOLATED capture entry (website/capture/devfleet-release-worktree.html),
 * which mounts the REAL DevFleetPage with `fetch` stubbed at the network seam, so the
 * menu, the item's own `releasing` state, the success toast and the refusal banner all
 * execute as in production. Each scene asserts its headline state before shooting, so
 * this can never quietly emit a frame of the wrong state; the frames are then checked
 * for distinct byte sizes, so a silently-failed fixture is caught.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-23344-release-worktree.mjs http://127.0.0.1:6812 ../temp-screenshots/release-worktree
 *
 * The default output directory is gitignored: attach the frames to the PR description.
 */
import { chromium } from 'playwright'
import { mkdirSync, statSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/release-worktree'
mkdirSync(OUT, { recursive: true })

// The menu trigger carries a test id; the item is found by its accessible name, which
// is its visible label ("Create worktree from latest stable", then "Creating stable
// worktree…" while the action runs).
const TRIGGER = '[data-testid="fleet-toolbar-more"]'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })
const shot = []

async function load(create) {
  const q = create ? `?theme=dark&create=${create}` : '?theme=dark'
  await page.goto(`${BASE}/capture/devfleet-release-worktree.html${q}`)
  await page.locator(TRIGGER).waitFor({ timeout: 15000 })
}

const item = (name) => page.getByRole('menuitem', { name, exact: true })

async function openMenu() {
  await page.locator(TRIGGER).click()
  await page.getByRole('menu').waitFor({ timeout: 15000 })
}

async function runAction() {
  await openMenu()
  await item('Create worktree from latest stable').click()
}

async function shoot(name) {
  await page.mouse.move(4, 4) // park the pointer off the toolbar and the menu
  await page.waitForTimeout(300)
  const path = `${OUT}/${name}`
  await page.screenshot({ path, fullPage: false })
  shot.push({ name, size: statSync(path).size })
  console.log(`captured ${name}`)
}

// 1. The menu open: Prune merged is the one standalone button; Refresh and "Create
// worktree from latest stable" are the menu's items (max-two-buttons-per-row counts the
// trigger).
await load('')
if (!(await page.getByRole('button', { name: 'Prune merged', exact: true }).isVisible())) {
  throw new Error('menu: the toolbar must keep its "Prune merged" button')
}
if (await page.getByRole('button', { name: /^Refresh/ }).count()) {
  throw new Error('menu: Refresh must not be a toolbar button')
}
await openMenu()
for (const name of ['Refresh', 'Create worktree from latest stable']) {
  if (await item(name).isDisabled()) throw new Error(`menu: "${name}" must be enabled`)
}
await shoot('01-release-worktree-menu-open-dark.png')

// 2. In flight: the POST never resolves; the trigger is busy and, reopened, the item is
// disabled and reads "Creating stable worktree…".
await load('busy')
await runAction()
await page.waitForFunction(
  (sel) => document.querySelector(sel)?.getAttribute('aria-busy') === 'true',
  TRIGGER,
  { timeout: 15000 },
)
await page.getByRole('menu').waitFor({ state: 'detached', timeout: 15000 })
await openMenu()
if (!(await item('Creating stable worktree…').isDisabled())) throw new Error('creating: the item must be disabled')
await shoot('02-release-worktree-creating-dark.png')

// 3. Success: the POST returns {ok:true}; the toast names the worktree and version, and
// the fleet re-read after invalidateAll() shows the new row.
await load('ok')
await runAction()
await page.waitForSelector('text=Created release-channel-stable at 0.10.0', { timeout: 15000 })
await page.getByText('release-channel-stable', { exact: true }).first().waitFor({ timeout: 15000 })
await shoot('03-release-worktree-success-toast-dark.png')

// 4. Refusal: the POST returns 409; the action-error banner carries the server's reason.
await load('fail')
await runAction()
await page.waitForSelector('text=No stable release found on the remote yet. Nothing was created.', { timeout: 15000 })
await shoot('04-release-worktree-refusal-dark.png')

const sizes = new Set(shot.map((s) => s.size))
if (sizes.size !== shot.length) {
  throw new Error(`frames are not distinct: ${shot.map((s) => `${s.name}=${s.size}`).join(', ')}`)
}
console.log(shot.map((s) => `${s.name} ${s.size}B`).join('\n'))
await browser.close()
