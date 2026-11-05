// Shoot the enterprise sign-in-only screen: the Company SSO form a signed-out
// enterprise gateway shows, and the wait after sign-in. Run from website/:
//   node capture/shoot-enterprise-sign-in-only.mjs <outdir>
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'

const outDir = process.argv[2] || '/tmp/enterprise-sign-in-only-shots'
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5198, strictPort: true, host: '127.0.0.1' },
})
await server.listen()

const browser = await chromium.launch({ executablePath })
const shots = [
  // First run as a user opens it: empty field, Continue disabled.
  { name: 'sso-form-empty-dark', scene: 'signed-out', theme: 'dark', wait: '[data-testid="kas-login-sso-form"]' },
  { name: 'sso-form-empty-light', scene: 'signed-out', theme: 'light', wait: '[data-testid="kas-login-sso-form"]' },
  // After the user types their portal URL: Continue enabled.
  { name: 'sso-form-filled-dark', scene: 'signed-out', theme: 'dark', wait: '[data-testid="kas-login-sso-form"]', fill: 'https://acme.awsapps.com/start' },
  { name: 'starting-dark', scene: 'signed-in', theme: 'dark', wait: '[role="status"]' },
  { name: 'restart-dark', scene: 'restart', theme: 'dark', wait: '[data-testid="enterprise-signed-in-restart"]' },
]
for (const shot of shots) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    deviceScaleFactor: 2,
    reducedMotion: 'reduce',
  })
  const page = await ctx.newPage()
  await page.goto(
    `http://127.0.0.1:5198/capture/enterprise-sign-in-only.html?scene=${shot.scene}&theme=${shot.theme}`,
  )
  await page.waitForSelector(shot.wait, { timeout: 45000 })
  if (shot.fill) {
    await page.fill('#kas-sso-start-url', shot.fill)
  }
  await page.waitForTimeout(300)
  // The sign-in card only, at 2x, not the empty page around it.
  await page.locator('main > div').first().screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  await ctx.close()
}
await browser.close()
await server.close()
