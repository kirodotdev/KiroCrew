/**
 * Video harness for the crews tab's launcher expand/collapse disclosure.
 *
 * The UX review could not evaluate the toggle because the recording it needed did
 * not exist: a screen reader met a button whose expanded/collapsed state pointed at
 * nothing it could place. The fix gave the toggle `aria-controls` and the disclosed
 * form a named `region`; this records the visible half of that pair so the review has
 * a moving picture of one control opening and closing one region.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures -- no gateway, no AWS, no kiro-cli.
 * The launcher's own preflight is stubbed reachable so the form renders its full
 * body rather than an error notice.
 *
 * Usage: npm run build && node scripts/capture-crew-launcher-disclosure.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-launcher-disclosure'
mkdirSync(OUT, { recursive: true })

const PREFLIGHT_OK = {
  reachable: true,
  account: '1234•••7890',
  arn: 'arn:aws:iam::123456787890:user/dev',
  ec2_reachable: true,
  cloudformation_reachable: true,
  ssm_reachable: true,
  session_manager_plugin: true,
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
    { key: 'provision', label: 'Create the instance and install Kiro Crew', state: 'pending', detail: '' },
    { key: 'signin', label: 'Sign in to Kiro', state: 'pending', detail: '' },
    { key: 'connect', label: 'Connect', state: 'pending', detail: '' },
  ],
}

// One connected crew so the crews list is a real card, not the empty state.
const CREW = {
  id: 'nobita',
  name: 'nobita',
  connection_method: 'ssh',
  ssh_host: 'nobita-alias',
  ssm_target: '',
  ssm_run_as: '',
  aws_profile: '',
  aws_region: '',
  provisioner_id: '',
  remote_port: 7777,
  local_port: 7801,
  ttl: '20h',
  remote_bin: '',
  was_connected: true,
  status: { instance_id: 'nobita', state: 'connected', local_port: 7801, remote_port: 7777 },
}

const extra = async (path, route) => {
  if (path === '/api/instances') {
    return json(route, { active: true, warm_set_cap: 5, instances: [CREW] }), true
  }
  if (path === '/api/cloud/launch') return json(route, { jobs: [] }), true
  if (path === '/api/cloud/provisioners') return json(route, { provisioners: [AWS_EC2_ROW] }), true
  if (path.startsWith('/api/cloud/preflight')) return json(route, PREFLIGHT_OK), true
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
const context = await browser.newContext({
  viewport: { width: 1220, height: 940 },
  deviceScaleFactor: 1,
  recordVideo: { dir: OUT, size: { width: 1220, height: 940 } },
})

try {
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme: 'dark' })
  logPageProblems(page)

  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })

  const toggle = page.getByTestId('deploy-crew-open')
  await toggle.waitFor({ state: 'visible' })

  // Collapsed to start: the button governs the region, and the region is absent.
  if ((await toggle.getAttribute('aria-expanded')) !== 'false') {
    fail('launcher toggle did not start collapsed (aria-expanded=false)')
  }
  if ((await toggle.getAttribute('aria-controls')) !== 'crew-launcher-panel') {
    fail('launcher toggle is missing aria-controls=crew-launcher-panel')
  }
  if (await page.locator('#crew-launcher-panel').count()) {
    fail('the launcher region rendered while the toggle was collapsed')
  }
  await page.waitForTimeout(900)

  // Expand: one click, region appears, state flips.
  await toggle.click()
  const region = page.locator('#crew-launcher-panel')
  await region.waitFor({ state: 'visible' })
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') {
    fail('launcher toggle did not report aria-expanded=true after opening')
  }
  if ((await region.getAttribute('role')) !== 'region') {
    fail('the disclosed launcher is not a region landmark')
  }
  await page.waitForTimeout(1400)

  // Collapse: the same one control closes the same region.
  await toggle.click()
  await region.waitFor({ state: 'detached' })
  if ((await toggle.getAttribute('aria-expanded')) !== 'false') {
    fail('launcher toggle did not report aria-expanded=false after closing')
  }
  await page.waitForTimeout(900)

  await page.close() // flush the video
} finally {
  await context.close()
  await browser.close()
  srv.close()
}

if (failures) {
  console.error(`\n${failures} assertion(s) failed — recording is NOT trustworthy evidence.`)
  process.exit(1)
}
console.log(`OK: crew-launcher disclosure recorded under ${OUT}`)
