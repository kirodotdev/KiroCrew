/**
 * Screenshot harness for Settings → Secrets, Custom secrets card — proving the
 * reworded `settings.secrets.custom_description` copy renders (UX Review evidence
 * for PR #10383, the mediated Custom-secret egress feature).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server, every /api/** call answered from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no vault.
 *
 * Usage: node scripts/capture-secrets-custom-description.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/secrets-custom-description'
mkdirSync(OUT, { recursive: true })

async function capture(theme) {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await page.addInitScript(mode => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme-mode', mode)
  }, theme)

  await stubDashboardApi(page, {
    slots: [],
    extra: async (path, route) => {
      if (path === '/api/config/kirocrew') return json(route, {}), true
      if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
      if (path === '/api/mcp' || path === '/api/mcp/probe') return json(route, []), true
      // One custom secret so the card renders populated (not the empty state),
      // and the managed section stays empty.
      if (path === '/api/secrets') {
        return json(route, { secrets: [{ name: 'STRIPE_KEY', source: 'custom' }], managed: [] }), true
      }
      return false
    },
  })

  await page.goto(base + '/settings/secrets', { waitUntil: 'domcontentloaded' })
  // The Custom secrets card header + its reworded description.
  await page.getByText('Custom secrets', { exact: false }).first().waitFor({ state: 'visible' })
  await page.getByText(/never shown in chat/i).first().waitFor({ state: 'visible' })

  const shot = name =>
    page.screenshot({ path: `${OUT}/${name}-${theme}.png`, fullPage: false })
  await shot('secrets-custom-description')

  await browser.close()
  await srv.close()
}

for (const theme of ['light', 'dark']) {
  await capture(theme)
  console.log(`captured ${theme}`)
}
console.log(`done -> ${OUT}`)
