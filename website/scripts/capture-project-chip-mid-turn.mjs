/**
 * The composer's project chip while a response is running (#7263).
 *
 * Drives website/capture/project-chip-mid-turn.html, which mounts the REAL
 * ChatInput with isRunning. Every frame asserts before writing: the project
 * chip is enabled and opens the picker on click, its accessible name carries
 * the next-response line, and the agent and model chips are still disabled.
 * A second frame per theme is taken after a mid-turn pick.
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
  const before = (await project.getAttribute('aria-label')) || ''
  const okBefore =
    (await project.isEnabled()) &&
    before.includes('Project: /home/user/work/KiroCrew') && // brand-ok: KiroCrew is the fixture folder name
    before.includes('Your next message runs in the project you pick. This reply finishes in KiroCrew.\nLinks in this reply will open in the project you pick.') && // brand-ok: fixture folder name
    (await agent.isDisabled()) &&
    (await model.isDisabled())
  console.log(`${theme} before: ${okBefore ? 'OK' : 'MISMATCH'} label=${JSON.stringify(before)}`)
  if (!okBefore) failed = true
  else {
    await project.hover()
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/project-chip-mid-turn-${theme}.png` })
  }
  // The click stands in for choosing OtherRepo in the picker mid-reply.
  await project.click()
  await page.locator('[data-capture-label]', { hasText: 'Picker opened: 1' }).waitFor()
  await page.locator('[data-capture-label]', { hasText: 'Project: /home/user/work/OtherRepo' }).waitFor()
  const after = (await project.getAttribute('aria-label')) || ''
  const okAfter =
    (await project.isEnabled()) &&
    after.includes('Project: /home/user/work/OtherRepo') &&
    after.includes('This reply finishes in KiroCrew.') && // brand-ok: fixture folder name
    (await page.locator('[data-capture-label]').innerText()).includes(after.split('\n')[0])
  console.log(`${theme} after pick: ${okAfter ? 'OK' : 'MISMATCH'} label=${JSON.stringify(after)}`)
  if (!okAfter) failed = true
  else {
    await project.hover()
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/project-chip-mid-turn-after-pick-${theme}.png` })
  }
  await page.close()

  // Sub-agents attached: the chip is disabled and says why.
  const lockedPage = await browser.newPage({ viewport: { width: 760, height: 420 }, deviceScaleFactor: 2 })
  await lockedPage.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const isList = /commands|skills|agents|sessions|files|history|models/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await lockedPage.goto(`${BASE}/capture/project-chip-mid-turn.html?theme=${theme}&locked=1`)
  await lockedPage.waitForSelector('[data-capture-root]')
  const lockedChip = lockedPage.locator('[data-capture-root] button[aria-label^="Project: "]')
  await lockedChip.waitFor()
  const lockedLabel = (await lockedChip.getAttribute('aria-label')) || ''
  const okLocked =
    (await lockedChip.isDisabled()) &&
    lockedLabel.includes("The project can't change while sub-agents are running.") &&
    !lockedLabel.includes('This reply finishes') &&
    (await lockedPage.locator('[data-capture-label]').innerText()).includes("The project can't change while sub-agents are running.")
  console.log(`${theme} locked: ${okLocked ? 'OK' : 'MISMATCH'} label=${JSON.stringify(lockedLabel)}`)
  if (!okLocked) failed = true
  else await lockedPage.locator('[data-capture-root]').screenshot({ path: `${OUT}/project-chip-locked-subagents-${theme}.png` })
  await lockedPage.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
