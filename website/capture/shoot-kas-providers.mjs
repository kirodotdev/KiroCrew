// Shoot the KAS login chooser (4 providers) and the expanded company-SSO form,
// reusing the shipped kas-login-copy capture harness (real i18n via initI18n,
// api-level stubs, ?lang= support). Run from website/:
//   node capture/shoot-kas-providers.mjs <outdir>
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import path from 'node:path'

const outDir = process.argv[2] || '/tmp/kas-provider-shots'
const executablePath = process.env.CHROMIUM_PATH

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5199, strictPort: true, host: '127.0.0.1' },
})
await server.listen()

const browser = await chromium.launch({ executablePath })
const shots = [
  { name: 'chooser-en', lang: 'en', sso: false, ssoLabel: 'Continue with company SSO' },
  { name: 'sso-form-en', lang: 'en', sso: true, ssoLabel: 'Continue with company SSO' },
  { name: 'chooser-zh-CN', lang: 'zh-CN', sso: false, ssoLabel: '使用公司 SSO 继续' },
  { name: 'sso-form-zh-CN', lang: 'zh-CN', sso: true, ssoLabel: '使用公司 SSO 继续' },
]
for (const shot of shots) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    // framer-motion entrance animations do not run under headless_shell,
    // leaving elements at initial opacity 0; the shell honors reduced motion.
    reducedMotion: 'reduce',
  })
  const page = await ctx.newPage()
  await page.goto(
    `http://127.0.0.1:5199/capture/kas-login-copy.html?scene=chooser&theme=dark&lang=${shot.lang}`,
  )
  await page.getByRole('button', { name: shot.ssoLabel }).waitFor({ timeout: 45000 })
  if (shot.sso) {
    await page.getByRole('button', { name: shot.ssoLabel }).click()
    await page.waitForSelector('[data-testid="kas-login-sso-form"]')
    await page.fill('#kas-sso-start-url', 'https://your-company.awsapps.com/start')
  }
  await page.waitForTimeout(400)
  await page.screenshot({ path: path.join(outDir, `${shot.name}.png`) })
  console.log('shot', shot.name)
  await ctx.close()
}
await browser.close()
await server.close()
