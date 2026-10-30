/**
 * Screenshot harness for #23692: in Settings → Remote Crew, when management is
 * on but the gateway has NOT been restarted yet (the list reports
 * `active: false`), the "not active yet" banner shows AND the row's Connect
 * button is now disabled with a title saying why, and points its
 * aria-describedby at the banner — instead of looking fully clickable.
 *
 * Each frame is the FULL app (sidebar, header, banner and the Connect row in
 * one shot), captured without a clip, which is what UX Review needs. Frames,
 * named to match the files committed under .github/screenshots/23692/:
 *   01-banner-connect-disabled-dark.png  — dark theme: the warn banner, plus a
 *                                           row whose Connect is muted/disabled.
 *   03-banner-connect-disabled-light.png — the disabled state in the light theme.
 *   04-connect-enabled-after-restart-dark.png — active:true: the banner is gone
 *                                           and Connect is live again.
 *
 * (No tooltip frame: browsers suppress the native `title` on a disabled button,
 * which is the reason this fix adds aria-describedby; a hover frame would be
 * identical to frame 1. The aria link is covered by the unit test instead.)
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no AWS.
 *
 * Usage: npm run build && node scripts/capture-23692-connect-disabled.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/23692-connect-disabled'
mkdirSync(OUT, { recursive: true })

const CREW = {
  id: 'dev-box-1', name: 'dev-box-1', ssh_host: 'dev-box-1', remote_port: 5476,
  local_port: 0, ttl: '20h', remote_bin: '', connection_method: 'ssh',
  ssm_target: '', ssm_run_as: '', aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: 'dev-box-1', state: 'disconnected', local_port: 0, remote_port: 5476 },
}
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const extra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/instances' && method === 'GET') {
    // active:false — management on, gateway not restarted yet.
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ active: false, instances: [CREW], warm_set_cap: 10, sso: SSO }),
    })
    return true
  }
  if (path === '/api/cloud/launch') {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: [] }) })
    return true
  }
  if (path === '/api/cloud/provisioners') {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ provisioners: [{ id: 'aws_ec2', kind: 'aws_ec2', label: 'Amazon EC2', posix_only: true, steps: [] }] }),
    })
    return true
  }
  if (path.startsWith('/api/cloud/')) {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    return true
  }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
// Full-app frames: a wide-enough viewport to hold the sidebar, the page header
// and the crew row in one shot, captured WITHOUT a clip so the whole app shell
// is in frame (what UX Review needs). deviceScaleFactor 2 keeps the text crisp.
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })

const openInstances = async (pg) => {
  await pg.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await pg.getByText('dev-box-1', { exact: true }).first().waitFor({ timeout: 20000 })
  await pg.waitForTimeout(500)
}

const page = await context.newPage()
await stubDashboardApi(page, { extra })
logPageProblems(page)
await openInstances(page)

// Verify the state the frames claim: banner present, Connect disabled.
const banner = page.getByText(/not active yet/)
if (!(await banner.count())) fail('expected the "not active yet" banner')
const connect = page.getByRole('button', { name: /^Connect$/i })
if (!(await connect.count())) fail('expected a Connect button')
else if (await connect.isEnabled()) fail('Connect should be disabled while active:false')

// Frame 1 — dark theme, full app: sidebar + header + banner + the muted Connect row.
await page.screenshot({ path: `${OUT}/01-banner-connect-disabled-dark.png` })

// No tooltip frame: the disabled Connect button carries a `title`, but browsers
// suppress the native tooltip on a disabled control (exactly why this fix adds
// aria-describedby), so a hover frame is byte-for-byte frame 1 and adds nothing.
// The aria link is asserted in RemoteCrewPanel.connectNeedsRestart.test.tsx.

// Frame 3 — light theme, on a fresh page with the theme stub set to light.
const lightPage = await context.newPage()
await stubDashboardApi(lightPage, { extra, theme: 'light' })
logPageProblems(lightPage)
await openInstances(lightPage)
{
  const lb = lightPage.getByText(/not active yet/)
  if (!(await lb.count())) fail('light: expected the "not active yet" banner')
  const lc = lightPage.getByRole('button', { name: /^Connect$/i })
  if (await lc.count() && await lc.isEnabled()) fail('light: Connect should be disabled while active:false')
}
await lightPage.screenshot({ path: `${OUT}/03-banner-connect-disabled-light.png` })

// Frame 4 — the re-enabled state after the restart: the same crew, but the
// gateway now reports active:true, so the banner is gone and Connect is live
// again. This is the "after restart" frame the review asked for (c).
const activeExtra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/instances' && method === 'GET') {
    // active:true — the gateway has been restarted, tunnels can be opened.
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ active: true, instances: [CREW], warm_set_cap: 10, sso: SSO }),
    })
    return true
  }
  return extra(path, route)
}
const activePage = await context.newPage()
await stubDashboardApi(activePage, { extra: activeExtra })
logPageProblems(activePage)
await openInstances(activePage)
{
  const ab = activePage.getByText(/not active yet/)
  if (await ab.count()) fail('active: the "not active yet" banner should be gone once active:true')
  const ac = activePage.getByRole('button', { name: /^Connect$/i })
  if (!(await ac.count())) fail('active: expected a Connect button')
  else if (!(await ac.isEnabled())) fail('active: Connect should be enabled once active:true')
}
await activePage.screenshot({ path: `${OUT}/04-connect-enabled-after-restart-dark.png` })

await browser.close()
srv.close()
if (failures) { console.error(`${failures} check(s) failed`); process.exit(1) }
console.log(`OK: wrote frames to ${OUT}`)
