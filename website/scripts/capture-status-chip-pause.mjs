/**
 * Status chips narrowing, hovered, and with Pinned paused; then the filter
 * menu with Pinned marked paused, reading "Pause all" and then "Resume all".
 * Usage: node scripts/<this file> [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/status-chip-pause'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const slot = (key, title, running, ageS) => ({
  key, title, running, messages: 3, agent: 'kirocrew', modified: now - ageS,
  last_ts: new Date(Date.now() - ageS * 1000).toISOString(), folder_id: '',
})
const slots = [
  slot('c-run-1', 'Build the release bundle', true, 60),
  slot('c-run-2', 'Deploy to staging', true, 120),
  slot('c-idle-1', 'Review CR feedback', false, 300),
  slot('c-idle-2', 'Daily worklog', false, 900),
]

const FILTER_BTN = 'button[aria-label^="Sort and filter sessions"]'
const FRAMES = [
  { name: 'shot-01-all-narrowing', stored: ['1', '1', '1'] },
  { name: 'shot-02-hover-unread', stored: ['1', '1', '1'], hover: 'unread', expectHint: 'pause' },
  { name: 'shot-03-pinned-paused', stored: ['1', '1', '2'], expectState: 'paused' },
  { name: 'shot-04-hover-paused-pinned', stored: ['1', '1', '2'], hover: 'pinned', expectHint: 'resume' },
  { name: 'shot-05-menu-pause-all', stored: ['1', '1', '2'], menu: 'Pause all filters' },
  { name: 'shot-06-menu-resume-all', stored: ['2', '2', '2'], menu: 'Resume all filters' },
]

function union(a, b) {
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y)
  return { x, y, width: Math.max(a.x + a.width, b.x + b.width) - x, height: Math.max(a.y + a.height, b.y + b.height) - y }
}

async function run() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const frame of FRAMES) {
      const page = await browser.newPage()
      await page.setViewportSize({ width: 1280, height: 800 })
      logPageProblems(page)
      const [unread, running, pinned] = frame.stored
      await stubDashboardApi(page, {
        slots,
        theme: 'dark',
        localStorageEntries: {
          'mc-onboarded': '1',
          'mc-sidebar-lane': 'flat',
          'mc-session-stale-collapse-ms': '0',
          'mc-session-unread-only': unread,
          'mc-session-running-only': running,
          'mc-session-pinned-only': pinned,
        },
      })
      await page.goto(`${base}/chat`)
      const pinnedChip = page.locator('[data-testid="filter-chip-pinned"]')
      await pinnedChip.waitFor({ timeout: 15_000 })
      await page.waitForTimeout(500)
      if (frame.hover) {
        const toggle = page.locator(`[data-testid="filter-chip-${frame.hover}-toggle"]`)
        await toggle.hover()
        await page.waitForTimeout(200)
        const after = await toggle.evaluate(el => { const s = getComputedStyle(el, '::after'); return { display: s.display, content: s.content } })
        if (after.display === 'none' || !after.content.includes(frame.expectHint)) throw new Error(`${frame.name}: hover hint not shown ${JSON.stringify(after)}`)
      }
      if (frame.expectState) {
        const state = page.locator('[data-testid="filter-chip-pinned-toggle"] [data-chip-state]')
        if (!(await state.isVisible()) || !(await state.innerText()).includes(frame.expectState)) throw new Error(`${frame.name}: paused state not shown`)
      }
      const field = await page.locator('[data-testid="search-field-glass"]').first().boundingBox()
      const row = await pinnedChip.locator('xpath=..').boundingBox()
      let box = union(field, row)
      if (frame.menu) {
        await page.click(FILTER_BTN)
        const item = page.locator('[data-testid="filter-pause-all"]')
        await item.waitFor({ timeout: 5_000 })
        await page.waitForTimeout(300)
        if (!(await item.innerText()).includes(frame.menu)) throw new Error(`${frame.name}: menu row reads ${JSON.stringify(await item.innerText())}`)
        const pinnedRow = page.getByRole('menuitem').filter({ hasText: /^Pinned/ }).first()
        if (!/· paused/.test(await pinnedRow.innerText())) throw new Error(`${frame.name}: paused Pinned menu row is not marked`)
        box = union(box, await page.locator('[role="menu"]').boundingBox())
      }
      await page.screenshot({
        path: join(OUT, `${frame.name}.png`),
        clip: { x: Math.floor(box.x - 12), y: Math.floor(box.y - 8), width: Math.ceil(box.width + 24), height: Math.ceil(box.height + 16) },
      })
      console.log(`  -> ${frame.name}.png`)
      await page.close()
    }
  } finally {
    await browser.close()
    srv.close()
  }
}

run().catch(err => { console.error(err); process.exit(1) })
