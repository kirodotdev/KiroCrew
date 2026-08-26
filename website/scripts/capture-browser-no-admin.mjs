/**
 * Evidence for the Browser panel's Node-blocked state, which now offers the
 * standalone installer instead of ending at a "Download Node.js" link.
 *
 * That link is the wrong and only answer for the operator this state usually
 * describes: a machine where Node cannot be installed at all, or a registry that
 * answers 401. Both scenes below are the ones a screenshot has to prove — the
 * offer appears when the install is blocked, and does NOT appear when it is not,
 * because otherwise the panel would tell a healthy user to go run a shell script.
 *
 * Runs the REAL built SPA (website/dist) behind a static server with every
 * /api/** call answered from a fixture — no gateway, no token, no agent. Same
 * harness as capture-browser-mode.mjs.
 *
 * Usage: node scripts/capture-browser-no-admin.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/browser-no-admin'
const PROJECT = '/home/user/workspace/KiroCrew'
mkdirSync(OUT, { recursive: true })

/** The single mutable fixture the panel reads, reassigned between reloads. */
let install = {
  installed: false,
  cli_path: null,
  cli_version: null,
  node_ok: false,
  node_version: null,
  browser_ok: false,
  installing: false,
  last_error: null,
  token: false,
  browsers: { chromium: false, firefox: false, webkit: false },
  standalone_install:
    '_pwcli_dir=$(mktemp -d) && curl -fsSL https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/playwright-cli.sh -o "$_pwcli_dir/playwright-cli.sh" && sh "$_pwcli_dir/playwright-cli.sh"',
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
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: true })
  console.log(`wrote ${OUT}/${name}`)
}

// 1. Node absent: the offer is the point of the shot.
await shoot('01-node-blocked-offers-installer.png', /no admin rights/i)

// 2. Node too old: same remedy, different sentence above it.
install = { ...install, node_version: '18.4.0' }
await shoot('02-node-too-old-offers-installer.png', /no admin rights/i)

// 3. Node fine, CLI absent: the offer must be ABSENT and the button present.
install = { ...install, node_ok: true, node_version: '22.11.0' }
await shoot('03-node-ok-plain-install-button.png', /Install Playwright CLI/i)

await browser.close()
srv.close()
