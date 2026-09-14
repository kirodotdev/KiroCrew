/**
 * Evidence for the two Spot surfaces a user actually meets in the dashboard:
 *
 *   1. A destroy whose Spot sweep could NOT finish. The gateway answers 200
 *      (the delete WAS accepted) with `warnings` — still-live resources only the
 *      user can clear — and `notices` — "nothing proves it either way". Those are
 *      different claims, so they get different treatments: warnings keep the
 *      amber block, notices take the panel's neutral note tone. The runnable
 *      `aws` command at the tail of each line renders as selectable code with a
 *      copy button, because it is a command the user has to run BECAUSE the
 *      gateway could not, and a mistyped --spot-instance-request-ids leaves the
 *      request live, handing out replacement instances that keep billing. The
 *      raw AWS failure in the middle of the line — the longest part, and the
 *      only one nobody here wrote — is muted, so the ids and the command keep
 *      the weight.
 *
 *   2. Start failing on a --spot crew. This is THE Spot event every user
 *      eventually hits (an EC2 interruption stop), and raw it reads like a
 *      broken box — whose only visible next affordance in this panel is Delete,
 *      which deletes the root volume the interruption deliberately preserved.
 *      The gateway appends the hint to the 502's `error` (the only field this
 *      client unwraps), and the panel splits it back off: the AWS failure stays
 *      in the error banner, the hint's sentences take the neutral note block one
 *      per line — "do NOT destroy this to fix it" has to be a line, not the tail
 *      of a paragraph sitting next to the Delete button.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered from a fixture — no gateway, no AWS, no token.
 *
 * Usage: node scripts/capture-cloud-spot-remedies.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/cloud-spot-destroy-warnings'
const PROJECT = '/home/user/workspace/KiroCrew'
mkdirSync(OUT, { recursive: true })

const TAG = 'kc-3f9a'
const INSTANCE_ID = 'i-0abc123456789def0'

const INSTANCES = {
  active: true,
  warm_set_cap: 5,
  sso: { configured: false },
  instances: [
    {
      id: 'kc1',
      name: `Kiro Crew Cloud (${TAG})`,
      connection_method: 'ssm',
      ssm_target: INSTANCE_ID,
      ssh_host: '',
      aws_profile: 'dev',
      aws_region: 'us-east-1',
      ssm_run_as: '',
      remote_port: 5476,
      local_port: 0,
      ttl: '20h',
      remote_bin: '',
      was_connected: true,
      status: { instance_id: INSTANCE_ID, state: 'connected' },
    },
  ],
}
const LAUNCHES = {
  jobs: [
    {
      id: 'j-done', tag: TAG, instance_id: INSTANCE_ID, profile: 'dev', region: 'us-east-1',
      size_key: 'balanced', status: 'done', steps: [], signin: null,
      created_at: 0, updated_at: 0,
    },
  ],
}

// The exact strings `ec2.grade_spot_sweep` produces, flattened the way
// `handlers_cloud._spot_sweep_report` flattens them: summary, then the AWS
// error, then the runnable remedy LAST.
const DESTROY_WARNINGS = [
  "Could NOT cancel this tag's persistent Spot request(s): sir-0f2c9a1b4e77d3c85 " +
    // Verbatim `str(AWSError)`, action prefix included (cloud.aws.checked builds
    // it) — that prefix is what the panel mutes on, so dropping it here would
    // shoot evidence of a rendering the real gateway never produces.
    'ec2:CancelSpotInstanceRequests failed: An error occurred (UnauthorizedOperation) ' +
    'when calling the CancelSpotInstanceRequests operation: You are not authorized to ' +
    'perform this operation. ' +
    'Cancel them yourself or EC2 keeps launching replacements: ' +
    'aws ec2 cancel-spot-instance-requests --spot-instance-request-ids ' +
    'sir-0f2c9a1b4e77d3c85 --profile dev --region us-east-1',
]
const DESTROY_NOTICES = [
  'Could not check for a leftover Spot request (no permission) — and with no stack left ' +
    'there is nothing to prove it either way; check the EC2 console if this tag ever ran --spot.',
]

// What `POST /api/cloud/{tag}/start` answers for an interruption-stopped Spot
// box: the raw AWS error, then ec2.spot_start_failure_hint appended to it.
const START_ERROR =
  'ec2:StartInstances failed: An error occurred (IncorrectSpotRequestState) when calling ' +
  'the StartInstances operation: Only Amazon EC2 can restart an interrupted stopped ' +
  'Spot Instance. ' +
  'This crew was launched with --spot, so the most likely cause is an EC2 INTERRUPTION ' +
  'stop, not a broken instance. ' +
  'Only EC2 can restart an interruption-stopped Spot instance. It resumes on its own when ' +
  'Spot capacity comes back — there is nothing to fix here. ' +
  'Your data is intact: an interruption stops the instance, it does not terminate it, so ' +
  'the root volume (and ~/.kiro/crew on it) is untouched. ' +
  "Do NOT destroy the instance to 'fix' this — destroy deletes that volume and everything " +
  'on it. Wait for the auto-resume, or check `kirocrew cloud status`.'

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const fixedApi = makeFixedApi(PROJECT)

/** One themed pass: open the crews tab, drive *scene*, shoot it. */
async function shoot(theme, name, scene) {
  const page = await browser.newPage({ viewport: { width: 1000, height: 900 } })
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/instances') return json(route, INSTANCES)
    if (path === '/api/cloud/launch') return json(route, LAUNCHES)
    if (path === `/api/cloud/${TAG}`) {
      return json(route, {
        destroyed: true, cleanup: 'pending',
        warnings: DESTROY_WARNINGS, notices: DESTROY_NOTICES,
      })
    }
    if (path === `/api/cloud/${TAG}/start`) {
      return json(route, { error: START_ERROR, code: 'aws_call_failed' }, 502)
    }
    return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi })
  })
  await page.addInitScript(t => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
  }, theme)

  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /More actions/i }).first().waitFor({ timeout: 20000 })
  await scene(page)
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: true })
  console.log(`wrote ${OUT}/${name}`)
  await page.close()
}

const destroyScene = async page => {
  await page.getByRole('button', { name: /More actions/i }).first().click()
  await page.getByRole('menuitem', { name: /^Delete Kiro Crew Cloud/i }).click()
  await page.getByRole('button', { name: /^Confirm deleting/i }).click()
  await page.getByText(/Could NOT cancel/i).waitFor({ timeout: 20000 })
}

const startScene = async page => {
  await page.getByRole('button', { name: /More actions/i }).first().click()
  await page.getByRole('menuitem', { name: /^Start Kiro Crew Cloud/i }).click()
  await page.getByText(/Do NOT destroy the instance/i).waitFor({ timeout: 20000 })
}

await shoot('dark', 'destroy-warnings-dark.png', destroyScene)
await shoot('light', 'destroy-warnings-light.png', destroyScene)
await shoot('dark', 'start-interrupted-dark.png', startScene)
await shoot('light', 'start-interrupted-light.png', startScene)

await browser.close()
srv.close()
