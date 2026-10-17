/**
 * Screen recording for the crew-bindings isolation preview notice.
 *
 * Walks the surfaces a still frame cannot prove in sequence: banner on the card
 * roster, switch to List so the two column "?" appear, open one, back to Cards,
 * open the editor sheet and expand the Workspace tip. Same gateway-free harness
 * as the capture script.
 *
 * Usage: node scripts/record-crew-preview-notice.mjs <outDir>
 *   Prints `WEBM <path>` as its last line; convert to mp4/gif with ffmpeg.
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = process.argv[2] || '/tmp/crew-preview-rec'
mkdirSync(OUT, { recursive: true })
// Playwright names the video by a random id, so the only way to tell this run's
// capture from a file that was already there is to record what was there first.
// Nothing is deleted or overwritten: OUT is caller-supplied and may hold videos
// that are none of this script's business.
const preexisting = new Set(readdirSync(OUT).filter(f => f.endsWith('.webm')))

const CREWS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' },
  { name: 'oncall', kiro_agent: 'oncall', workspace: 'oncall', memory_store: 'default' },
  { name: 'research', kiro_agent: 'kirocrew', workspace: 'research', memory_store: 'research' },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: OUT, size: { width: 1280, height: 800 } },
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { extra: crewsApi({ crews: CREWS, defaultAgent: 'kirocrew' }) })

  const main$ = page.locator('#main-content')
  await page.goto(base + '/capabilities', { waitUntil: 'domcontentloaded' })
  await main$.locator('[data-testid="crew-card"]').first().waitFor({ timeout: 15000 })
  await page.waitForTimeout(1800) // hold on the banner long enough to read it

  await main$.getByRole('button', { name: 'List' }).click()
  await main$.locator('table').waitFor({ timeout: 15000 })
  await page.waitForTimeout(900)
  await main$.locator('th button[title*="Isolated memory per crew"]').first().click()
  await page.waitForTimeout(1800)
  await page.keyboard.press('Escape')

  await main$.getByRole('button', { name: 'Cards' }).click()
  await main$.locator('[data-testid="crew-card"]').first().waitFor()
  await page.waitForTimeout(600)

  await main$.locator('[data-testid="crew-card"]').first().click()
  const sheet = page.getByRole('dialog')
  await sheet.waitFor({ timeout: 15000 })
  await page.waitForTimeout(900)
  await sheet.locator('button[title*="Isolated memory per crew"]').first().click()
  await page.waitForTimeout(2200)

  await context.close() // flushes the video file
  await browser.close()
  srv.close()

  const produced = readdirSync(OUT).filter(f => f.endsWith('.webm') && !preexisting.has(f))
  // Exactly one: the context records one page. Zero means the capture never
  // started; more than one means an assumption changed and picking arbitrarily
  // would silently ship the wrong clip as evidence.
  if (produced.length !== 1) {
    throw new Error(
      `expected exactly 1 new .webm in ${OUT}, found ${produced.length}`
      + ' — the recording did not start, or the page count changed',
    )
  }
  console.log(`WEBM ${join(OUT, produced[0])}`)
}

main().catch(err => { console.error(err); process.exit(1) })
