/**
 * Screenshot harness for #32068: the Remote Crew set-up check on a host with no
 * sandbox for the `aws` command.
 *
 *  1. `no-sandbox`: the gateway answers the preflight with its typed
 *     `503 sandbox_unavailable`. The notice says, in the reader's language, that the
 *     check could not run, that credentials were not checked, and to run
 *     `kirocrew doctor`. The line under it says the check did not run.
 *  2. `raw-500`: the old answer, aiohttp's bare text 500 page. Run against a build
 *     of origin/main this is the BEFORE frame; against this branch it shows that any
 *     other failed check no longer blames credentials either.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures. The backend tests cover the
 * gateway's own answer.
 *
 * Usage: npm run build && node scripts/capture-32068-preflight-no-sandbox.mjs [outDir] [before]
 *   Pass `before` as the second argument on an origin/main build: only frame 2 is
 *   shot, and its assertions describe the old copy.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/32068-preflight-no-sandbox'
const BEFORE = process.argv[3] === 'before'
mkdirSync(OUT, { recursive: true })

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

/** The body handlers_cloud._sandbox_unavailable_response writes. */
const NO_SANDBOX = {
  error:
    'The AWS check could not run: this computer has no sandbox for the aws command, so nothing ' +
    'was sent to AWS and your credentials were not checked. Run `kirocrew doctor` in a terminal for the fix.',
  code: 'sandbox_unavailable',
  kind: 'no_backend',
  detail: 'unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)',
  remedy: 'apparmor_userns',
}

const world = { preflight: 'no-sandbox' }

const extra = async (path, route) => {
  if (path === '/api/instances') return json(route, { active: true, warm_set_cap: 5, instances: [] }), true
  if (path === '/api/cloud/launch') return json(route, { jobs: [] }), true
  if (path === '/api/cloud/provisioners') return json(route, { provisioners: [AWS_EC2_ROW] }), true
  if (path.startsWith('/api/cloud/preflight')) {
    if (world.preflight === 'no-sandbox') return json(route, NO_SANDBOX, 503), true
    // aiohttp's default answer to an uncaught handler exception.
    await route.fulfill({
      status: 500,
      contentType: 'text/plain; charset=utf-8',
      body: '500 Internal Server Error\n\nServer got itself in trouble',
    })
    return true
  }
  if (path === '/api/cloud/identity') {
    return json(route, { identity: null, suggested_target: null, discovery: 'read' }), true
  }
  if (path === '/api/cloud/iam-policy') return json(route, { policy: '{}' }), true
  return false
}

let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const VIEW = { width: 1220, height: 940 }
const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })

/** The "Before you start" card once the failed check has rendered its Re-check. */
const openCard = async theme => {
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /Set up a new one/i }).click()
  const card = page.locator('div', { has: page.getByText('Before you start — your AWS setup', { exact: true }) })
    .filter({ has: page.getByRole('button', { name: /^Re-check$/ }) })
    .last()
  // The query retries a failed read before it reports isError.
  await card.waitFor({ timeout: 30000 })
  await page.waitForTimeout(400)
  return { page, card }
}

const shoot = async (page, card, file) => {
  await card.scrollIntoViewIfNeeded()
  await page.waitForTimeout(250)
  const box = await card.boundingBox()
  const pad = 12
  const x = Math.max(0, box.x - pad)
  const y = Math.max(0, box.y - pad)
  await page.screenshot({
    path: `${OUT}/${file}`,
    clip: { x, y, width: Math.min(VIEW.width - x, box.width + 2 * pad), height: Math.min(VIEW.height - y, box.height + 2 * pad) },
  })
  console.log(`wrote ${file}`)
}

if (!BEFORE) {
  world.preflight = 'no-sandbox'
  for (const theme of ['dark', 'light']) {
    const { page, card } = await openCard(theme)
    const text = await card.innerText()
    if (!/The AWS check could not run because this computer has no sandbox for the aws command\. Your credentials were not checked\. Run kirocrew doctor in a terminal for the fix, then re-check\./.test(text)) {
      fail(`${theme}: the notice must name the missing sandbox and kirocrew doctor: ${JSON.stringify(text)}`)
    }
    if (/500 Internal Server Error|Server got itself in trouble/.test(text)) fail(`${theme}: no raw 500 text`)
    if (/Check your credentials/.test(text)) fail(`${theme}: credentials were never read, so they must not be blamed`)
    if (!/The check did not run, so your credentials were not checked\./.test(text)) {
      fail(`${theme}: the no-result line must say the check did not run`)
    }
    await shoot(page, card, `01-no-sandbox-after-${theme}.png`)
    await page.close()
  }
}

world.preflight = 'raw-500'
for (const theme of ['dark', 'light']) {
  const { page, card } = await openCard(theme)
  const text = await card.innerText()
  if (!/Server got itself in trouble/.test(text)) fail(`${theme}: the raw 500 must reach the notice`)
  if (BEFORE) {
    if (!/Could not reach AWS with this profile\. Check your credentials, then re-check\./.test(text)) {
      fail(`${theme}: main blames credentials under the raw 500`)
    }
  } else if (/Check your credentials/.test(text)) {
    fail(`${theme}: a check that failed to run must not blame credentials`)
  }
  await shoot(page, card, `02-raw-500-${BEFORE ? 'before' : 'after'}-${theme}.png`)
  await page.close()
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
