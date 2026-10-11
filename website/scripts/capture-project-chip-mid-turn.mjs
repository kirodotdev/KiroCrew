/**
 * The composer's project chip while a response is running (#7263).
 *
 * Drives website/capture/project-chip-mid-turn.html, which mounts the REAL
 * ChatInput with isRunning. Every frame asserts before writing: the project
 * chip is enabled and opens the picker on click, its accessible name carries
 * the next-response line, and the agent and model chips are still disabled.
 * The chip is hovered for the shot so its enabled hover state is visible.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6833 --strictPort   # in another shell
 *   node scripts/capture-project-chip-mid-turn.mjs http://127.0.0.1:6833 ../temp-screenshots/project-chip-mid-turn
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6833'
const OUT = process.argv[3] || '../temp-screenshots/project-chip-mid-turn'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = false

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 760, height: 420 }, deviceScaleFactor: 2 })
  // Gateway-free: answer every API call the mounted ChatInput makes.
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/project-chip-mid-turn.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  const project = page.locator('[data-capture-root] button[aria-label^="Project: "]')
  const agent = page.getByRole('button', { name: /switch agents/ })
  const model = page.getByRole('button', { name: /switch model/ })
  await project.waitFor()
  await project.click()
  const label = (await project.getAttribute('aria-label')) || ''
  const ok =
    (await project.isEnabled()) &&
    label.includes('Changes apply from the next response.') &&
    (await agent.isDisabled()) &&
    (await model.isDisabled()) &&
    (await page.locator('[data-capture-label]').innerText()).includes('Picker opened: 1')
  console.log(`${theme}: ${ok ? 'OK' : 'MISMATCH'} label=${JSON.stringify(label)}`)
  if (!ok) failed = true
  else {
    await project.hover()
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/project-chip-mid-turn-${theme}.png` })
  }
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
