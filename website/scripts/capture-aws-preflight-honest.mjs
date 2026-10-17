/**
 * Screenshot harness for the Remote Crew set-up pre-flight's two honest failure rows.
 *
 *  1. The AWS credentials row when the profile's credential_process helper is not on
 *     the gateway's PATH: the row names the helper and says to give it a full path,
 *     with the AWS CLI's own error underneath on its own monospace line.
 *  2. The Session Manager plugin row when the plugin is installed but the provenance
 *     check refused it, and AWS's package tree is owned by a third account: the row
 *     says where it was found and why, links AWS's install page for the reinstall it
 *     asks for, and the copyable command is the chown that clears it, not the install
 *     command.
 *  3. The same refusal with no known fix: the reason, and no command.
 *
 * Each row shows one warning triangle: its own status circle.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures. It does not show the gateway's own
 * probe of a real install; the backend tests cover that.
 *
 * Every claim is ASSERTED before its PNG is written.
 *
 * Usage: npm run build && node scripts/capture-aws-preflight-honest.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/aws-preflight-honest'
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

/** The AWS CLI's own words for a credential_process helper it cannot exec. */
const HELPER_MISSING = {
  ...PREFLIGHT_OK,
  reachable: false,
  account: '',
  arn: '',
  ec2_reachable: false,
  cloudformation_reachable: false,
  ssm_reachable: false,
  detail: "aws: [ERROR]: [Errno 2] No such file or directory: 'sso-cred-helper'",
  credential_helper_missing: 'sso-cred-helper',
}

/** Verbatim reason shape from validate_provider_executable for AWS's macOS pkg. */
const PLUGIN_REFUSED_FIX = {
  ...PREFLIGHT_OK,
  session_manager_plugin: false,
  session_manager_plugin_command: 'sudo chown -R root:wheel /usr/local/sessionmanagerplugin',
  session_manager_plugin_refused_path: '/usr/local/sessionmanagerplugin/bin/session-manager-plugin',
  session_manager_plugin_refused_reason: 'executable parent is owned by another user (uid 1501)',
}

const PLUGIN_REFUSED_NO_FIX = {
  ...PLUGIN_REFUSED_FIX,
  session_manager_plugin_command: '',
  session_manager_plugin_refused_reason: 'executable is owned by another user (uid 502)',
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

const world = { preflight: PREFLIGHT_OK }

const extra = async (path, route) => {
  if (path === '/api/instances') return json(route, { active: true, warm_set_cap: 5, instances: [] }), true
  if (path === '/api/cloud/launch') return json(route, { jobs: [] }), true
  if (path === '/api/cloud/provisioners') return json(route, { provisioners: [AWS_EC2_ROW] }), true
  if (path.startsWith('/api/cloud/preflight')) return json(route, world.preflight), true
  if (path === '/api/cloud/identity') {
    return json(route, { identity: null, suggested_target: null, discovery: 'read' }), true
  }
  if (path === '/api/cloud/iam-policy') return json(route, { policy: '{}' }), true
  return false
}

let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const INSTALL_URL =
  'https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html'

/** The row's warning circle marks the failed check; the notice inside draws no second triangle. */
const oneTriangle = async (theme, row) => {
  const n = await row.locator('.lucide-triangle-alert').count()
  if (n !== 1) fail(`${theme}: the row must show one warning triangle, not ${n}`)
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const VIEW = { width: 1220, height: 940 }
const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })

/** The pre-flight row whose title is `title`, once the check has answered. */
const openRow = async (theme, title) => {
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /Set up a new one/i }).click()
  const row = page.locator('li').filter({ has: page.getByText(title, { exact: true }) })
  await row.first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  return { page, row: row.first() }
}

const clipOf = async (page, locator, pad = 12) => {
  await locator.scrollIntoViewIfNeeded()
  await page.waitForTimeout(250)
  const box = await locator.boundingBox()
  const x = Math.max(0, box.x - pad)
  const y = Math.max(0, box.y - pad)
  return {
    x,
    y,
    width: Math.min(VIEW.width - x, box.width + 2 * pad),
    height: Math.min(VIEW.height - y, box.height + 2 * pad),
  }
}

// ---- Frame 1: the credential_process helper is not on the gateway's PATH ----

world.preflight = HELPER_MISSING
for (const theme of ['dark', 'light']) {
  const { page, row } = await openRow(theme, 'AWS credentials')
  const text = await row.innerText()
  if (!/could not find sso-cred-helper on the gateway’s PATH/.test(text)) {
    fail(`${theme}: the row must name the missing helper: ${JSON.stringify(text)}`)
  }
  if (!/full path in the credential_process line/.test(text)) {
    fail(`${theme}: the row must say how to fix it: ${JSON.stringify(text)}`)
  }
  if (!/No such file or directory: 'sso-cred-helper'/.test(text)) {
    fail(`${theme}: the AWS CLI's own error must stay under the sentence`)
  }
  // The sentence leads; the CLI's error is its own smaller monospace line.
  const raw = row.locator('.font-mono', { hasText: "No such file or directory: 'sso-cred-helper'" })
  if ((await raw.count()) !== 1) fail(`${theme}: the CLI's error must be a separate monospace line`)
  else if (/could not find sso-cred-helper/.test(await raw.innerText())) {
    fail(`${theme}: the sentence must not run on into the CLI's error line`)
  }
  await oneTriangle(theme, row)
  await page.screenshot({ path: `${OUT}/01-credential-helper-missing-${theme}.png`, clip: await clipOf(page, row) })
  console.log(`wrote 01 (${theme})`)
  await page.close()
}

// ---- Frame 2: the plugin is installed but refused, with the known fix --------

world.preflight = PLUGIN_REFUSED_FIX
for (const theme of ['dark', 'light']) {
  const { page, row } = await openRow(theme, 'Session Manager plugin')
  const text = await row.innerText()
  if (/Not installed/.test(text)) fail(`${theme}: a refused plugin must not read as not installed`)
  if (!/Found at \/usr\/local\/sessionmanagerplugin\/bin\/session-manager-plugin/.test(text)) {
    fail(`${theme}: the row must say where the plugin was found: ${JSON.stringify(text)}`)
  }
  if (!/owned by another user \(uid 1501\)/.test(text)) fail(`${theme}: the row must give the reason`)
  if (!/reinstall it from AWS first\. Then run the command below/.test(text)) {
    fail(`${theme}: the row must ask for AWS provenance before the ownership command`)
  }
  if (!/sudo chown -R root:wheel \/usr\/local\/sessionmanagerplugin/.test(text)) {
    fail(`${theme}: the copyable command must be the chown that clears the refusal`)
  }
  if (/brew install/.test(text)) fail(`${theme}: the install command cannot fix a refusal`)
  if (!(await row.getByRole('button', { name: /Copy command/i }).isVisible())) {
    fail(`${theme}: the fix must be copyable`)
  }
  const install = row.getByRole('link', { name: /Open AWS’s Session Manager plugin install page/ })
  if (!(await install.isVisible())) fail(`${theme}: "reinstall it from AWS first" must link AWS's install page`)
  else if ((await install.getAttribute('href')) !== INSTALL_URL) fail(`${theme}: the link must open AWS's install page`)
  await oneTriangle(theme, row)
  await page.screenshot({ path: `${OUT}/02-plugin-refused-with-fix-${theme}.png`, clip: await clipOf(page, row) })
  console.log(`wrote 02 (${theme})`)
  await page.close()
}

// ---- Frame 3: refused, no known command --------------------------------------

world.preflight = PLUGIN_REFUSED_NO_FIX
for (const theme of ['dark', 'light']) {
  const { page, row } = await openRow(theme, 'Session Manager plugin')
  const text = await row.innerText()
  if (!/owned by another user \(uid 502\)/.test(text)) fail(`${theme}: the row must give the reason`)
  if (!/Fix the file’s ownership or permissions/.test(text)) fail(`${theme}: the row must name the remedy`)
  if (await row.getByRole('button', { name: /Copy command/i }).count()) {
    fail(`${theme}: no Copy button with no command to copy`)
  }
  await oneTriangle(theme, row)
  await page.screenshot({ path: `${OUT}/03-plugin-refused-no-fix-${theme}.png`, clip: await clipOf(page, row) })
  console.log(`wrote 03 (${theme})`)
  await page.close()
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
