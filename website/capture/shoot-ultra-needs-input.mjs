/**
 * Screenshot harness for the Ultra Mode Board's Needs your input.
 *
 * Serves the isolated capture entry (capture/ultra-needs-input.tsx) with a
 * programmatic Vite dev server on loopback and photographs the REAL
 * UltraCrewPanel at side-panel width: the Status view, then the same view after
 * pressing one offered answer, which fills the stand-in composer. The reads are
 * stubbed inside the entry, so no gateway / kiro-cli / token is involved.
 *
 * Usage (from website/): node capture/shoot-ultra-needs-input.mjs [outDir]
 * SHOT_PREFIX names the files (e.g. `before` on main, `after` on the branch).
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/ultra-needs-input')
mkdirSync(OUT, { recursive: true })
const PREFIX = process.env.SHOT_PREFIX || 'shot'

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 420, height: 800 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/ultra-needs-input.html?theme=${theme}`)
  await page.locator('[data-testid="ultra-milestone"]').first().waitFor({ timeout: 30000 })
  await page.waitForTimeout(600)
  const frame = page.locator('[data-testid="capture-frame"]')
  await frame.screenshot({ path: `${OUT}/${PREFIX}-${theme}.png` })
  console.log('wrote', `${OUT}/${PREFIX}-${theme}.png`)
  const answers = page.locator('[data-testid="ultra-person-ask-answers"]')
  if (theme === 'dark' && await answers.count()) {
    await answers.click()
    await page.getByRole('menuitem', { name: 'Oolong', exact: true }).waitFor()
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${PREFIX}-menu.png`, clip: await frame.boundingBox() })
    console.log('wrote', `${OUT}/${PREFIX}-menu.png`)
    await page.getByRole('menuitem', { name: 'Oolong', exact: true }).click()
    await page.waitForTimeout(300)
    await frame.screenshot({ path: `${OUT}/${PREFIX}-pressed.png` })
    console.log('wrote', `${OUT}/${PREFIX}-pressed.png`)
  }
}

// A crew member on screen: the row offers to open the main chat instead of buttons.
await page.goto(`${base}/capture/ultra-needs-input.html?theme=dark&viewing=w-tea`)
const openMain = page.locator('[data-testid="ultra-person-ask-open-main"]')
if (await openMain.count() || await openMain.waitFor({ timeout: 15000 }).then(() => true, () => false)) {
  await page.waitForTimeout(400)
  await page.locator('[data-testid="capture-frame"]').screenshot({ path: `${OUT}/${PREFIX}-member.png` })
  console.log('wrote', `${OUT}/${PREFIX}-member.png`)
}

await browser.close()
await server.close()
