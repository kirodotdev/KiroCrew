/**
 * Capture + regression harness for the layout editor's INVALID (no-op) drop
 * preview — the state the UX review on PR #14062 could not verify from the
 * supplied stills (shot-02 showed a valid drag, ghost reading "Chat", not the
 * invalid state).
 *
 * The coded invalid state is `preview && !preview.valid` (`LayoutEditor.tsx`):
 * a pane dragged where it cannot fit even with the victim under the pointer
 * excluded. It paints the covering cells and the blocking pane MUTED
 * (`.le-invalid`, deliberately NOT danger red — a no-op must read differently
 * from a destructive replace) and the ghost reads "Doesn't fit"
 * (`components.crewLayout.doesNotFit`).
 *
 * A palette tile is always 1×1 and so always fits or replaces — it can never go
 * invalid. The invalid state needs a MULTI-CELL pane in a grid too full for it
 * to relocate. This PR ships no resize grip, so we seed that arrangement through
 * the dev harness's `?seed=` override: a 3×1 grid with a 2-wide Chat pane at
 * cols 0-1 and a 1-wide Files pane at col 2. Dragging Chat's title bar rightward
 * anchors its 2-wide target at col 1, which would span cols 1-2 and overlap
 * Files even with Chat itself excluded — an unfittable, invalid drop.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server, at
 * the real `/developer/layout-editor` route, with REAL pointer events (the
 * editor's drag is pointer-event based — no synthetic shortcut). It asserts the
 * invalid cue and ghost text are actually present before shooting, so it is a
 * regression test and not just a camera: before the coded `!preview.valid`
 * branch, this drag would show no muted cue and no "Doesn't fit" ghost.
 *
 * Usage: node scripts/capture-layout-editor-invalid-drop.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/14062-layout-editor-core'
const VIEW = { width: 1280, height: 800 } // the width the UX review flagged the hint at

// A 3-wide, 1-row grid: a 2-wide Chat pane (cols 0-1) and a 1-wide Files pane
// (col 2). Chat cannot move right without overlapping Files → invalid drop.
const SEED = {
  cols: 3,
  rows: 1,
  items: [
    { id: 'wide-chat', element: 'chat', x: 0, y: 0, w: 2, h: 1 },
    { id: 'blocker-files', element: 'files', x: 2, y: 0, w: 1, h: 1 },
  ],
}

mkdirSync(OUT, { recursive: true })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({
    executablePath: chromiumExecutable(),
    args: ['--no-sandbox'],
  })
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` — ${note}` : ''}`)
  }

  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)

  // Boot the real SPA past its onboarding/prerequisite gate with the shared
  // fixture stub (sets mc-onboarded and answers the ~25 boot endpoints); without
  // it the gateway-free app shows the onboarding overlay (fixed inset-0 z-[120]),
  // which intercepts every pointer event and no drag ever reaches the editor.
  await stubDashboardApi(page, { theme: 'light' })

  const seed = encodeURIComponent(JSON.stringify(SEED))
  await page.goto(`${base}/developer/layout-editor?seed=${seed}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="layout-editor"]', { timeout: 12000 })
  // Both seeded panes must have mounted before we grab one.
  await page.waitForSelector('[aria-label="Move Chat"]', { timeout: 12000 })
  await page.waitForSelector('[aria-label="Move Files"]', { timeout: 12000 })
  await page.waitForTimeout(400)

  const shot = async (name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // Drive a real title-bar drag of the 2-wide Chat pane toward the RIGHT column.
  // Anchoring the 2-wide pane at col 1 makes it span cols 1-2, colliding with
  // the Files pane at col 2 — invalid even with Chat excluded.
  const bar = page.locator('[aria-label="Move Chat"]')
  const canvas = page.locator('.le-canvas')
  const bb = await bar.boundingBox()
  const cb = await canvas.boundingBox()
  if (!bb || !cb) throw new Error(`missing geometry: bar=${!!bb} canvas=${!!cb}`)

  const sx = bb.x + bb.width / 2
  const sy = bb.y + bb.height / 2
  // Target the horizontal centre of the LAST (rightmost) column so the dragged
  // pane's grabbed origin lands at col 1 → 2-wide span hits col 2 (Files).
  const tx = cb.x + cb.width * (5 / 6) // centre of col 3 of 3
  const ty = cb.y + cb.height / 2

  await page.mouse.move(sx, sy)
  await page.mouse.down()
  await page.mouse.move(sx + 8, sy + 4, { steps: 3 }) // cross the 5px threshold
  await page.waitForTimeout(120)
  for (let i = 1; i <= 12; i++) {
    await page.mouse.move(sx + ((tx - sx) * i) / 12, sy + ((ty - sy) * i) / 12)
    await page.waitForTimeout(30)
  }
  await page.waitForTimeout(350)

  // Assertions (make it a regression test, not just a camera) --------------
  const mutedCell = await page.locator('.le-cell.le-invalid').count()
  const mutedItem = await page.locator('.le-item.le-invalid').count()
  const hasDanger = await page.locator('.le-cell.le-replace, .le-item.le-replace').count()
  const ghostText = (await page.locator('.le-ghost').innerText().catch(() => '')).trim()

  record('invalid drop paints the covering cell(s) muted (.le-cell.le-invalid)', mutedCell > 0, `count=${mutedCell}`)
  record('invalid drop paints the blocking pane muted (.le-item.le-invalid)', mutedItem > 0, `count=${mutedItem}`)
  record('invalid drop shows NO danger-red replace cue', hasDanger === 0, `replaceCount=${hasDanger}`)
  record('the ghost reads the invalid "Doesn\'t fit" label', /doesn.t fit/i.test(ghostText), `ghost="${ghostText}"`)

  await shot('mid-drag-invalid-muted')

  await page.mouse.up()
  await page.waitForTimeout(200)

  await page.close()
  await context.close()
  await browser.close()
  srv.close()

  const failed = results.filter((r) => !r.pass)
  console.log(`\n--- ${results.length - failed.length}/${results.length} assertions passed ---`)
  if (failed.length) {
    for (const f of failed) console.log(`FAILED: ${f.name} — ${f.note}`)
    process.exitCode = 1
  }
}

main().catch((e) => {
  console.error(e)
  process.exitCode = 1
  setTimeout(() => process.exit(1), 500)
})
