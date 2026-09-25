/**
 * Screenshots of the artifact detail page's SYNC-BANNER ABORT state (#7818),
 * driven through the REAL page's own flush code: the dirty editor buffer is
 * flushed before a sync action, the flush comes back 409 (the content changed
 * since it was loaded), and the action stops instead of pushing the stale
 * buffer over the newer content (website/capture/artifact-sync-conflict.html
 * stubs the network answers).
 *
 * Each frame ASSERTS the state before writing the file, so a frame cannot
 * silently document a page that never conflicted:
 *   - the edit bar's conflict notice ("Save refused — content changed" /
 *     "Content changed since you loaded it") is up;
 *   - the sync banner shows the SAME short refusal inline, beside its button,
 *     saying why its action did not run;
 *   - the editor still holds the draft (the flush was refused, nothing moved);
 *   - Save reads "Save — overwrite newer content": after the 409 the plain Save
 *     is the one path that overwrites, and its label says so;
 *   - exactly one PATCH was issued (the refused flush) — the sync action's own
 *     snapshot=true write never ran.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort   # in another shell
 *   node scripts/capture-artifact-sync-conflict.mjs http://127.0.0.1:6842 <out dir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/artifact-sync-conflict'
mkdirSync(OUT, { recursive: true })

const ALERT = '[role="alert"]'
const DRAFT = 'Edited in this window before the other writer landed.'

const browser = await chromium.launch()
// 1280x800 is the layout the banner is designed at; scale 1.5 keeps the PNG at
// 1920x1200, under the 2000px-per-side limit of the review image readers (a
// scale of 2 would be 2560 wide and rejected unread).
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1.5 })

let failed = false
for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/artifact-sync-conflict.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.addStyleTag({ content: '*, *::before, *::after { animation: none !important; transition: none !important; }' })

  // The sync banner needs the provider registry and upstream probe to settle.
  const syncButton = page.getByRole('button', { name: 'Snapshot to publish', exact: true })
  await syncButton.waitFor()

  // Edit → type into the real editor (Pierre renders it inside a shadow root)
  // so the buffer is genuinely dirty and Save arms.
  await page.getByTitle('Edit content').click()
  const editor = page.locator('diffs-container [contenteditable]')
  await editor.waitFor()
  await editor.click()
  await page.keyboard.press('Control+End')
  await page.keyboard.type(`\n${DRAFT}`)
  const save = page.getByRole('button', { name: /^Save/ })
  await save.filter({ hasNot: page.locator('[disabled]') }).waitFor()
  const saveBefore = (await save.textContent() || '').trim()

  // The sync action: its pre-flush sends the edit-base token → 409 → abort.
  await syncButton.click()
  await page.locator(ALERT).filter({ hasText: /Content changed since you loaded it/ }).waitFor()
  await syncButton.filter({ hasNot: page.locator('[disabled]') }).waitFor()

  const alerts = await page.locator(ALERT).allInnerTexts()
  const banner = page.locator('div').filter({ has: syncButton }).last()
  const bannerText = (await banner.innerText()).replace(/\s+/g, ' ')
  const draftKept = await page.getByText(DRAFT).count()
  const saveAfter = (await save.textContent() || '').trim()
  const snapshotButton = page.getByRole('button', { name: 'Snapshot', exact: true })
  const snapshotDisabled = await snapshotButton.isDisabled()
  // The harness stub counts content PATCHes (they never cross the network, so
  // they cannot be counted here): exactly one, the refused flush — the sync
  // action's own snapshot=true write never ran.
  const patches = await page.evaluate(() => window.__syncConflictCounters?.patches)

  const ok =
    alerts.some(t => /Save refused — content changed/.test(t) && /Content changed since you loaded it/.test(t)) &&
    /Save refused — content changed/.test(bannerText) && /Snapshot to publish/.test(bannerText) &&
    draftKept === 1 &&
    saveBefore === 'Save' && saveAfter === 'Save — overwrite newer content' &&
    snapshotDisabled &&
    patches === 1
  console.log(`${theme}: alerts=${alerts.length} banner "${bannerText.slice(0, 90)}" draft=${draftKept} save "${saveBefore}" → "${saveAfter}" snapshotDisabled=${snapshotDisabled} patches=${patches} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  await page.screenshot({ path: `${OUT}/sync-abort-${theme}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
