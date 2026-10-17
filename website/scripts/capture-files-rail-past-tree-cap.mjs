/**
 * Real-browser evidence for the Files rail on a project past the tree's
 * 10,000-row cap.
 *
 * Drives website/capture/files-rail-past-tree-cap.html, which mounts the REAL
 * `FileBrowserRail` with `/api/project/tree` and `/api/file-search` stubbed at
 * the fetch boundary. Every frame is reached by typing and clicking.
 *
 * Frames per theme:
 *   01-cut-tree          the whole listing, cut by the cap
 *   02-folder-loaded     `packages` expanded: its own listing brings the folders the cap hid
 *   03-name-search       a name query on a cut tree goes to the filename search
 *   04-folder-revealed   a folder hit, clicked: the tree opens on it
 *   05-open-in-folder-tab  a folder still cut on its own listing: its limit row opens it in a folder tab
 *   06-folder-unavailable  a cut folder answers 404: the no-longer-exists notice, dismiss only, and the re-read tree without it
 *   06b-folder-refused   a cut folder answers 403: the link-or-protected notice, dismiss only
 *   07-folder-load-failed  a cut folder's own listing fails: the notice above, the folder still cut
 *   08-search-pending    the search in flight
 *   09-search-failed     the search refused: its error, and the tree's own matches
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6838 --strictPort   # in another shell (website/)
 *   node scripts/capture-files-rail-past-tree-cap.mjs http://127.0.0.1:6838 ../temp-screenshots/files-rail-past-tree-cap
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6838'
const OUT = process.argv[3] || '../temp-screenshots/files-rail-past-tree-cap'
mkdirSync(OUT, { recursive: true })

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const theme of ['light', 'dark']) {
  const page = await browser.newPage({ viewport: { width: 520, height: 640 } })
  page.on('pageerror', e => { console.error(`[${theme}] pageerror:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/files-rail-past-tree-cap.html?theme=${theme}`, { waitUntil: 'networkidle', timeout: 120000 })
  const shot = async (name) => {
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${theme}-${name}.png` })
    console.log(`${theme}-${name}.png`)
  }
  // Rows by path: Pierre splits a label around its truncation point, so the
  // visible text of a row is not its name.
  const row = (path) => page.locator(`[data-type="item"][data-item-path="${path}"]`).first()
  const under = (folder) => page.locator(`[data-type="item"][data-item-path^="${folder}/"]:not([data-item-path="${folder}/"])`).first()

  await row('packages/core/').waitFor({ timeout: 15000 })
  await shot('01-cut-tree')

  await row('packages/core/').click()
  await row('packages/zz-billing/').waitFor({ timeout: 15000 })
  await shot('02-folder-loaded')
  await row('packages/').click()

  await page.getByLabel('Filter files…').fill('billing')
  await page.getByTestId('rail-name-search').getByText('packages/zz-billing', { exact: true }).waitFor({ timeout: 15000 })
  await shot('03-name-search')

  await page.getByTestId('rail-name-search').getByText('packages/zz-billing', { exact: true }).click()
  await row('packages/zz-billing/invoice.ts').waitFor({ timeout: 15000 })
  await shot('04-folder-revealed')

  await row('huge-assets/').click()
  await under('huge-assets').waitFor({ timeout: 15000 })
  await shot('05-open-in-folder-tab')
  await page.close()

  for (const [name, arm] of [['06-folder-unavailable', 'dir-gone'], ['06b-folder-refused', 'dir-refused'], ['07-folder-load-failed', 'dir-fail'], ['08-search-pending', 'search-pending'], ['09-search-failed', 'search-fail']]) {
    const p2 = await browser.newPage({ viewport: { width: 520, height: 640 } })
    p2.on('pageerror', e => { console.error(`[${theme}/${name}] pageerror:`, e.message); failures++ })
    await p2.goto(`${BASE}/capture/files-rail-past-tree-cap.html?theme=${theme}&arm=${arm}`, { waitUntil: 'networkidle', timeout: 120000 })
    const r2 = (path) => p2.locator(`[data-type="item"][data-item-path="${path}"]`).first()
    await r2('packages/core/').waitFor({ timeout: 15000 })
    if (arm === 'dir-fail' || arm === 'dir-gone' || arm === 'dir-refused') {
      await r2('packages/core/').click()
      await p2.getByTestId(arm === 'dir-gone' ? 'workspace-tree-folder-gone' : arm === 'dir-refused' ? 'workspace-tree-folder-refused' : 'workspace-tree-folder-listing-retry').waitFor({ timeout: 15000 })
      if (arm === 'dir-gone') await r2('packages/core/').waitFor({ state: 'detached', timeout: 15000 })
    } else {
      await p2.getByLabel('Filter files…').fill('util')
      if (arm === 'search-fail') await p2.getByTestId('rail-name-search-error').waitFor({ timeout: 15000 })
      else await p2.getByTestId('rail-name-search').waitFor({ timeout: 15000 })
    }
    await p2.waitForTimeout(400)
    await p2.screenshot({ path: `${OUT}/${theme}-${name}.png` })
    console.log(`${theme}-${name}.png`)
    await p2.close()
  }
}

await browser.close()
if (failures) {
  console.error(`${failures} page error(s)`)
  process.exit(1)
}
