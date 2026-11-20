/**
 * Screenshot harness for #38256: Settings > Remote Crew with remote crew
 * management off still shows the Shared Crews cards under the "off" card.
 *
 * Two frames, dark and light. `/api/instances` answers the 403
 * `instances_disabled` refusal; the desktop bridge (`window.relayCrewAPI`) is
 * stubbed so the relay hook reads the list; names are made up.
 *
 * Usage: npm run build && node scripts/capture-38256-shared-crews-off.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/shared-crews-off'
mkdirSync(OUT, { recursive: true })

const CREWS = [
  { endpointId: 'ep-1', name: 'Platform team', crewType: 'TEAM', presence: 'CONNECTED', relayConnectionStatus: 'CONNECTED', crewUrl: 'https://ep-1.relay.example', lastSeenAt: null },
  { endpointId: 'ep-2', name: 'Data pipeline', crewType: 'TEAM', presence: 'GONE', relayConnectionStatus: 'DISCONNECTED', crewUrl: 'https://ep-2.relay.example', lastSeenAt: null },
]
const json = (status, body) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()

for (const theme of ['dark', 'light']) {
  const context = await browser.newContext({ viewport: { width: 1200, height: 1000 }, deviceScaleFactor: 2, colorScheme: theme })
  const page = await context.newPage()
  await page.addInitScript(() => {
    window.relayCrewAPI = { open: async () => ({ ok: true }) }
  })
  await stubDashboardApi(page, {
    theme,
    extra: async (path, route) => {
      if (path === '/api/relay-viewer/crews') { await route.fulfill(json(200, { crews: CREWS })); return true }
      if (path === '/api/instances') {
        await route.fulfill(json(403, { error: 'instances feature is disabled', code: 'instances_disabled' }))
        return true
      }
      if (path === '/api/cloud/launch') { await route.fulfill(json(200, { jobs: [] })); return true }
      if (path.startsWith('/api/cloud/')) { await route.fulfill(json(200, {})); return true }
      return false
    },
  })
  logPageProblems(page)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  const enable = page.getByRole('button', { name: 'Enable remote crew management' })
  const card = page.getByTestId('shared-crews-card')
  await enable.waitFor({ timeout: 20000 })
  await card.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  if ((await page.getByTestId('shared-crew-row').count()) !== 2) fail(`${theme}: expected 2 rows`)
  const off = enable.locator('xpath=ancestor::div[contains(@class,"space-y-4")][1]')
  const setup = page.getByTestId('shared-crews-setup-card')
  await setup.scrollIntoViewIfNeeded()
  const a = await off.boundingBox()
  const b = await setup.boundingBox()
  const pad = 12
  await page.screenshot({
    path: `${OUT}/shared-crews-off-${theme}.png`,
    clip: { x: Math.max(0, a.x - pad), y: Math.max(0, a.y - pad), width: a.width + 2 * pad, height: b.y + b.height - a.y + 2 * pad },
  })
  console.log(`wrote shared-crews-off-${theme}`)
  await context.close()
}

await browser.close()
srv.close()
if (failures) {
  console.error(`${failures} check(s) failed`)
  process.exit(1)
}
