/** Real-browser evidence for #25861's "Group by kind" option, in the TREE lane
 * (the default view, `readStoredLane()` -> 'tree').
 *
 * Drives website/capture/group-by-kind-25861.html -- the REAL ChatSidebar with
 * unfiled sessions of each kind at the top level and one folder (Alpha) that
 * keeps its own session. Shot in dark and light:
 *
 *   group-by-kind-menu   the Sort and filter menu open, showing the new
 *                        "Group by kind" row in the default tree lane.
 *   group-by-kind-list   the option on: the top-level unfiled list split into
 *                        Crew Mode / Normal / From crewmates sections, the
 *                        pinned row still leading, and the Alpha folder below
 *                        with its own session left as it is.
 *
 * Usage (website/ is the vite root):
 *   npx vite --host 127.0.0.1 --port 6875 --strictPort
 *   node scripts/capture-25861-group-by-kind.mjs http://127.0.0.1:6875 ../temp-screenshots/group-by-kind
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6875'
const OUT = resolve(process.argv[3] || '../temp-screenshots/group-by-kind')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

async function open(query, size = { width: 360, height: 760 }) {
  const ctx = await browser.newContext({ viewport: size, deviceScaleFactor: 2 })
  ctx.setDefaultTimeout(120000)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/group-by-kind-25861.html?${query}`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('[data-testid="tree-view-lane"]'); await page.waitForTimeout(400)
  return { ctx, page, errors }
}

// -- The menu in the default (tree) lane, showing the new row (dark then light) --
for (const theme of ['dark', 'light']) {
  const { ctx, page, errors } = await open(`theme=${theme}`)
  await page.getByLabel('Sort and filter sessions').press('Enter')
  const row = page.getByTestId('group-by-kind-toggle')
  await row.waitFor()
  check(`[${theme}] Group by kind row present in the tree-lane menu`, await row.count() === 1)
  check(`[${theme}] the row reads "Group by kind"`, (await row.innerText()).includes('Group by kind'))
  await row.scrollIntoViewIfNeeded()
  await page.waitForTimeout(300)
  await page.screenshot({ path: resolve(OUT, `group-by-kind-menu-${theme}.png`) })
  check(`[${theme}] no page errors`, errors.length === 0)
  await ctx.close()
}

// -- The grouped unfiled list in the tree lane (dark then light) --
for (const theme of ['dark', 'light']) {
  const { ctx, page, errors } = await open(`theme=${theme}&group=1`)
  const lane = page.getByTestId('tree-view-lane')
  const headers = (await lane.locator('[data-testid="date-segment-header"]').allInnerTexts()).map(t => t.trim().toUpperCase())
  check(`[${theme}] three kind headers render in the tree lane`, headers.join('|') === 'CREW MODE|NORMAL|FROM CREWMATES')
  // The Alpha folder is still on screen (folders stay as they are).
  check(`[${theme}] the Alpha folder is still shown`, (await lane.locator('[data-folder-row="f1"]').count()) === 1)
  await page.waitForTimeout(300)
  await page.screenshot({ path: resolve(OUT, `group-by-kind-list-${theme}.png`) })
  check(`[${theme}] no page errors`, errors.length === 0)
  await ctx.close()
}

await browser.close()
console.log(failures ? `FAILURES: ${failures}` : 'ALL OK')
process.exit(failures ? 1 : 0)
