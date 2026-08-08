/**
 * Screenshot harness for the third-party trust-consent modal (TrustAppModal).
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 * `POST /api/apps/launchdarkly/enable` is answered with the real refusal shape
 * (403 + `code: "app_execution_denied"`), which is what opens the modal.
 *
 *   01 consent   → title, scope line, three capabilities, provenance, actions
 *   02 failed    → the retried enable also fails, reported inline
 *
 * Also asserts the translucent capability panel resolves to a real
 * `background-color` (color-mix), not a dropped declaration.
 *
 * Usage: node scripts/capture-trust-app-modal.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/trust-app-modal'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const REPO = 'https://github.com/launchdarkly-labs/launchdarkly-kiro-crew-app'

const MANIFEST = {
  name: 'launchdarkly', version: '1.0.0', displayName: 'LaunchDarkly',
  description: 'Manage feature flags from your agentic workspace.',
  author: 'launchdarkly', repo: REPO,
}

const INSTALLED = [{
  name: 'launchdarkly', displayName: 'LaunchDarkly', version: '1.0.0',
  enabled: false, installedAt: '2026-08-03T00:00:00Z', origin: 'registry',
  manifest: MANIFEST,
}]

const REGISTRY = [{
  name: 'launchdarkly', displayName: 'LaunchDarkly', version: '1.0.0',
  description: MANIFEST.description, author: 'launchdarkly', repo: REPO,
  tags: ['feature-flags'], installed: true, enabled: false, origin: 'registry',
}]

const MODAL = '[role="dialog"]'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  logPageProblems(page)

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/apps') { await route.fulfill({ json: INSTALLED }); return true }
      if (path === '/api/apps/registry') {
        await route.fulfill({ json: { apps: REGISTRY, serverPlatform: { os: 'darwin', arch: 'arm64' } } })
        return true
      }
      if (path === '/api/apps/registries') { await route.fulfill({ json: { registries: [] } }); return true }
      // The refusal that opens the modal — identified by CODE, not message.
      if (path === '/api/apps/launchdarkly/enable') {
        await route.fulfill({
          status: 403,
          json: { error: 'App launchdarkly is not trusted to run its own code.', code: 'app_execution_denied' },
        })
        return true
      }
      if (path === '/api/security/trusted-apps/launchdarkly') {
        await route.fulfill({ json: { apps: ['launchdarkly'], allowAll: false } })
        return true
      }
      return false
    },
  })

  await page.goto(`${base}/apps`)
  await page.getByRole('button', { name: /Library/ }).first().click()
  await page.getByRole('button', { name: /^Enable$/ }).first().click()
  await page.waitForSelector(MODAL)
  await page.waitForTimeout(400)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-01-consent.png` })

  // The translucent capability panel must resolve to a real background-color.
  const bg = await page.locator(`${MODAL} ul`).evaluate(el => getComputedStyle(el).backgroundColor)
  console.log('capability panel background-color =', bg)
  if (!bg || bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') {
    throw new Error(`translucent surface resolved to nothing: ${bg}`)
  }

  // Confirm → trust grant succeeds, retried enable still refused → inline failure.
  await page.getByRole('button', { name: /Trust this app and enable/ }).click()
  await page.waitForSelector(`${MODAL} [role="alert"]`)
  await page.waitForTimeout(300)
  await page.locator(MODAL).screenshot({ path: `${OUT}/${PREFIX}-02-failed.png` })

  await browser.close()
  srv.close()
  console.log(`Wrote ${OUT}/${PREFIX}-01-consent.png and ${PREFIX}-02-failed.png`)
}

main().catch(e => { console.error(e); process.exit(1) })
