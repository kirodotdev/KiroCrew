// Screenshots for the dock terminal's Close all terminals: the More actions
// menu that lists the tabs and ends with Close all, then the confirm it
// raises, at a desktop and a phone width.
//   node capture/shoot-terminal-close-all.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/terminal-close-all-shots'
const PORT = Number(process.argv[3] || 5180)
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

async function shoot(theme, width, prefix, mobile = false) {
  const ctx = await browser.newContext({ viewport: { width, height: 640 }, reducedMotion: 'reduce', ...(mobile ? { hasTouch: true, isMobile: true } : {}) })
  const page = await ctx.newPage()
  page.on('pageerror', (e) => console.error('pageerror', e.message))
  await page.goto(`http://127.0.0.1:${PORT}/capture/terminal-close-all.html?theme=${theme}`)
  await page.getByRole('button', { name: 'More actions' }).waitFor({ timeout: 30000 })
  await page.waitForTimeout(800)
  await page.getByTestId('terminal-more-trigger').click()
  await page.getByTestId('terminal-tabs-close-all').waitFor()
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `${prefix}-1-menu.png`) })
  await page.getByTestId('terminal-tabs-close-all').click()
  await page.getByRole('dialog').waitFor()
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `${prefix}-2-confirm.png`) })
  console.log('shot', prefix)
  await ctx.close()
}

await shoot('light', 900, 'desktop')
await shoot('light', 390, 'phone', true)
await browser.close()
await server.close()
