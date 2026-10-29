/**
 * Evidence for the Browser panel's npm registry refusal message.
 *
 * When the operator's own npm registry refuses the CLI install (npm E401/E403,
 * usually an expired login), the status region now names that registry, says
 * how to sign in again, and offers an opt-in install from the public npm
 * registry. When that retry is refused too, no further retry is offered.
 *
 * Runs the REAL built SPA (website/dist) behind a static server with every
 * /api/** call answered from a fixture. Same harness as capture-browser-no-admin.mjs.
 *
 * Usage: node scripts/capture-browser-npm-registry-refusal.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/browser-npm-registry-refusal'
const PROJECT = '/home/user/workspace/KiroCrew'
mkdirSync(OUT, { recursive: true })

const failedSetup = (errorCode, params) => ({
  id: 'job-1',
  kind: 'cli_setup',
  engine: null,
  status: 'failed',
  stage: 'installing_cli',
  started_at: '2026-10-29T05:00:00Z',
  updated_at: '2026-10-29T05:00:20Z',
  finished_at: '2026-10-29T05:00:20Z',
  elapsed_s: 20,
  error_code: errorCode,
  error_detail: 'npm-install-global: npm could not sign in to your configured npm registry',
  error_params: params,
})

let install = {
  installed: false,
  cli_path: null,
  cli_version: null,
  node_ok: true,
  node_version: '24.15.0',
  browser_ok: false,
  installing: false,
  last_error: 'npm-install-global: npm could not sign in to your configured npm registry',
  token: false,
  browsers: { chromium: false, firefox: false, webkit: false },
  install_job: failedSetup('registry_auth', {
    registry: 'https://registry.example.test/',
    npm_code: 'E401',
  }),
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 900 } })
await page.routeWebSocket(/\/api\/ws/, () => {})

const fixedApi = makeFixedApi(PROJECT)
await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/browser/install') return json(route, install)
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})
await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
})

const shoot = async (name, match) => {
  await page.goto(`${base}/settings?tab=browser`, { waitUntil: 'domcontentloaded' })
  await page.getByText(match).first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  await page.getByTestId('browser-install-status').screenshot({ path: `${OUT}/${name}` })
  console.log(`wrote ${OUT}/${name}`)
}

// 1. The configured registry refused: named, with the opt-in retry.
await shoot('01-registry-refused-offers-public-retry.png', /Install from the public npm registry/)

// 2. The public-registry retry was refused too: no further retry.
install = {
  ...install,
  install_job: failedSetup('public_registry_refused', {
    registry: 'https://registry.npmjs.org/',
    npm_code: 'E403',
  }),
}
await shoot('02-public-registry-refused-no-retry.png', /refused the request too/)

await browser.close()
srv.close()
