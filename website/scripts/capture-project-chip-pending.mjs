/**
 * The project chip's visible pending badge after a mid-reply pick (#20350).
 *
 * Drives website/capture/project-chip-mid-turn.html, which mounts the REAL
 * ChatInput with isRunning. Every frame asserts before writing: before a pick
 * the chip carries no badge; after a pick the badge is visible WITHOUT any
 * hover (the shot is taken with the pointer away from the chip, as a touch
 * user would see it) and the chip's accessible name names the pending switch
 * and the project the running reply finishes in.
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

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 760, height: 420 }, deviceScaleFactor: 2, hasTouch: true })
  // Gateway-free: answer every API call the mounted ChatInput makes.
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  // A cold dev server can spend well over 30s on its first dependency scan.
  await page.goto(`${BASE}/capture/project-chip-mid-turn.html?theme=${theme}`, { timeout: 180000 })
  await page.waitForSelector('[data-capture-root]', { timeout: 180000 })
  const project = page.getByTestId('composer-project-chip')
  const badge = page.getByTestId('composer-project-pending')
  await project.waitFor()
  const okBefore = (await project.isEnabled()) && (await badge.count()) === 0
  console.log(`${theme} before: ${okBefore ? 'OK' : 'MISMATCH'}`)
  if (!okBefore) failed = true
  else await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/project-chip-before-pick-${theme}.png` })
  // A tap stands in for choosing OtherRepo in the picker mid-reply.
  await project.tap()
  await page.locator('[data-capture-label]', { hasText: 'Project: /home/user/work/OtherRepo' }).waitFor()
  await page.mouse.move(0, 0)
  const after = (await project.getAttribute('aria-label')) || ''
  const okAfter =
    (await badge.isVisible()) &&
    (await badge.innerText()).trim() === 'Next message' &&
    after.includes('Switching to OtherRepo from your next message.') &&
    after.includes('This reply finishes in KiroCrew.') // brand-ok: KiroCrew is the fixture folder name
  console.log(`${theme} after pick: ${okAfter ? 'OK' : 'MISMATCH'} label=${JSON.stringify(after)}`)
  if (!okAfter) failed = true
  else await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/project-chip-pending-${theme}.png` })
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
