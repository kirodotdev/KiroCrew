/**
 * Screenshot harness for the memory-mode switch lock.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call through `stubDashboardApi`. The one
 * scene-specific stub is `POST /api/chat/slots`, the replacement session's
 * create: it is left unanswered, so the switch stays in flight and the lock
 * can be photographed. Nothing else is faked.
 *
 * Shots, at a desktop and a narrow viewport:
 *   01  the welcome screen mid-switch, with a draft in the composer;
 *   02  a second session the user opened while the switch still runs;
 *   03  an optimize in flight, with the chip clicked: it stays shut and says
 *       to wait. `POST /api/optimizer/optimize` is held open the same way.
 *
 * Usage: node scripts/capture-memory-switch-lock.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/16004-memory-switch-lock'
mkdirSync(OUT, { recursive: true })

const SLOTS = [
  { key: 'chat-a', title: 'Release notes', messages: 0, running: false, agent: 'kirocrew', mode: '' },
  { key: 'chat-b', title: 'Flaky test triage', messages: 0, running: false, agent: 'kirocrew', mode: '' },
]
const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'narrow', width: 390, height: 844 },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

for (const vp of VIEWPORTS) {
  const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots: SLOTS,
    localStorageEntries: { 'mc-active-slot': 'chat-a', 'mc-lang': 'en' },
    // Hold the replacement's create open: never fulfilled, so the switch stays in flight.
    extra: async (path, route) => path === '/api/chat/slots' && route.request().method() === 'POST',
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })

  const composer = page.getByLabel('Message input').last()
  await composer.waitFor({ state: 'visible', timeout: 15000 })
  await composer.fill('Summarise the release notes for the 0.5 cut, grouped by area')
  await page.getByTestId('memory-mode-chip').click()
  await page.getByRole('button', { name: /^Incognito/ }).click()
  await page.getByTestId('memory-mode-chip').filter({ hasText: 'Switching memory mode' })
    .waitFor({ state: 'visible', timeout: 5000 })

  const welcome = join(OUT, `${vp.name}-01-welcome-mid-switch.png`)
  await page.screenshot({ path: welcome })
  console.log('wrote', welcome)

  // Open the other session while the switch still runs: its composer is locked too.
  const row = page.getByText('Flaky test triage', { exact: true }).first()
  // The narrow layout keeps the session list behind its toggle.
  if (!(await row.isVisible())) await page.locator('button').first().click()
  const picked = await row.elementHandle()
  await row.click()
  // The narrow drawer closes itself on a pick; wait until it has slid away.
  if (vp.name === 'narrow') await picked.waitForElementState('hidden', { timeout: 5000 })
  await page.getByLabel('Message input').last().and(page.locator('[aria-disabled="true"]'))
    .waitFor({ state: 'visible', timeout: 5000 })
  await page.getByLabel('Message input').last().and(page.locator('[placeholder="Switching memory mode…"]'))
    .waitFor({ state: 'visible', timeout: 5000 })
  const second = join(OUT, `${vp.name}-02-second-session-mid-switch.png`)
  await page.screenshot({ path: second })
  console.log('wrote', second)

  await context.close()

  // An optimize in flight: the chip refuses a switch and says why.
  const optContext = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 2 })
  const optPage = await optContext.newPage()
  await stubDashboardApi(optPage, {
    slots: SLOTS,
    localStorageEntries: { 'mc-active-slot': 'chat-a', 'mc-lang': 'en' },
    extra: async (path, route) => path === '/api/optimizer/optimize' && route.request().method() === 'POST',
  })
  await optPage.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  const optComposer = optPage.getByLabel('Message input').last()
  await optComposer.waitFor({ state: 'visible', timeout: 15000 })
  await optComposer.fill('summarise release notes 0.5 by area')
  await optPage.getByRole('button', { name: 'Optimize prompt' }).click()
  const chip = optPage.getByTestId('memory-mode-chip').filter({ hasText: 'Wait for the prompt optimizer to finish' })
  await chip.waitFor({ state: 'visible', timeout: 5000 })
  await chip.click({ force: true })
  await chip.hover({ force: true })
  const optimizing = join(OUT, `${vp.name}-03-chip-during-optimize.png`)
  await optPage.screenshot({ path: optimizing })
  console.log('wrote', optimizing)
  await optContext.close()
}

await browser.close()
srv.close()
