/**
 * Screenshots of Dev Fleet's untracked-scratch cleanup affordances.
 *
 * Drives the ISOLATED capture entry (website/capture/devfleet-prune-untracked.html),
 * which mounts the REAL DevFleetPage with `fetch` stubbed at the network seam to
 * serve the `/fleet`, `/prune-candidates`, and `/worktree?name=` payloads. This
 * script performs the SAME clicks a user does — "Prune merged" for the prune
 * dialog, expand-then-Remove for the confirm — so the evidence exercises the
 * exact prune-preview classifier, per-row checkbox enable/disable logic, and
 * remove-confirm sentence composition the PR changes. Each scene asserts its
 * distinguishing copy before shooting, so this can never quietly emit a
 * screenshot of a closed dialog or the wrong branch.
 *
 * Usage:
 *   node ./node_modules/.bin/vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-devfleet-prune-untracked.mjs http://127.0.0.1:6813 "$KIROCREW_SCRATCH/shots"
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || `${process.env.KIROCREW_SCRATCH || '/tmp'}/shots`
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })

async function open(scene) {
  await page.goto(`${BASE}/capture/devfleet-prune-untracked.html?scene=${scene}&theme=dark`)
  await page.waitForSelector('text=Dev Fleet', { timeout: 15000 })
}

// SCENE A — the prune review dialog. Click "Prune merged" to fire
// GET /prune-candidates, then assert the KEPT section, the new hint paragraph,
// and each row's inline label before shooting.
await open('prune')
await page.getByRole('button', { name: /Prune merged/i }).click()
await page.waitForSelector('text=Prune worktrees', { timeout: 15000 })
for (const text of [
  'Kept',
  'Rows held up only by untracked scratch', // the new hint paragraph
  'wt-brief-notes',
  'frontend-brief-p0.md',                    // scratch-only row shows filenames
  'wt-session-lock',
  'wt-unreadable',
]) {
  await page.waitForSelector(`text=${text}`, { timeout: 15000 })
}
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/01-prune-dialog-dark.png`, fullPage: false })
console.log('captured 01-prune-dialog-dark.png')

// SCENE B — the single-row remove confirm. Expand the row (fires
// GET /worktree?name=) then click Remove, and assert the unmerged-work warning
// AND the additive discard line appear together in one dialog.
await open('remove')
await page.getByRole('button', { name: /^Expand$/ }).click()
await page.getByRole('button', { name: /^Remove$/ }).click()
for (const text of [
  'Has unmerged work',                       // the force-delete warning
  'Also discards untracked files',           // the additive discard line
  'a.py, b.md, c.mjs',                        // the named files
]) {
  await page.waitForSelector(`text=${text}`, { timeout: 15000 })
}
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/02-remove-confirm-dark.png`, fullPage: false })
console.log('captured 02-remove-confirm-dark.png')

await browser.close()
