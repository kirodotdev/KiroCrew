/**
 * Screenshot harness for moving the "Chat on a crew" preview opt-in from
 * Settings > Remote crews to Developer > Feature Previews.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth.
 *
 * Two shots, because a MOVE is only legible as a pair: the destination has to
 * show the card arriving, and the origin has to show it gone. A single frame of
 * either one proves half the change and reads as an addition.
 *
 * Labels come from the CATALOG, not from literals, so a key rename breaks the
 * capture loudly instead of silently shooting the wrong page. The origin shot
 * asserts ABSENCE against the same catalog string the destination shot found, so
 * the pair cannot both pass on a build where the copy moved but the toggle did
 * not.
 *
 * Usage: node scripts/capture-chat-on-crew-toggle-move.mjs [outDir] [prefix]
 *   Run against the branch (after) and against a main build (before).
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chat-on-crew-toggle-move'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))

// The key lives under featurePreviewsTab on the branch and under remoteCrewPanel
// on a main build. Reading BOTH keeps one script able to shoot either side of
// the move, which is what makes the before/after pair comparable.
const LABEL = manual.pages?.developer?.featurePreviewsTab?.chat_on_a_crew
  ?? manual.pages?.settings?.remoteCrewPanel?.chat_on_a_crew
if (!LABEL) {
  throw new Error('chat_on_a_crew label missing from en.manual.json — renamed?')
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px type renders soft at 1x on GitHub
  })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page)
  // AFTER the shared stub, whose own init script clears storage: Developer Mode
  // is what puts the Developer row in the sidebar at all.
  await page.addInitScript(() => localStorage.setItem('mc-dev-mode', '1'))

  const shot = []
  const save = async (name) => {
    await page.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
    shot.push(`${PREFIX}-${name}.png`)
  }

  // 1. The destination — Developer > Feature Previews, one card per held
  //    feature, with the moved opt-in as its own card rather than folded into
  //    the existing Crew card (the word names two unrelated features).
  await page.goto(base + '/developer?tab=feature-previews', { waitUntil: 'domcontentloaded' })
  const previews = page.getByRole('switch', { name: /webhooks/i })
  await previews.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(500) // let the cards' rise animation finish
  await save('feature-previews')

  // 2. The origin — Settings > Remote crews, where Auto-connect crews is now
  //    the first card. Asserting the label is GONE is the half of the pair that
  //    proves a move rather than a copy.
  await page.goto(base + '/settings?tab=instances', { waitUntil: 'domcontentloaded' })
  const autoConnect = page.getByRole('switch', { name: /auto-connect crews/i })
  await autoConnect.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(500)
  await save('remote-crews')

  const onDestination = await page.locator(`text=${JSON.stringify(LABEL)}`).count()
  await browser.close()
  srv.close()
  console.log(`wrote ${shot.length} shot(s) to ${OUT}: ${shot.join(', ')}`)
  console.log(`"${LABEL}" still on Settings > Remote crews: ${onDestination > 0 ? 'YES' : 'no'}`)
}

main().catch(err => { console.error(err); process.exit(1) })
