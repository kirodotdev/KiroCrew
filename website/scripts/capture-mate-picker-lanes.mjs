/**
 * Screenshot harness for the mate deploy flow: the lane chips AND the confirm step.
 *
 * The PR body's picker shots still showed a `Coder` chip that HEAD no longer renders --
 * that lane advertised a provisioner nothing backed and was removed (First Principles
 * item 10) -- and the confirm shots showed the old billing line with no cost figure. This
 * recaptures both so the body shows what ships: a picker with Fargate and no Coder, and a
 * confirm step whose billing line carries the bounded hourly figure. Each claim is
 * ASSERTED before its PNG is written, so a stale build cannot hand the PR a picture of the
 * removed chip or the number-less copy again.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server with
 * every /api/** call answered from fixtures -- no gateway, no AWS, no kiro-cli.
 *
 * Usage: npm run build && node scripts/capture-mate-picker-lanes.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mate-picker-lanes'
mkdirSync(OUT, { recursive: true })

// A Fargate mate lane, pinned to the mate we will pick so its chip is enabled.
const FARGATE_ROW = {
  id: 'aws_fargate',
  kind: 'aws_fargate',
  label: 'AWS Fargate in your own account',
  posix_only: false,
  serves_mate: 'demo',
  confirm_before_launch: 'kirocrew/crew/demo/KIRO_IDENTITY',
  steps: [{ key: 'provision', label: 'Run the task' }],
}
const EC2_ROW = {
  id: 'aws_ec2',
  kind: 'aws_ec2',
  label: 'AWS EC2 in your own account',
  posix_only: true,
  steps: [{ key: 'provision', label: 'Create the instance' }],
}

const MEMBERS = {
  members: [
    { name: 'demo', slug: 'demo', display_name: '', avatar: '' },
    { name: 'orchard-sde', slug: 'orchard-sde', display_name: '', avatar: '' },
  ],
}

const extra = async (path, route) => {
  if (path === '/api/instances') return json(route, { active: true, warm_set_cap: 5, instances: [] }), true
  if (path === '/api/cloud/launch') return json(route, { jobs: [] }), true
  if (path === '/api/cloud/provisioners') return json(route, { provisioners: [EC2_ROW, FARGATE_ROW] }), true
  if (path === '/api/members') return json(route, MEMBERS), true
  return false
}

let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1220, height: 940 }, deviceScaleFactor: 2 })

try {
  for (const theme of ['dark', 'light']) {
    const page = await context.newPage()
    await stubDashboardApi(page, { extra, theme })
    logPageProblems(page)

    await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: /Remote mates/i }).click()
    await page.getByTestId('deploy-mate-open').click()

    const picker = page.getByTestId('deploy-mate-picker')
    await picker.waitFor({ state: 'visible' })
    await page.getByRole('option', { name: 'demo', exact: true }).click()

    // The lane strip must have rendered (Fargate chip present) and must NOT carry a
    // Coder chip, nor the removed "server URL, a token and a template name" sentence.
    await page.getByRole('button', { name: /Fargate/ }).first().waitFor({ state: 'visible' })
    if (await page.getByText(/Coder/i).count()) {
      fail(`${theme}: a "Coder" chip is still rendered in the mate picker`)
    }
    if (await page.getByText(/server URL, a token and a template name/i).count()) {
      fail(`${theme}: the removed Coder "server URL, a token and a template name" sentence is still shown`)
    }

    await page.screenshot({ path: `${OUT}/mate-picker-lanes-${theme}.png`, clip: await picker.boundingBox() })

    // The confirm step, where the billing line lives. Advance past the picker.
    await page.getByTestId('deploy-mate-continue').click()
    const confirm = page.getByTestId('deploy-mate-confirm')
    await confirm.waitFor({ state: 'visible' })

    // The new bounded hourly figure must be on screen, and the old number-less copy gone.
    if (!(await page.getByText(/roughly \$0\.35–0\.45 per hour/i).count())) {
      fail(`${theme}: the confirm step does not show the new hourly figure`)
    }
    await page.screenshot({ path: `${OUT}/mate-confirm-billing-${theme}.png`, clip: await confirm.boundingBox() })

    await page.close()
  }
} finally {
  await context.close()
  await browser.close()
  srv.close()
}

if (failures) {
  console.error(`\n${failures} assertion(s) failed — screenshots are NOT trustworthy evidence.`)
  process.exit(1)
}
console.log(`OK: mate picker (no Coder chip) and confirm step (with hourly figure) recaptured under ${OUT}`)
