/**
 * Screenshots for the approval-mode discoverability work (A1 hint row,
 * A2 spotlight open, B2 threshold nudge).
 *
 * Drives the isolated capture entry (website/capture/approval-mode-discover.html),
 * which mounts the REAL ChatInput with a seeded pending approval. Every frame
 * asserts its state before writing, so a frame cannot document the wrong state:
 *   01-hint       the approval bar carries the "Adjust approval mode" hint
 *   02-spotlight  clicking that REAL hint opened the mode picker and the
 *                 trigger wears the spotlight ring
 *   03-nudge      three real "Allow once" clicks (API answered by route
 *                 interception, next approval injected per round) surfaced
 *                 the one-time callout anchored to the picker
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort   # in another shell
 *   node scripts/capture-approval-mode-discover.mjs http://127.0.0.1:6824 ../temp-screenshots/approval-mode-discover
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/approval-mode-discover'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = false

async function newPage(theme) {
  const page = await browser.newPage({ viewport: { width: 720, height: 560 }, deviceScaleFactor: 2 })
  // Gateway-free: answer every REAL API call the mounted ChatInput makes.
  // Predicate on the pathname — a glob like **/api/** would also swallow
  // vite-served source modules such as /src/api/client.ts and break boot.
  // Array-shaped endpoints must answer [] ({} crashes their .map consumers).
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/approval-mode-discover.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Allow once').waitFor()
  return page
}

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

for (const theme of ['dark', 'light']) {
  // ---- 01: A1 hint row on the approval bar
  let page = await newPage(theme)
  const hintQ = await page.getByText('Tired of confirming every step?').count()
  const hintLink = await page.getByRole('button', { name: /Adjust approval mode/ }).count()
  if (check(`01-hint-${theme}`, hintQ === 1 && hintLink === 1, `q=${hintQ} link=${hintLink}`)) {
    await page.screenshot({ path: `${OUT}/01-hint-${theme}.png` })
  }

  // ---- 02: A2 — clicking the real hint opens + spotlights the picker
  await page.getByRole('button', { name: /Adjust approval mode/ }).click()
  await page.getByRole('menuitem').first().waitFor({ timeout: 5000 }).catch(() => {})
  const menuItems = await page.getByRole('menuitem').count()
  const trigger = page.getByLabel('Approval mode: Normal')
  const ringed = ((await trigger.getAttribute('class')) || '').includes('ring-accent')
  if (check(`02-spotlight-${theme}`, menuItems >= 4 && ringed, `items=${menuItems} ring=${ringed}`)) {
    await page.screenshot({ path: `${OUT}/02-spotlight-${theme}.png` })
  }
  await page.close()

  // ---- 03: B2 — three real approvals surface the nudge
  page = await newPage(theme)
  for (let i = 1; i <= 3; i++) {
    await page.getByText('Allow once').click()
    // The bar unmounts once the decision resolves; re-arm the next approval
    // through the same dispatch the WS layer makes.
    await page.getByText('Allow once').waitFor({ state: 'detached' })
    if (i < 3) {
      await page.evaluate(n => window.__capture.nextApproval(n), i + 1)
      await page.getByText('Allow once').waitFor()
    }
  }
  const dialog = page.getByRole('dialog', { name: 'Want fewer approval prompts?' })
  await dialog.waitFor({ timeout: 3000 }).catch(() => {})
  const nudgeUp = await dialog.count()
  const seeOptions = await page.getByText('See options').count()
  if (check(`03-nudge-${theme}`, nudgeUp === 1 && seeOptions === 1, `dialog=${nudgeUp} cta=${seeOptions}`)) {
    await page.screenshot({ path: `${OUT}/03-nudge-${theme}.png` })
  }
  // Outside-click semantics (real Radix, real browser — jsdom cannot drive
  // DismissableLayer): clicking the transcript hides the callout for the
  // SITTING, so the next approval in the same session re-fires it.
  await page.mouse.click(700, 200)
  await dialog.waitFor({ state: 'detached', timeout: 3000 }).catch(() => {})
  const hidden = (await dialog.count()) === 0
  await page.evaluate(n => window.__capture.nextApproval(n), 4)
  await page.getByText('Allow once').waitFor()
  await page.getByText('Allow once').click()
  await page.getByText('Allow once').waitFor({ state: 'detached' })
  await dialog.waitFor({ timeout: 3000 }).catch(() => {})
  const refired = (await dialog.count()) === 1
  check(`03b-outside-hide-refire-${theme}`, hidden && refired, `hidden=${hidden} refired=${refired}`)
  await page.close()
}

// ---- 04: narrow-viewport clamp — the callout must stay fully on a 390px screen
{
  const page = await browser.newPage({ viewport: { width: 390, height: 700 }, deviceScaleFactor: 2 })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/approval-mode-discover.html?theme=dark`)
  await page.getByText('Allow once').waitFor()
  for (let i = 1; i <= 3; i++) {
    await page.getByText('Allow once').click()
    await page.getByText('Allow once').waitFor({ state: 'detached' })
    if (i < 3) {
      await page.evaluate(n => window.__capture.nextApproval(n), i + 1)
      await page.getByText('Allow once').waitFor()
    }
  }
  const dlg = page.getByRole('dialog', { name: 'Want fewer approval prompts?' })
  await dlg.waitFor({ timeout: 3000 }).catch(() => {})
  const box = await dlg.boundingBox()
  const inView = !!box && box.x >= 0 && box.x + box.width <= 390
  if (check('04-nudge-mobile-clamp', !!box && inView, `box=${JSON.stringify(box)}`)) {
    await page.screenshot({ path: `${OUT}/04-nudge-mobile-clamp.png` })
  }
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
