/**
 * Capture harness for a crewmate's own change (`update_self`) in its DM thread.
 *
 * Shoots a RUNNING pod (`kirocrew pod up <worktree>`), not a fixture server: the
 * rows under test are written by the gateway when the real agent calls the real
 * tool, so the frames show the product end to end -- the user's ask, the tool
 * row, one `self` row per change (name only, description only, face) with the
 * old face, the new face and Undo, and the crewmate's reply -- then a row after
 * Undo was pressed, and the one state the pod cannot reach on demand: Undo
 * refused because the gateway holds no record any more (answered 410 by a
 * route stub on the undo request alone; everything else on the page is live).
 *
 * The pod and the crewmate are prepared outside this script (a `scout` crewmate
 * whose thread already holds four applied changes: name, description, face, then
 * name and face together; a second run adds the pending asks, which are change
 * cards rather than rows: approval mode, reasoning effort, tools). This script
 * only opens the thread and shoots. The pod token is used as the dashboard cookie
 * directly, so the script can run more than once against one pod (a `/?token=`
 * sign-in is single-use).
 *
 * Usage:
 *   POD_URL=http://127.0.0.1:7840 POD_TOKEN=<token> \
 *     node scripts/capture-crewmate-self-change.mjs [outDir] [member=scout]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/crewmate-self-change'
const MEMBER = process.argv[3] || 'scout'
const BASE = process.env.POD_URL
const TOKEN = process.env.POD_TOKEN
if (!BASE || !TOKEN) {
  console.error('POD_URL and POD_TOKEN are required (from `kirocrew pod up`)')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const ROW = '[data-testid="self-change-row"]'
const UNDO = '[data-testid="self-change-undo"]'
const CARD = '[data-testid="change-card"]'
const THREAD = `/members?member=${encodeURIComponent(MEMBER)}`

/** Navigate the way the sidebar does, in-app: a full load of the member route
 *  right after sign-in is aborted by the shell's own token redirect. */
async function goInApp(page, url) {
  await page.evaluate(u => {
    window.history.pushState({}, '', u)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, url)
}

async function settle(page) {
  await page.waitForSelector(`${ROW}, ${CARD}`, { timeout: 30_000 })
  // Let the faces (data-URI SVGs) and the thread's scroll settle.
  await page.waitForTimeout(800)
}

async function setTheme(page, mode) {
  await page.emulateMedia({ colorScheme: mode })
  await page.evaluate(m => localStorage.setItem('mc-theme', m), mode)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(500)
  await goInApp(page, THREAD)
  await settle(page)
}

const browser = await chromium.launch()
try {
  // The shell redirects 127.0.0.1 to localhost, so the cookie covers both hosts.
  const port = new URL(BASE).port
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })
  await context.addCookies([BASE, `http://localhost:${port}`].map(u => ({ name: `mc_token_${port}`, value: TOKEN, url: u })))
  const page = await context.newPage()
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
  // A pod is a fresh install: its first-run chapters (import, privacy) and the
  // Crewmates page's own "Meet CrewMates" intro would sit over the thread. The
  // same keys the e2e auth setup seeds mark them done.
  await page.evaluate(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-crewmates-onboarded', '1')
    localStorage.removeItem('mc-crewmates-pending')
  })
  await page.waitForTimeout(800)

  for (const scheme of ['light', 'dark']) {
    await setTheme(page, scheme)
    await page.screenshot({ path: `${OUT}/thread-after-change-${scheme}.png` })
    // One frame per row kind, in the order the changes were made. The row texts
    // are printed so the frames can be checked against what they should say.
    const rows = page.locator(ROW)
    const n = await rows.count()
    console.log(scheme, 'rows:', JSON.stringify(await rows.evaluateAll(ns => ns.map(el => el.textContent?.trim()))))
    for (let i = 0; i < n; i++) {
      await rows.nth(i).screenshot({ path: `${OUT}/row-${i + 1}-applied-${scheme}.png` })
    }
    // The second tier: the asks a crewmate cannot act on itself (approval mode,
    // model and effort, tools) are the same change cards the profile editor and
    // the capabilities view write, in the order the crewmate raised them, so the
    // user reads "Approval mode  Trust -> Normal" in the picker's words, under the
    // crewmate's own name, and nothing changes until the card is pressed.
    const cards = page.locator(CARD)
    const cardCount = await cards.count()
    for (let i = 0; i < cardCount; i++) {
      const card = cards.nth(i)
      console.log(scheme, `card ${i + 1}:`, JSON.stringify((await card.evaluate(el => el.textContent?.trim()))?.slice(0, 240)))
      await card.screenshot({ path: `${OUT}/card-${i + 1}-${scheme}.png` })
    }
  }

  if (!(await page.locator(ROW).count())) {
    console.log('no rows on screen (a cards-only run); skipping the undo frames')
    console.log(`wrote ${OUT}`)
    await browser.close()
    process.exit(0)
  }
  // The two refusals the pod cannot reach on demand. Only the undo request is
  // stubbed; the row, the thread and the rest of the page are the live pod's.
  // 410: the gateway holds no record any more (a restart, or past the newest
  // 256). 409 stale_undo: the field was edited again since the change.
  await setTheme(page, 'light')
  const refuse = (status, code, error) => route => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify({ error, code }),
  })
  await page.route(/\/self-changes\/[^/]+\/undo$/, refuse(410, 'undo_unavailable', 'this change can no longer be undone here'))
  await page.locator(UNDO).nth(0).click()
  await page.waitForSelector('[data-testid="self-change-undo-error"]', { timeout: 15_000 })
  await page.waitForTimeout(500)
  await page.locator(ROW).nth(0).screenshot({ path: `${OUT}/row-undo-unavailable-light.png` })
  await page.unroute(/\/self-changes\/[^/]+\/undo$/)
  await page.route(/\/self-changes\/[^/]+\/undo$/, refuse(409, 'stale_undo', "the mate's description was changed again since; nothing was undone"))
  await page.locator(UNDO).nth(0).click()
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="self-change-undo-error"]').length >= 2, null, { timeout: 15_000 })
  await page.waitForTimeout(500)
  await page.locator(ROW).nth(1).screenshot({ path: `${OUT}/row-undo-stale-light.png` })
  await page.unroute(/\/self-changes\/[^/]+\/undo$/)

  // Undo once for real, on the last row, and shoot the row that now reads "undone".
  await page.locator(UNDO).last().click()
  await page.waitForSelector(`${ROW}[data-self-status="undone"]`, { timeout: 15_000 })
  await page.waitForTimeout(800)
  await page.screenshot({ path: `${OUT}/thread-after-undo-light.png` })
  await page.locator(`${ROW}[data-self-status="undone"]`).last().screenshot({ path: `${OUT}/row-undone-light.png` })
  console.log(`wrote ${OUT}`)
} finally {
  await browser.close()
}
