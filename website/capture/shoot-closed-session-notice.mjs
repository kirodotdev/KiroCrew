// Screenshots for the closed-session inline notice on a `?sid=` link:
// the notice after a click on the link, the same for the backticked key, the
// same at a phone width, and the page banner a Try again that meets a deleted
// session raises.
//   node capture/shoot-closed-session-notice.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/closed-session-notice-shots'
const PORT = Number(process.argv[3] || 5178)
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
console.log(`vite up on ${PORT}`)

const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

async function shoot(theme, width, prefix) {
  const ctx = await browser.newContext({ viewport: { width, height: 420 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:${PORT}/capture/closed-session-notice.html?theme=${theme}`)
  const link = page.locator('[data-bubble] a')
  await link.waitFor({ timeout: 15000 })
  // The failed background probe says nothing until the click; wait for it to
  // have answered (the link is live again, not muted) so the click reaches it.
  await page.waitForFunction(() => {
    const a = document.querySelector('[data-bubble] a')
    return !!a && !a.classList.contains('text-muted')
  }, null, { timeout: 15000 })
  await page.waitForTimeout(200)
  await page.screenshot({ path: join(OUT, `${prefix}-0-before-click.png`) })
  await link.click()
  await page.waitForSelector('[data-closed-session-notice]', { timeout: 15000 })
  await page.waitForTimeout(200)
  await page.screenshot({ path: join(OUT, `${prefix}-1-notice.png`) })
  // The backticked key names the same session, so its click shows its own notice.
  await page.locator('[data-bubble] code[data-session-key]').click()
  await page.waitForFunction(() => document.querySelectorAll('[data-closed-session-notice]').length === 2, null, { timeout: 15000 })
  await page.waitForTimeout(200)
  await page.screenshot({ path: join(OUT, `${prefix}-1b-notice-chip.png`) })
  await page.getByRole('button', { name: 'Try again' }).first().click()
  await page.waitForSelector('[data-banner]', { timeout: 15000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `${prefix}-2-retry-deleted-banner.png`) })
  console.log('shot', prefix)
  await ctx.close()
}

await shoot('dark', 820, '01-dark-wide')
await shoot('dark', 420, '02-dark-narrow')
await shoot('light', 820, '03-light-wide')

await browser.close()
await server.close()
console.log('done')
