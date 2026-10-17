/**
 * Screenshot harness for the Remote Crew launch form's unlaunched drafts.
 *
 *  0. The identity-lookup failure notice before any pick, with its "Ask the
 *     agent" hand-off.
 *  1. The identity-lookup failure notice beside the sign-in choice, with its
 *     "Ask the agent" hand-off, while the subnet and an Identity Center entry are
 *     typed in.
 *  2. The same form after the reader has left the panel and come back: the subnet
 *     and the Identity Center entry are still there, with Company SSO selected.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures. Every claim is ASSERTED before
 * its PNG is written.
 *
 * Usage: npm run build && node scripts/capture-remote-crew-launch-draft.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/remote-crew-launch-draft'
mkdirSync(OUT, { recursive: true })

const PREFLIGHT_OK = {
  reachable: true,
  account: '1234•••7890',
  arn: 'arn:aws:iam::123456787890:user/dev',
  ec2_reachable: true,
  cloudformation_reachable: true,
  ssm_reachable: true,
  session_manager_plugin: true,
  session_manager_plugin_command: '',
  session_manager_plugin_refused_path: '',
  session_manager_plugin_refused_reason: '',
  credential_helper_missing: '',
  note: '',
  detail: '',
}

const AWS_EC2_ROW = {
  id: 'aws_ec2',
  kind: 'aws_ec2',
  label: 'AWS EC2 in your own account',
  posix_only: true,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup', state: 'pending', detail: '' },
    { key: 'provision', label: 'Create the instance', state: 'pending', detail: '' },
    { key: 'signin', label: 'Sign in to Kiro', state: 'pending', detail: '' },
    { key: 'connect', label: 'Connect', state: 'pending', detail: '' },
  ],
}

const SUBNET = 'subnet-0123456789abcdef0'
const START_URL = 'https://example.awsapps.com/start'
const IDC_REGION = 'us-west-2'

const extra = async (path, route) => {
  if (path === '/api/instances') return json(route, { active: true, warm_set_cap: 5, instances: [] }), true
  if (path === '/api/cloud/launch') return json(route, { jobs: [] }), true
  if (path === '/api/cloud/provisioners') return json(route, { provisioners: [AWS_EC2_ROW] }), true
  if (path.startsWith('/api/cloud/preflight')) return json(route, PREFLIGHT_OK), true
  // The read of this computer's Kiro sign-in fails, so the notice shows.
  if (path === '/api/cloud/identity') return json(route, { error: 'identity lookup failed' }, 500), true
  if (path === '/api/cloud/iam-policy') return json(route, { policy: '{}' }), true
  return false
}

let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const VIEW = { width: 1220, height: 940 }
const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })

const openSetup = async page => {
  await page.getByRole('button', { name: /Set up a new one/i }).click()
  await page.getByRole('textbox', { name: 'Subnet ID (optional)' }).waitFor({ timeout: 20000 })
}

/** The subnet field through the Identity Center fields, as one clip. The label
 *  sits above its input, so the clip starts a line above the subnet box. */
const clipOf = async page => {
  const top = page.getByRole('textbox', { name: 'Subnet ID (optional)' })
  const bottom = page.getByRole('textbox', { name: /Identity Center region/i })
  await top.scrollIntoViewIfNeeded()
  await page.waitForTimeout(250)
  const a = await top.boundingBox()
  const b = await bottom.boundingBox()
  const pad = 14
  const x = Math.max(0, Math.min(a.x, b.x) - pad)
  const y = Math.max(0, a.y - 26 - pad)
  const right = Math.max(a.x + a.width, b.x + b.width) + pad
  return { x, y, width: Math.min(VIEW.width - x, right - x), height: Math.min(VIEW.height - y, b.y + b.height + 40 - y) }
}

const formState = async page => ({
  subnet: await page.getByRole('textbox', { name: 'Subnet ID (optional)' }).inputValue(),
  sso: await page.getByRole('radio', { name: /Company SSO/i }).isChecked(),
  url: await page.getByRole('textbox', { name: /Identity Center start URL/i }).inputValue(),
  region: await page.getByRole('textbox', { name: /Identity Center region/i }).inputValue(),
})

for (const theme of ['dark', 'light']) {
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await openSetup(page)

  // ---- Frame 0: the failure notice before any pick, with its hand-off ----
  await page.getByRole('textbox', { name: 'Subnet ID (optional)' }).fill(SUBNET)
  const prePick = page.locator('[role="alert"]').filter({ hasText: /request to read this computer's Kiro sign-in failed/i })
  await prePick.first().waitFor({ timeout: 20000 })
  if (!(await prePick.first().getByText('Ask the agent').isVisible())) {
    fail(`${theme}: the notice must offer Ask the agent before a pick`)
  }
  if (await page.getByRole('radio', { name: /Company SSO/i }).isChecked()) fail(`${theme}: nothing is picked yet`)
  await page.getByRole('radio', { name: /Company SSO/i }).scrollIntoViewIfNeeded()
  await page.waitForTimeout(250)
  {
    const a = await page.getByRole('textbox', { name: 'Subnet ID (optional)' }).boundingBox()
    const b = await page.getByRole('radio', { name: /Company SSO/i }).boundingBox()
    const x = Math.max(0, a.x - 14)
    const y = Math.max(0, a.y - 40)
    await page.screenshot({
      path: `${OUT}/00-identity-notice-before-pick-${theme}.png`,
      clip: { x, y, width: Math.min(VIEW.width - x, a.width + 28), height: Math.min(VIEW.height - y, b.y + b.height + 60 - y) },
    })
  }
  console.log(`wrote 00 (${theme})`)

  // ---- Frame 1: the failure notice beside a half-filled sign-in ----
  await page.getByRole('textbox', { name: 'Subnet ID (optional)' }).fill(SUBNET)
  await page.getByRole('radio', { name: /Company SSO/i }).check()
  await page.getByRole('textbox', { name: /Identity Center start URL/i }).fill(START_URL)
  await page.getByRole('textbox', { name: /Identity Center region/i }).fill(IDC_REGION)
  const notice = page.locator('[role="alert"]').filter({ hasText: /Kiro sign-in could not be read/i })
  await notice.first().waitFor({ timeout: 20000 })
  const askAgent = notice.first().getByText('Ask the agent')
  if (!(await askAgent.isVisible())) fail(`${theme}: the identity-lookup notice must offer Ask the agent`)
  const before = await formState(page)
  if (before.subnet !== SUBNET || !before.sso || before.url !== START_URL || before.region !== IDC_REGION) {
    fail(`${theme}: the form must hold what was typed: ${JSON.stringify(before)}`)
  }
  await page.screenshot({ path: `${OUT}/01-identity-notice-with-handoff-${theme}.png`, clip: await clipOf(page) })
  console.log(`wrote 01 (${theme})`)

  // ---- Frame 2: back after leaving the panel, the entries are still there ----
  // Leaves by an in-app navigation to another Settings section: it unmounts the
  // panel exactly as the hand-off's navigation to chat does (the unit tests drive
  // the hand-off itself), and this fixture server cannot render the chat page.
  await page.evaluate(() => {
    history.pushState({}, '', '/settings?tab=about')
    dispatchEvent(new PopStateEvent('popstate'))
  })
  try {
    await page.getByRole('textbox', { name: 'Subnet ID (optional)' }).waitFor({ state: 'detached', timeout: 20000 })
  } catch {
    fail(`${theme}: leaving the section must unmount the Remote Crew panel`)
  }
  await page.goBack({ waitUntil: 'domcontentloaded' })
  await openSetup(page)
  await page.waitForTimeout(400)
  const after = await formState(page)
  if (JSON.stringify(after) !== JSON.stringify(before)) {
    fail(`${theme}: the form must come back as it was left: ${JSON.stringify(after)}`)
  }
  const back = page.locator('[role="alert"]').filter({ hasText: /The new crew will sign in with the option you picked below\./ })
  if ((await back.count()) !== 1) fail(`${theme}: the notice must say the kept choice is the reader's own`)
  if (await page.getByText(/Choose one below to enable Launch/).count()) {
    fail(`${theme}: the notice must not ask for a choice that is already made`)
  }
  await page.screenshot({ path: `${OUT}/02-restored-after-leaving-${theme}.png`, clip: await clipOf(page) })
  console.log(`wrote 02 (${theme})`)
  await page.close()
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
