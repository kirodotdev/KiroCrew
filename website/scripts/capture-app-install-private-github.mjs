/**
 * Component render, not the live page: real AppDetailPage with mocked API data.
 * Triggers a failed install and requires the complete backend signed-out sentence
 * and Install failed state before capturing dark and light frames.
 * Starts its own Vite server on 127.0.0.1 and closes it even on failure.
 * Usage: node scripts/capture-app-install-private-github.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const OUT = resolve(process.argv[2] || '../temp-screenshots/app-install-private-github')
mkdirSync(OUT, { recursive: true })
const ERROR = "acme/team-apps looks private. Sign in to the GitHub CLI (gh) on the computer that runs Kiro Crew, with an account that can read acme/team-apps. Kiro Crew uses that computer's GitHub sign-in."
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
let browser
try {
  await server.listen()
  const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
  const browserEnv = { ...process.env }
  delete browserEnv.LD_LIBRARY_PATH
  browser = await chromium.launch({ env: browserEnv })
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 1000, height: 900 }, deviceScaleFactor: 2, colorScheme: theme })
    try {
      const page = await ctx.newPage()
      await page.goto(`${BASE}/capture/app-install-private-github.html?theme=${theme}`, { waitUntil: 'networkidle' })
      await page.getByRole('button', { name: 'Install', exact: true }).click({ timeout: 15000 })
      await page.getByRole('alert').getByText(ERROR, { exact: true }).waitFor({ state: 'visible', timeout: 10000 })
      await page.getByText('Install failed', { exact: true }).waitFor({ state: 'visible', timeout: 10000 })
      const file = `${OUT}/app-install-gh_signed_out-${theme}.png`
      await page.locator('[data-capture-root]').screenshot({ path: file })
      console.log(`  ok ${file}`)
    } finally {
      await ctx.close()
    }
  }
} finally {
  await browser?.close()
  await server.close()
}
