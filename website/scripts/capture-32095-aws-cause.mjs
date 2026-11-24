/**
 * Screenshot harness for #32095: the Remote Crew "Before you start" AWS checklist when the
 * credentials check fails for a cause that is not a permission: no AWS CLI, a profile that
 * does not exist, a Region the account has not enabled.
 *
 * Runs a REAL built SPA behind the shared loopback static server with every /api/** call
 * answered from fixtures; the fixtures carry the fields the gateway's `reachability_check`
 * returns (backend tests cover the classification). Build the SPA of the head you want to
 * shoot, copy `dist` aside, and pass it here: run once on main's build ("before") and once
 * on this branch's ("after"). "after" ASSERTS every claim before its PNG is written.
 *
 * Usage: node scripts/capture-32095-aws-cause.mjs <distDir> <before|after> [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const DIST = process.argv[2]
const MODE = process.argv[3] || 'after'
const OUT = process.argv[4] || `../temp-screenshots/32095-aws-cause/${MODE}`
if (!DIST) throw new Error('usage: capture-32095-aws-cause.mjs <distDir> <before|after> [outDir]')
mkdirSync(OUT, { recursive: true })

const BASE = {
  reachable: false,
  account: '',
  arn: '',
  ec2_reachable: false,
  cloudformation_reachable: false,
  ssm_reachable: false,
  session_manager_plugin: true,
  session_manager_plugin_command: '',
  session_manager_plugin_refused_path: '',
  session_manager_plugin_refused_reason: '',
  session_manager_plugin_refused_code: '',
  session_manager_plugin_refused_owner: '',
  session_manager_plugin_refused_owner_uid: null,
  credential_helper_missing: '',
  note: '',
  detail: '',
  cause: '',
  profile_missing: '',
}

const CASES = [
  {
    file: '01-no-aws-cli',
    profile: '',
    region: '',
    fixture: {
      ...BASE,
      detail: 'sandbox: FATAL — cannot exec aws (No such file or directory)',
      cause: 'cli_missing',
    },
    after: [/The AWS CLI is not installed on the computer that runs/, /Open the AWS CLI install guide/],
  },
  {
    file: '02-unknown-profile',
    profile: 'kirocrew-test',
    region: '',
    fixture: {
      ...BASE,
      detail: 'aws: [ERROR]: The config profile (kirocrew-test) could not be found',
      cause: 'profile_not_found',
      profile_missing: 'kirocrew-test',
    },
    after: [/There is no AWS profile named kirocrew-test on the computer that runs .+aws configure sso --profile kirocrew-test/],
  },
  {
    file: '03-region-not-enabled',
    profile: '',
    region: 'af-south-1',
    fixture: {
      ...BASE,
      detail:
        'aws: [ERROR]: An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation: The security token included in the request is invalid',
      cause: 'region_not_enabled',
    },
    after: [/Region af-south-1 is not enabled for this AWS account\. Pick another Region, or enable af-south-1/],
  },
]

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

const world = { preflight: BASE }
const extra = async (path, route) => {
  if (path === '/api/instances') return json(route, { active: true, warm_set_cap: 5, instances: [] }), true
  if (path === '/api/cloud/launch') return json(route, { jobs: [] }), true
  if (path === '/api/cloud/provisioners') return json(route, { provisioners: [AWS_EC2_ROW] }), true
  if (path.startsWith('/api/cloud/preflight')) return json(route, world.preflight), true
  if (path === '/api/cloud/identity') return json(route, { identity: null, suggested_target: null, discovery: 'read' }), true
  if (path === '/api/cloud/iam-policy') return json(route, { policy: '{}' }), true
  return false
}

let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
const VIEW = { width: 1220, height: 1100 }
const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })

for (const theme of ['dark', 'light']) {
  for (const c of CASES) {
    world.preflight = c.fixture
    const page = await context.newPage()
    await stubDashboardApi(page, { extra, theme })
    logPageProblems(page)
    await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: /Add Crew/ }).first().click()
    await page.getByRole('dialog').getByText('Set up a new Crew', { exact: true }).click()
    if (c.profile) await page.getByRole('textbox', { name: 'AWS profile' }).fill(c.profile)
    if (c.region) await page.getByRole('textbox', { name: /^Region/ }).first().fill(c.region)
    const recheck = page.getByRole('button', { name: /Re-check/ }).first()
    await recheck.waitFor({ timeout: 20000 })
    if (c.profile || c.region) await recheck.click()
    const row = page.locator('li').filter({ has: page.getByText('AWS credentials', { exact: true }) }).first()
    await row.waitFor({ timeout: 20000 })
    await page.waitForTimeout(800)
    const list = row.locator('xpath=..')
    const text = await list.innerText()
    if (MODE === 'after') {
      for (const want of c.after) {
        if (!want.test(text)) fail(`${theme} ${c.file}: expected ${want} in ${JSON.stringify(text)}`)
      }
      if (!text.includes(c.fixture.detail)) fail(`${theme} ${c.file}: the CLI's own text must stay as a detail`)
      if (/Copy policy JSON/.test(text)) fail(`${theme} ${c.file}: no policy to copy`)
      if (/needs permission for this service/.test(text)) fail(`${theme} ${c.file}: no row may blame a permission`)
    }
    await list.scrollIntoViewIfNeeded()
    await page.waitForTimeout(200)
    const box = await list.boundingBox()
    const pad = 12
    const x = Math.max(0, box.x - pad)
    const y = Math.max(0, box.y - pad)
    await page.screenshot({
      path: `${OUT}/${c.file}-${theme}.png`,
      clip: { x, y, width: Math.min(VIEW.width - x, box.width + 2 * pad), height: Math.min(VIEW.height - y, box.height + 2 * pad) },
    })
    console.log(`wrote ${c.file} (${theme}, ${MODE})`)
    await page.close()
  }
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
