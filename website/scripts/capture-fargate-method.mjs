/**
 * Screenshot harness for the `fargate` connection method on the Instances
 * surface: the add form with the Fargate method selected, and the Remote Crew
 * card with a connected fargate row (chat API URL to copy, nothing to open), an
 * idle fargate row (no URL) and an EC2-over-SSM row for contrast.
 *
 * Frames, one per claim a reader has to be able to verify:
 *
 *  1. The add form with "AWS Fargate task (chat API)" selected: the ECS task
 *     target field showing its placeholder, plus its hint, AWS profile and region, the remote port at
 *     the front proxy's 8080 with its hint; no remote user, token TTL or
 *     kirocrew path field.
 *  2. The crew list: the connected fargate row carries the Fargate badge, the
 *     Chat API copy field holding the forwarded URL and its note, and a
 *     Disconnect control, with no open or link control; the idle fargate row
 *     shows Connect and no URL; the EC2 row keeps its EC2 and SSM badges.
 *  3. Frame 2 again in the light theme.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no AWS.
 *
 * Usage: npm run build && node scripts/capture-fargate-method.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/fargate-method'
mkdirSync(OUT, { recursive: true })

const ECS_TARGET = 'ecs:kirocrew-dev_4f1c9a2b7d3e4f5a8b6c7d8e9f0a1b2c_4f1c9a2b7d3e4f5a8b6c7d8e9f0a1b2c-2653819172'
const LOCAL_PORT = 51230
const TURN_URL = `http://127.0.0.1:${LOCAL_PORT}/v1/chat/completions`

const crew = (id, name, extra) => ({
  id, name, ssh_host: '', remote_port: 5476, local_port: 0, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: id, state: 'disconnected', local_port: 0, remote_port: 5476 },
  ...extra,
})

const CREWS = [
  crew('fg-live', 'research-crew', {
    connection_method: 'fargate', ssm_target: ECS_TARGET, aws_profile: 'dev', aws_region: 'us-west-2',
    remote_port: 8080, local_port: LOCAL_PORT, was_connected: true,
    status: { instance_id: 'fg-live', state: 'connected', local_port: LOCAL_PORT, remote_port: 8080, turn_url: TURN_URL },
  }),
  crew('fg-idle', 'batch-crew', {
    connection_method: 'fargate', ssm_target: ECS_TARGET.replace('2653819172', '1187304456'),
    aws_profile: 'dev', aws_region: 'us-west-2', remote_port: 8080,
    status: { instance_id: 'fg-idle', state: 'disconnected', local_port: 0, remote_port: 8080 },
  }),
  crew('ec2-ssm', 'build-farm', {
    connection_method: 'ssm', ssm_target: 'i-0a1b2c3d4e5f60718', ssm_run_as: 'ec2-user',
    aws_profile: 'dev', aws_region: 'us-west-2', provisioner_id: 'aws_ec2',
  }),
]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const extra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/instances' && method === 'GET') {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ active: true, instances: CREWS, warm_set_cap: 10, sso: SSO }),
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
const context = await browser.newContext({ viewport: { width: 1200, height: 900 }, deviceScaleFactor: 2 })

const openInstances = async (pg) => {
  await pg.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await pg.getByText('research-crew', { exact: true }).first().waitFor({ timeout: 20000 })
  await pg.waitForTimeout(500)
}
const rowOf = (pg, name) => pg.locator('[data-crew-id]').filter({ hasText: name }).first()
const listClip = async (pg, padBottom = 20) => {
  const rows = pg.locator('[data-crew-id]')
  const first = await rows.first().boundingBox()
  const last = await rows.last().boundingBox()
  const pad = 12
  const top = Math.max(0, first.y - 48)
  return {
    x: Math.max(0, first.x - pad), y: top,
    width: Math.min(1200, first.width + 2 * pad), height: Math.min(900, last.y + last.height + padBottom) - top,
  }
}

const page = await context.newPage()
await stubDashboardApi(page, { extra })
logPageProblems(page)
await openInstances(page)

// ---- Frame 2 first: the rows are at the top of the page -------------------
const live = rowOf(page, 'research-crew')
const liveText = await live.innerText()
if (!/Fargate/.test(liveText)) fail(`connected fargate row must carry the Fargate badge: ${JSON.stringify(liveText)}`)
if (!/An AWS Fargate task/.test(liveText) || /cannot verify/.test(liveText)) fail(`connected fargate row must state it is an AWS task and not hedge: ${JSON.stringify(liveText)}`)
const urlField = live.getByTestId('turn-url')
await urlField.waitFor({ timeout: 10000 })
const shownUrl = await urlField.inputValue().catch(() => urlField.innerText())
if (!shownUrl.includes(TURN_URL)) fail(`chat API URL field must show the forwarded URL: ${JSON.stringify(shownUrl)}`)
if (!(await live.getByRole('button', { name: 'Copy the chat API URL of research-crew' }).isVisible())) fail('copy control missing on the connected fargate row')
if (!(await live.getByRole('button', { name: /Disconnect/ }).isVisible())) fail('Disconnect missing on the connected fargate row')
if ((await live.getByRole('button', { name: /^Open/ }).count()) !== 0) fail('a fargate row must offer nothing to open')
if ((await live.getByRole('link').count()) !== 0) fail('a fargate row must carry no link')
const idle = rowOf(page, 'batch-crew')
const idleText = await idle.innerText()
if (!/Fargate/.test(idleText)) fail('idle fargate row must carry the Fargate badge')
if ((await idle.getByTestId('turn-url').count()) !== 0) fail('an idle fargate row must show no chat API URL')
if (!(await idle.getByRole('button', { name: /Connect/ }).first().isVisible())) fail('Connect missing on the idle fargate row')
// The two fargate rows sit in one cluster and differ only at the tail of the
// runtime id, which the row's right-side truncation would cut off; the printed
// target must keep them apart and the hover title must carry the full target.
const targetOf = async (row) => {
  const span = row.locator('span[title^="ecs:"]').first()
  return { shown: (await span.innerText()).trim(), title: await span.getAttribute('title') }
}
const liveTarget = await targetOf(live)
const idleTarget = await targetOf(idle)
if (liveTarget.shown === idleTarget.shown) fail(`two fargate rows in one cluster must print distinct targets: ${JSON.stringify(liveTarget.shown)}`)
if (liveTarget.title !== ECS_TARGET) fail(`connected fargate row must carry the full target on hover: ${JSON.stringify(liveTarget.title)}`)
const ec2Text = await rowOf(page, 'build-farm').innerText()
if (!/EC2/.test(ec2Text) || !/SSM/.test(ec2Text)) fail(`build-farm row must keep its EC2 and SSM badges: ${JSON.stringify(ec2Text)}`)
await page.screenshot({ path: `${OUT}/02-crew-rows-connected-fargate.png`, clip: await listClip(page, 24) })
console.log('wrote 02')

// ---- Frame 1: the add form with the Fargate method selected ---------------
const trigger = page.getByRole('combobox', { name: 'Connection method' })
await trigger.scrollIntoViewIfNeeded()
const portBox = page.getByRole('textbox', { name: 'Remote port' })
if ((await portBox.inputValue()) !== '5476') fail(`the port default before the switch must be 5476: ${await portBox.inputValue()}`)
await trigger.click()
await page.getByRole('option', { name: 'AWS Fargate task (chat API)' }).click()
await page.getByRole('textbox', { name: 'ECS task target' }).waitFor({ timeout: 10000 })
if ((await portBox.inputValue()) !== '8080') fail(`the port default must follow the transport to 8080: ${await portBox.inputValue()}`)
for (const gone of ['SSH host / alias', 'Remote user', 'Token TTL']) {
  if ((await page.getByLabel(gone).count()) !== 0) fail(`${gone} must be absent for the fargate method`)
}
if ((await page.getByLabel(/Remote kirocrew path/i).count()) !== 0) fail('the kirocrew path field must be absent for the fargate method')
await page.getByRole('textbox', { name: 'Name' }).last().fill('research-crew-2')
// The ECS field stays empty on purpose: the frame has to show its placeholder,
// the shape a first-time user types against.
const ecsBox = page.getByRole('textbox', { name: 'ECS task target' })
if ((await ecsBox.getAttribute('placeholder')) !== 'ecs:my-cluster_taskid_runtimeid') fail(`ECS target placeholder: ${await ecsBox.getAttribute('placeholder')}`)
await page.getByRole('textbox', { name: 'AWS profile' }).fill('dev')
await page.getByRole('textbox', { name: 'AWS region' }).fill('us-west-2')
await page.waitForTimeout(300)
const addCard = page.locator('div.card-glow').filter({ has: page.getByRole('combobox', { name: 'Connection method' }) }).first()
await addCard.screenshot({ path: `${OUT}/01-add-form-fargate.png` })
console.log('wrote 01')

// ---- Frame 3: the rows again in the LIGHT theme ---------------------------
const lightPage = await context.newPage()
await stubDashboardApi(lightPage, { extra, theme: 'light' })
logPageProblems(lightPage)
await openInstances(lightPage)
await rowOf(lightPage, 'research-crew').getByTestId('turn-url').waitFor({ timeout: 10000 })
await lightPage.screenshot({ path: `${OUT}/03-crew-rows-connected-fargate-light.png`, clip: await listClip(lightPage, 24) })
console.log('wrote 03')

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
