/**
 * Screenshot harness for #13337: a crew whose connection method this build has
 * no transport for (`outbound`), next to a fargate crew and an SSH crew.
 *
 * Frames:
 *  1/2. The crew switcher menu (dark, light): the unmapped row names no machine
 *       at all and the SSH row keeps its host. A fargate crew has no dashboard
 *       pane (`hasDashboardPane`), so it gets no switcher row, before or after.
 *  3/4. The Remote Crew card rows (dark, light): the unmapped row renders as its
 *       own method name, its Connect is disabled, and the hint that explains why
 *       is foreground text ending in the next step.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call stubbed.
 * Usage: npm run build && node scripts/capture-unmapped-transport.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/13337-unmapped-transport'
mkdirSync(OUT, { recursive: true })

const ECS_TARGET = 'ecs:kirocrew-dev_4f1c9a2b7d3e4f5a8b6c7d8e9f0a1b2c_4f1c9a2b7d3e4f5a8b6c7d8e9f0a1b2c-2653819172'
const HINT_TAIL = 'Update Kiro Crew, then connect again.'

const crew = (id, name, extra) => ({
  id, name, ssh_host: '', remote_port: 5476, local_port: 0, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: id, state: 'disconnected', local_port: 0, remote_port: 5476 },
  ...extra,
})

const CREWS = [
  crew('fg', 'research-crew', {
    connection_method: 'fargate', ssm_target: ECS_TARGET, aws_profile: 'dev',
    aws_region: 'us-west-2', remote_port: 8080, was_connected: true,
  }),
  // was_connected: a crew joins the switcher once it has been connected.
  crew('out', 'edge-crew', { connection_method: 'outbound', was_connected: true }),
  crew('ssh', 'devdesk', { ssh_host: 'dev-dsk-alias', was_connected: true }),
]
// One idle chat slot, the shape capture-crew-tab-stable-order seeds: the app
// shell's recents provider expects a slot list, and the switcher lives in it.
const SLOTS = [{
  key: 'unmapped-shot', title: 'Remote crews', running: false, last_message: '',
  messages: 1, agent: 'kirocrew', memory_mode: 'persistent', folder_id: '',
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const extra = async (path, route) => {
  if (path === '/api/instances' && route.request().method() === 'GET') {
    await json(route, { active: true, instances: CREWS, warm_set_cap: 10, sso: SSO })
    return true
  }
  if (path === '/api/cloud/launch') { await json(route, { jobs: [] }); return true }
  if (path.startsWith('/api/cloud/') || path.startsWith('/api/instances/')) {
    await json(route, {})
    return true
  }
  return false
}

const { srv, base } = await serveDist()
// PW_CHROMIUM lets a host whose cached browser revision differs from the pinned
// playwright reuse the one it has instead of downloading another.
const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {})

const newPage = async (theme) => {
  const context = await browser.newContext({ viewport: { width: 1200, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme, slots: SLOTS })
  logPageProblems(page)
  return page
}

const TRIGGER = '[aria-label^="Switch crew"]'

const switcher = async (theme, file) => {
  const page = await newPage(theme)
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(TRIGGER, { timeout: 20000 })
  await page.locator(`${TRIGGER}:visible`).first().click()
  await page.waitForSelector('[role="menuitemradio"]', { timeout: 10000 })
  await page.waitForTimeout(300)
  const row = (name) => page.locator('[role="menuitemradio"]').filter({ hasText: name }).first()
  const out = await row('edge-crew').innerText()
  const ssh = await row('devdesk').innerText()
  if ((await row('research-crew').count()) !== 0) fail(`${theme}: a fargate crew must get no switcher row`)
  if (/dev-dsk-alias|ecs:/.test(out)) fail(`${theme}: unmapped switcher row must name no machine: ${JSON.stringify(out)}`)
  if (!ssh.includes('dev-dsk-alias')) fail(`${theme}: ssh switcher row must keep its host: ${JSON.stringify(ssh)}`)
  const box = await page.evaluate(() => {
    const r = document.querySelector('[role="menuitemradio"]')?.closest('[role="menu"]')?.getBoundingClientRect()
    return r ? { x: r.x, y: r.y, width: r.width, height: r.height } : null
  })
  if (!box) fail(`${theme}: switcher menu not found`)
  await page.screenshot({
    path: `${OUT}/${file}`,
    clip: box ? { x: Math.max(0, box.x - 8), y: 0, width: box.width + 16, height: box.y + box.height + 8 } : undefined,
  })
  console.log(`wrote ${file}`)
  await page.context().close()
}

const settingsRows = async (theme, file) => {
  const page = await newPage(theme)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await page.getByText('edge-crew', { exact: true }).first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  const out = page.locator('[data-crew-id]').filter({ hasText: 'edge-crew' }).first()
  const text = await out.innerText()
  if (!/outbound/i.test(text)) fail(`${theme}: unmapped row must render its own method name: ${JSON.stringify(text)}`)
  if (!text.includes(HINT_TAIL)) fail(`${theme}: unmapped hint must end with the next step: ${JSON.stringify(text)}`)
  const connect = out.getByRole('button', { name: /Connect/ }).first()
  if (!(await connect.isDisabled())) fail(`${theme}: Connect must be disabled on the unmapped row`)
  const hintClass = await out.locator('span[id$="-unmapped-hint"]').getAttribute('class')
  if (/text-muted/.test(hintClass || '')) fail(`${theme}: the unmapped hint must not be muted: ${hintClass}`)
  const rows = page.locator('[data-crew-id]')
  const first = await rows.first().boundingBox()
  const last = await rows.last().boundingBox()
  const top = Math.max(0, first.y - 48)
  await page.screenshot({
    path: `${OUT}/${file}`,
    clip: { x: Math.max(0, first.x - 12), y: top, width: Math.min(1200, first.width + 24), height: Math.min(900, last.y + last.height + 24) - top },
  })
  console.log(`wrote ${file}`)
  await page.context().close()
}

await switcher('dark', 'switcher-dark.png')
await switcher('light', 'switcher-light.png')
await settingsRows('dark', 'after-dark.png')
await settingsRows('light', 'after-light.png')

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
