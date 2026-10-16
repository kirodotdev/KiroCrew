/**
 * Capture harness for issue #17111: "sidebar: label the pinned band while a tag
 * filter or status chip narrows the list".
 *
 * Scenes × two themes × two widths: before has 2 scenes × 2 × 2 = 8 frames,
 * after has 4 scenes × 2 × 2 = 16 frames.
 *
 * Usage:
 *   node capture/shoot-sidebar-pinned-band-caption.mjs [outDir] [port]
 *
 * Environment:
 *   CHROMIUM_PATH  — override the Chromium executable (default: auto-detect)
 *   BEFORE_FIX     — if set, only captures the "before" scenes for the Repro section.
 *
 * Note: The sidebar renders sessions inside a deeply nested scrollable lane.
 * We locate the containing lane, scroll the sessions into view, and screenshot
 * the sidebar region from just above the sessions.
 */
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/pinned-band-caption-shots'
const PORT = Number(process.argv[3] || 5199)
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
  || undefined
const BEFORE_FIX = !!process.env.BEFORE_FIX

mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
console.log(`vite up on ${PORT}`)

const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

async function shoot(theme, width, scene, prefix) {
  // Tall viewport and wide enough for the sidebar.
  const ctx = await browser.newContext({ viewport: { width: width + 100, height: 1000 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:${PORT}/capture/sidebar-pinned-band-caption.html?theme=${theme}&scene=${scene}`)
  await page.waitForSelector('[data-capture-ready]', { timeout: 20000 })
  await page.waitForSelector('[data-session-row]', { timeout: 20000 })
  await page.waitForTimeout(400)

  // Scroll the lane so the sessions are near the top of the scrollable area.
  await page.evaluate(() => {
    const lane = document.querySelector('[data-testid="tree-view-lane"]')
    const row = document.querySelector('[data-session-row]')
    if (!lane || !row) return
    const rowOffsetInLane = row.getBoundingClientRect().top - lane.getBoundingClientRect().top + lane.scrollTop
    // Put the row near the top of the visible lane area (40px from lane top).
    lane.scrollTop = Math.max(0, rowOffsetInLane - 40)
  })
  await page.waitForTimeout(400)

  // Locate the tree-view-lane and screenshot its top portion where sessions appear.
  const laneEl = page.locator('[data-testid="tree-view-lane"]')
  // Use element screenshot which scrolls to the element and crops it.
  // Limit to first 400px of the lane by taking the lane's bounding box and cropping.
  const laneBB = await laneEl.boundingBox()
  if (!laneBB) {
    // Fallback: screenshot the full page.
    await page.screenshot({ path: join(OUT, `${prefix}.png`) })
  } else {
    const cropHeight = Math.min(400, 1000 - laneBB.y)
    await page.screenshot({
      path: join(OUT, `${prefix}.png`),
      clip: {
        x: laneBB.x,
        y: laneBB.y,
        width: Math.min(laneBB.width, width),
        height: cropHeight,
      },
    })
  }
  console.log(`  shot ${prefix}.png`)
  await ctx.close()
}

const WIDTHS = [[820, 'desktop'], [390, 'mobile']]
const THEMES = ['dark', 'light']

if (BEFORE_FIX) {
  for (const [width, wname] of WIDTHS) {
    for (const theme of THEMES) {
      await shoot(theme, width, 'no-filter',       `before-${theme}-${wname}-no-filter`)
      await shoot(theme, width, 'chip-only-pinned', `before-${theme}-${wname}-chip-only`)
    }
  }
} else {
  const SCENES = [
    ['no-filter',          'no-filter'],
    ['chip-only-pinned',   'chip-only-pinned'],
    ['chip-with-unpinned', 'chip-with-unpinned'],
    ['tag-filter',         'tag-filter'],
  ]
  for (const [width, wname] of WIDTHS) {
    for (const theme of THEMES) {
      for (const [scene, sname] of SCENES) {
        await shoot(theme, width, scene, `after-${theme}-${wname}-${sname}`)
      }
    }
  }
}

await browser.close()
await server.close()
console.log(`\nFrames written to ${OUT}`)
