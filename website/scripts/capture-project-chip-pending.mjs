/**
 * The project chip's visible pending badge after a mid-reply pick (#20350).
 *
 * Drives website/capture/project-chip-mid-turn.html, which mounts the REAL
 * ChatInput with isRunning, with `bare=1` so the frame holds product UI only
 * (the fixture's printed accessible name is left out: a touch user never sees
 * it). Every frame asserts before writing: before a pick the chip carries no
 * badge; after a pick the badge is visible WITHOUT any hover (the shot is
 * taken with the pointer away from the chip, as a touch user would see it)
 * and the chip's accessible name names the pending switch and the project the
 * running reply finishes in. Three after-pick states are captured per theme:
 * the full shelf, a compact shelf (the badge is a clock glyph), and the chip
 * locked by attached sub-agents.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6833 --strictPort   # in another shell
 *   node scripts/capture-project-chip-pending.mjs http://127.0.0.1:6833 ../temp-screenshots/project-chip-pending
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6833'
const OUT = process.argv[3] || '../temp-screenshots/project-chip-pending'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = false

const VARIANTS = [
  { name: 'pending', query: '', viewport: 760, text: 'Starts next message', locked: false },
  { name: 'pending-compact', query: '&width=360', viewport: 400, text: '', locked: false },
  { name: 'pending-locked', query: '&lockAfterPick=1', viewport: 760, text: 'Starts next message', locked: true },
]

for (const theme of ['dark', 'light']) {
  for (const v of VARIANTS) {
    const page = await browser.newPage({ viewport: { width: v.viewport, height: 420 }, deviceScaleFactor: 2, hasTouch: true })
    // Gateway-free: answer every API call the mounted ChatInput makes.
    await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
      const path = new URL(route.request().url()).pathname
      const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
      return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
    })
    // A cold dev server can spend well over 30s on its first dependency scan.
    await page.goto(`${BASE}/capture/project-chip-mid-turn.html?theme=${theme}&bare=1${v.query}`, { timeout: 180000 })
    await page.waitForSelector('[data-capture-root]', { timeout: 180000 })
    const project = page.getByTestId('composer-project-chip')
    const badge = page.getByTestId('composer-project-pending')
    await project.waitFor()
    const okBefore = (await project.isEnabled()) && (await badge.count()) === 0
    console.log(`${theme} ${v.name} before: ${okBefore ? 'OK' : 'MISMATCH'}`)
    if (!okBefore) failed = true
    else if (v.name === 'pending') await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/project-chip-before-pick-${theme}.png` })
    // A tap stands in for choosing OtherRepo in the picker mid-reply.
    await project.tap()
    await page.locator('[data-capture-label]', { hasText: 'Project: /home/user/work/OtherRepo' }).waitFor({ state: 'attached' })
    await page.mouse.move(0, 0)
    const after = (await project.getAttribute('aria-label')) || ''
    const badgeText = (await badge.textContent() || '').trim()
    const okAfter =
      (await badge.isVisible()) &&
      badgeText === v.text &&
      (v.text || (await badge.evaluate(el => el.tagName.toLowerCase() === 'svg'))) &&
      (await project.isDisabled()) === v.locked &&
      (await page.locator('[data-capture-label]').isHidden()) &&
      after.includes('Switching to OtherRepo from your next message.') &&
      (v.locked ? after.includes("The project can't change while sub-agents are running.") : after.includes('This reply finishes in KiroCrew.')) // brand-ok: KiroCrew is the fixture folder name
    console.log(`${theme} ${v.name} after pick: ${okAfter ? 'OK' : 'MISMATCH'} badge=${JSON.stringify(badgeText)} label=${JSON.stringify(after)}`)
    if (!okAfter) failed = true
    else await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/project-chip-${v.name}-${theme}.png` })
    await page.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
