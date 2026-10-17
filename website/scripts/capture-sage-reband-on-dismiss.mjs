/**
 * Screenshot + recording evidence for a Code Review Sage report row re-banding
 * when its last red finding is dismissed (issue #18573).
 *
 * Frames from the `capture/sage-reband-on-dismiss` scene, which mounts the REAL
 * RunDetail behind the REAL SageProvider and stubs only `fetch` (statefully: a
 * dismissal re-bands the row on the next report read, as the backend does):
 *
 *   01-filtered-before.png   "Needs review" selected: two red rows, CR-8902
 *                            expanded with its red finding's Dismiss control
 *   02-filtered-after.png    the finding dismissed: CR-8902 is STILL listed, in
 *                            the same place, its dot now yellow, a "Now in
 *                            Worth a glance" line beside its pills, "Needs
 *                            review" now counts 1, its Undo control at hand
 *   03-filter-changed.png    "All" selected: the hold is released, CR-8902 now
 *                            sits below CR-8903 as its new band and score rank it
 *   04-all-before.png        a fresh page, "All" selected, CR-8902 expanded
 *   05-all-after.png         dismissed under "All": nothing is held, CR-8902 takes
 *                            its new place below CR-8903, dot yellow, counts
 *                            updated, its expanded card and Undo move with it
 *   filtered-dismiss.webm    the recording of the filtered flow (01 -> 03)
 *
 * Every frame is produced by CLICKING the real controls and then asserting the
 * row order and dot colour, so the frames are evidence of the shipped wiring; a
 * regression FAILS this script instead of quietly shooting a frame.
 *
 * Usage: node scripts/capture-sage-reband-on-dismiss.mjs [outDir]
 * Boots its own Vite dev server on loopback, so it needs no running gateway.
 */
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2]
  || fileURLToPath(new URL('../../temp-screenshots/18573-sage-reband-on-dismiss/', import.meta.url))
mkdirSync(OUT, { recursive: true })

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const vite = await createServer({
  root: ROOT,
  configFile: join(ROOT, 'vite.config.ts'),
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await vite.listen()
const { port } = vite.httpServer.address()
const base = `http://127.0.0.1:${port}`

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const problems = []

async function openScene(record) {
  const context = await browser.newContext({
    viewport: { width: 800, height: 1400 },
    deviceScaleFactor: 2,
    ...(record ? { recordVideo: { dir: OUT, size: { width: 800, height: 1400 } } } : {}),
  })
  const page = await context.newPage()
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`console: ${m.text()}`) })
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`HTTP ${r.status()} ${r.url()}`) })
  page.on('requestfailed', (r) => problems.push(`requestfailed ${r.url()} ${r.failure()?.errorText}`))
  await page.goto(`${base}/capture/sage-reband-on-dismiss.html?theme=dark`, { waitUntil: 'networkidle' })
  await page.getByText('Re-band a report row when its findings are dismissed').waitFor({ state: 'visible', timeout: 20_000 })
  return { context, page }
}

/** The row cards in display order, by PR number. */
async function order(page) {
  return page.locator('a[title="Open the pull request"]').allInnerTexts()
}

/** The band dot colour class of the card holding this title. */
async function dotOf(page, title) {
  const card = page.getByText(title).locator('xpath=ancestor::div[contains(@class,"rounded-lg")][1]')
  const dot = card.locator('span.mt-1.flex-shrink-0').first()
  const cls = await dot.getAttribute('class')
  return cls.includes('text-danger') ? 'red' : cls.includes('text-warn') ? 'yellow' : cls.includes('text-ok') ? 'green' : cls
}

/** Shoot from the top of the pane: the dismiss form scrolls the detail pane,
 *  and a frame that cuts off the band chips shows nothing about the band. */
async function shoot(page, name) {
  await page.locator('.overflow-y-auto').first().evaluate((el) => { el.scrollTop = 0 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: join(OUT, name) })
  console.log(`captured ${name}`)
}

async function expect(cond, msg) {
  if (!cond) throw new Error(msg)
}

const TARGET = 'Re-band a report row when its findings are dismissed'
const chip = (page, name) => page.getByRole('button', { name }).first()

async function expandTarget(page) {
  const card = page.getByText(TARGET).locator('xpath=ancestor::div[contains(@class,"rounded-lg")][1]')
  await card.locator('button[aria-expanded]').click()
  await card.getByRole('button', { name: 'Dismiss', exact: true }).first().waitFor({ state: 'visible', timeout: 20_000 })
  return card
}

async function dismissRedFinding(page, card) {
  // The red finding is the first card; its Dismiss opens the reason form.
  await card.getByRole('button', { name: 'Dismiss', exact: true }).first().click()
  const reason = card.getByLabel('Why dismiss this finding?')
  await reason.waitFor({ state: 'visible', timeout: 20_000 })
  await reason.fill('Fixed on the branch already')
  await card.getByRole('button', { name: 'Dismiss finding' }).click()
  await card.getByText('Dismissed: Fixed on the branch already').waitFor({ state: 'visible', timeout: 20_000 })
  // The report refetch has landed once the "Needs review" count moved.
  await page.getByRole('button', { name: /Needs review\s*1/ }).waitFor({ state: 'visible', timeout: 20_000 })
}

// --- Flow 1: under the "Needs review" filter (recorded) ----------------------
{
  const { context, page } = await openScene(true)
  await chip(page, /Needs review/).click()
  await expect((await order(page)).join() === '#8901,#8902', `filtered before: ${await order(page)}`)
  const card = await expandTarget(page)
  await shoot(page, '01-filtered-before.png')

  await dismissRedFinding(page, card)
  const after = await order(page)
  await expect(after.join() === '#8901,#8902', `filtered after: the acted-on row must stay listed, saw ${after}`)
  await expect((await dotOf(page, TARGET)) === 'yellow', 'filtered after: the dot must show the new band')
  await expect(await card.getByRole('button', { name: 'Undo' }).isVisible(), 'filtered after: Undo must stay at hand')
  await expect(await card.getByText('Now in Worth a glance').isVisible(), 'filtered after: the row must say it is kept in place')
  await shoot(page, '02-filtered-after.png')

  await chip(page, /^All/).click()
  const released = await order(page)
  await expect(released.join() === '#8901,#8903,#8902', `filter changed: the hold must release, saw ${released}`)
  await expect((await page.getByText('Now in Worth a glance').count()) === 0, 'filter changed: the moved line goes with the hold')
  await shoot(page, '03-filter-changed.png')

  const video = page.video()
  await context.close()
  const path = await video.path()
  renameSync(path, join(OUT, 'filtered-dismiss.webm'))
  console.log('captured filtered-dismiss.webm')
}

// --- Flow 2: under "All" ------------------------------------------------------
{
  const { context, page } = await openScene(false)
  await expect((await order(page)).join() === '#8901,#8902,#8903', `all before: ${await order(page)}`)
  const card = await expandTarget(page)
  await shoot(page, '04-all-before.png')

  await dismissRedFinding(page, card)
  const after = await order(page)
  // Nothing is held under All: the row takes the stored order at once, its card
  // (still expanded, Undo at hand) moves with it, and no hold line is shown.
  await expect(after.join() === '#8901,#8903,#8902', `all after: the row must take its new place, saw ${after}`)
  await expect((await dotOf(page, TARGET)) === 'yellow', 'all after: the dot must show the new band')
  await expect(await card.getByRole('button', { name: 'Undo' }).isVisible(), 'all after: Undo must stay at hand on the moved card')
  await expect((await page.getByText('Now in Worth a glance').count()) === 0, 'all after: no hold line under All')
  await shoot(page, '05-all-after.png')
  await context.close()
}

if (problems.length) {
  console.error(`page problems:\n  ${problems.join('\n  ')}`)
}
await browser.close()
await vite.close()
if (problems.length) process.exit(1)
