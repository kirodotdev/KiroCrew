/* Capture QueueStack reorder screenshots from the vite-served harness.
 * Usage: node temp-screenshots/queue-reorder/capture.mjs <baseUrl>
 */
import { chromium } from 'playwright'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const page1 = `${base}/temp-screenshots/queue-reorder/harness.html`
const outDir = 'temp-screenshots/queue-reorder'

const browser = await chromium.launch({
  // Reuse the newest cached browser instead of downloading the pinned one -
  // this script is an ephemeral harness, not part of CI.
  executablePath: process.env.HOME + '/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell',
})
const ctx = await browser.newContext({ viewport: { width: 900, height: 520 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()

async function shot(theme, name, prepare) {
  await page.goto(`${page1}?theme=${theme}`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.queue-card', { timeout: 10000 })
  if (prepare) await prepare()
  await page.waitForTimeout(900) // let framer-motion springs settle
  await page.screenshot({ path: `${outDir}/${name}.png` })
  console.log(`captured ${name}`)
}

/** Expand the stack via a programmatic DOM click (framer-motion's animated
 *  container fails Playwright's actionability check; behavior is covered by
 *  the component tests - here we only need the rendered state). */
const expand = () => page.evaluate(() => {
  const el = document.querySelector('[role="button"][aria-expanded]')
  el?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
})

// Collapsed stack: arrows must NOT show.
await shot('dark', 'collapsed-dark', null)

// Expanded stack: arrows on every card, boundaries disabled.
for (const theme of ['dark', 'light']) {
  await shot(theme, `expanded-${theme}`, expand)
}

await browser.close()
