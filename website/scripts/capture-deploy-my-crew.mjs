/**
 * Screenshot harness for the Crew Members page's "Your crew in the cloud"
 * panel (DeployMyCrew.tsx) and its labelled header trigger.
 *
 * The panel answers one question -- "is my crew deployed, and what do I do
 * next?" -- with the crew's faces, one sentence, and at most one button. A
 * render test can pin that DOM; it cannot show whether those parts compose
 * readably, which is the exact thing the redesign exists to fix. These frames
 * are that evidence, one per state the panel can be in:
 *
 *   01-trigger          the Members page header with the labelled Cloud control
 *   02-none             no launch on record: one sentence, one button
 *   03-deploying        a moving launch: Step N of M, no button, no estimate
 *   04-signin           a launch holding for the reader's sign-in: one button
 *   05-deployed         since-deploy, region, the Session Manager target with
 *                       its Copy button, Details collapsed
 *   06-deployed-details the same launch with Details expanded (raw identifiers)
 *   07-task-running     a Fargate launch whose task ECS reports RUNNING: the
 *                       lane's own view, since-deploy and region, cluster and
 *                       task id with the ARN to copy, the read instant with
 *                       Check again, and the task's ECS console page
 *   08-failed           the recorded error through ErrorNotice, one button
 *   09-loading          the list is still being read
 *   10-read-failed      the list could not be read: error and Try again
 *   11-copy-failed      both clipboard layers refused: the row says so, no tick, hand-off on
 *   12-copied           the copy landed: the button reads Copied, no notice
 *   13-failed-over-live a failed retry over an earlier launch that still runs: the line says so
 *   14-finished-gone    a finished EC2 launch whose machine left the registry
 *   15-finished-unknown the Instances feature is off: the panel says it cannot tell
 *   16-deployed-over-live a second registered machine behind the deployed headline
 *   17-registry-failed  the registry read failed: an error, not a state
 *   18-task-stopped     ECS reports the task STOPPED: its own reason and when, the set-up door
 *   19-task-missing     ECS no longer lists the task: said as exactly that
 *   20-task-unknown     the task read failed: could not read, Try again, never a state
 *   21-task-starting    a Fargate launch still moving: Step N of M, no task read
 *   22-task-failed      the Fargate launch failed before a task existed: error, hand-off, set-up door
 *   23-task-pending     ECS reports the task PENDING: stats, Check again, no claim of running
 *   24-task-stopping    ECS accepted a stop before lastStatus caught up: stopping, not running
 *   25-task-other       a lifecycle word the panel does not know, shown verbatim
 *   26-task-loading     the task read is still in flight: no state, no error
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures -- no gateway, no AWS.
 * Every claim is ASSERTED before its PNG is written: a harness that only writes
 * PNGs can hand a PR a picture of an error boundary as evidence.
 *
 * Usage: npm run build && node scripts/capture-deploy-my-crew.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { MEMBERS } from './lib/members-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/deploy-my-crew'
mkdirSync(OUT, { recursive: true })

const NOW_SEC = Math.floor(Date.now() / 1000)

const STEPS_DONE = [
  { key: 'preflight', label: 'Check your AWS setup', state: 'done', detail: '' },
  { key: 'provision', label: 'Create the instance and install Kiro Crew', state: 'done', detail: 'i-0abc123456789def0' },
  { key: 'signin', label: 'Sign in to Kiro', state: 'done', detail: '' },
  { key: 'connect', label: 'Connect', state: 'done', detail: 'Added to Your crews.' },
]

/** A launch that finished on the built-in EC2 lane three hours ago. */
const DEPLOYED_JOB = {
  id: 'j-deployed',
  provider_id: 'aws_ec2',
  tag: 'kc-5e10bb',
  instance_id: 'i-0abc123456789def0',
  profile: 'default',
  region: 'us-east-1',
  size_key: 'balanced',
  status: 'done',
  steps: STEPS_DONE,
  signin: null,
  signin_detected: true,
  error: '',
  created_at: NOW_SEC - 3 * 3600 - 12 * 60,
  updated_at: NOW_SEC - 3 * 3600,
}

/** The same crew, still on step 2 of 4. */
const DEPLOYING_JOB = {
  ...DEPLOYED_JOB,
  id: 'j-deploying',
  instance_id: '',
  status: 'running',
  steps: [
    { ...STEPS_DONE[0] },
    { ...STEPS_DONE[1], state: 'active', detail: '' },
    { ...STEPS_DONE[2], state: 'pending' },
    { ...STEPS_DONE[3], state: 'pending' },
  ],
  created_at: NOW_SEC - 90,
  updated_at: NOW_SEC - 5,
}

/** Holding for the reader to approve a device code in Settings. */
const SIGNIN_JOB = {
  ...DEPLOYED_JOB,
  id: 'j-signin',
  status: 'awaiting_signin',
  steps: STEPS_DONE.map((s) => (s.key === 'signin' ? { ...s, state: 'active' } : s.key === 'connect' ? { ...s, state: 'pending', detail: '' } : s)),
  signin: { url: 'https://example.invalid/device', code: 'GFXK-MNKS', ports: [] },
  signin_detected: false,
  created_at: NOW_SEC - 6 * 60,
  updated_at: NOW_SEC - 20,
}

/** A finished Fargate launch: `instance_id` carries the task ARN. */
const FARGATE_JOB = {
  ...DEPLOYED_JOB,
  id: 'j-fargate',
  provider_id: 'aws_fargate',
  tag: 'kc-7a21cd',
  instance_id: 'arn:aws:ecs:us-east-1:123456787890:task/kc-7a21cd/8f3e2a1b9c0d4e5f6a7b8c9d0e1f2a3b',
  created_at: NOW_SEC - 2 * 86400 - 5 * 3600,
  updated_at: NOW_SEC - 2 * 86400,
}

/** A launch that died in provisioning, with the gateway's own sentence. */
const FAILED_JOB = {
  ...DEPLOYED_JOB,
  id: 'j-failed',
  instance_id: '',
  status: 'failed',
  steps: [
    { ...STEPS_DONE[0] },
    { ...STEPS_DONE[1], state: 'error', detail: '' },
    { ...STEPS_DONE[2], state: 'pending', detail: '' },
    { ...STEPS_DONE[3], state: 'pending', detail: '' },
  ],
  error: 'CloudFormation stack kc-5e10bb rolled back: VcpuLimitExceeded in us-east-1.',
  created_at: NOW_SEC - 40 * 60,
  updated_at: NOW_SEC - 32 * 60,
}

/**
 * The fixture world, switched per frame. `jobs` decides the panel's state;
 * `launchAnswer` overrides the whole list response (a 500, or a hold that
 * never answers, for the loading and read-failed frames).
 */
const world = { jobs: [], launchAnswer: null, instances: [], instancesAnswer: null, taskAnswer: null }

/** What ECS says about FARGATE_JOB's task, as `GET /api/cloud/launch/{id}/task`
 *  reports it: the running case; the other frames override fields. */
const TASK_RUNNING = {
  job_id: FARGATE_JOB.id,
  task_arn: FARGATE_JOB.instance_id,
  read_at: NOW_SEC - 4,
  task: {
    task_arn: FARGATE_JOB.instance_id,
    cluster: 'kc-7a21cd',
    task_id: '8f3e2a1b9c0d4e5f6a7b8c9d0e1f2a3b',
    last_status: 'RUNNING',
    desired_status: 'RUNNING',
    started_at: FARGATE_JOB.created_at + 40,
    stopped_at: null,
    stopped_reason: '',
  },
}

/** The registry row the EC2 launcher writes for DEPLOYED_JOB: its instance id
 *  as the SSM target. A Settings delete removes this row once AWS confirms the
 *  stack is gone, and that removal is what the panel reads liveness from. */
const REGISTERED = [{
  id: 'inst-5e10bb', name: 'Kiro Crew Cloud (kc-5e10bb)', ssh_host: '', remote_port: 7777, local_port: 0,
  ttl: '', remote_bin: '', connection_method: 'ssm', ssm_target: DEPLOYED_JOB.instance_id,
  aws_profile: 'default', aws_region: 'us-east-1', ssm_run_as: '', provisioner_id: 'aws_ec2',
  was_connected: true, status: 'idle',
}]

const extra = async (path, route) => {
  if (path === '/api/members') return json(route, { members: MEMBERS, default_agent: 'kirocrew' }), true
  if (path === '/api/crons') return json(route, { jobs: [] }), true
  if (path === '/api/webhooks') return json(route, { tokens: [] }), true
  if (path === '/api/agents') return json(route, { agents: [], default_agent: 'kirocrew' }), true
  if (path === '/api/instances') {
    if (world.instancesAnswer) return json(route, world.instancesAnswer.body, world.instancesAnswer.status), true
    return json(route, { instances: world.instances, active: true, warm_set_cap: 0, sso: { state: 'none' } }), true
  }
  if (path === '/api/cloud/launch') {
    if (world.launchAnswer === 'hold') return true // never fulfilled: the read stays in flight
    if (world.launchAnswer) return json(route, world.launchAnswer.body, world.launchAnswer.status), true
    return json(route, { jobs: world.jobs }), true
  }
  if (/^\/api\/cloud\/launch\/[^/]+\/task$/.test(path)) {
    // The Fargate lane's own read. Answered from the fixture world, never from
    // ECS: a 502 here is the "could not read" frame, a null task the "no longer
    // listed" one.
    if (world.taskAnswer === 'hold') return true // never fulfilled: the read stays in flight
    if (world.taskAnswer) return json(route, world.taskAnswer.body, world.taskAnswer.status), true
    return json(route, TASK_RUNNING), true
  }
  const thread = /^\/api\/members\/([^/]+)\/thread$/.exec(path)
  if (thread) {
    const slug = decodeURIComponent(thread[1])
    return json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false }), true
  }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) {
    return json(route, { slug: 'radar', member: 'radar', capped: false, entries: [] }), true
  }
  if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
    return json(route, { key: 'member-radar', title: 'radar', running: false, messages: [] }), true
  }
  return false
}

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const VIEW = { width: 1220, height: 900 }
const context = await browser.newContext({
  viewport: VIEW,
  deviceScaleFactor: 2, // the 11px hint copy renders soft at 1x on GitHub
})

/** A Members page on the current fixture world, in the given theme. */
const openMembers = async (theme) => {
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  await page.goto(`${base}/members`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-deploy-open').waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  return page
}

/** Open the panel and wait for the state it should land in. */
const openPanel = async (page, stateTestId) => {
  await page.getByTestId('member-deploy-open').click()
  await page.getByTestId(stateTestId).waitFor({ timeout: 20000 })
  await page.waitForTimeout(350)
  return page.getByRole('dialog')
}

/** A clip around one element, clamped to the viewport. */
const clipOf = async (locator, pad = 14) => {
  const box = await locator.boundingBox()
  const x = Math.max(0, Math.min(box.x - pad, VIEW.width - 1))
  const y = Math.max(0, Math.min(box.y - pad, VIEW.height - 1))
  return {
    x,
    y,
    width: Math.max(1, Math.min(VIEW.width - x, box.width + 2 * pad)),
    height: Math.max(1, Math.min(VIEW.height - y, box.height + 2 * pad)),
  }
}

/** The one-button invariant: at most one action besides Close and Copy. */
const countActions = async (dialog) => {
  const names = await dialog.locator('button').evaluateAll((els) =>
    els.map((el) => (el.textContent || '').trim()).filter(Boolean),
  )
  return names.filter((n) => !/^Close$/.test(n) && !/^(Copy|Copied)$/.test(n))
}

/** The faces row must lead: it sits above the state sentence. */
const facesLead = async (dialog, stateTestId) => {
  const faces = await dialog.getByTestId('deploy-faces').boundingBox()
  const state = await dialog.getByTestId(stateTestId).boundingBox()
  return !!faces && !!state && faces.y + faces.height <= state.y + 1
}

// ---- Frame 1: the labelled trigger in the Members page header --------------

for (const theme of ['dark', 'light']) {
  const page = await openMembers(theme)
  const trigger = page.getByTestId('member-deploy-open')
  const label = (await trigger.innerText()).trim()
  if (label !== 'Cloud') fail(`${theme}: the header trigger must read "Cloud", got ${JSON.stringify(label)}`)
  if (!/Your crew in the cloud/.test((await trigger.getAttribute('title')) || '')) {
    fail(`${theme}: the trigger's title must name the panel it opens`)
  }
  // Bordered, so it reads as a button and not as a status chip beside the
  // page title.
  const border = await trigger.evaluate((el) => {
    const cs = getComputedStyle(el)
    return { style: cs.borderTopStyle, width: parseFloat(cs.borderTopWidth) }
  })
  if (border.style === 'none' || !(border.width > 0)) fail(`${theme}: the trigger must carry a visible border: ${JSON.stringify(border)}`)
  // The full-width header strip the trigger lives in, so the reader sees the
  // control among its neighbours rather than a cropped button.
  const box = await trigger.boundingBox()
  const y = Math.max(0, box.y - 14)
  await page.screenshot({
    path: `${OUT}/01-trigger-${theme}.png`,
    clip: { x: 0, y, width: VIEW.width, height: Math.min(VIEW.height - y, box.height + 60) },
  })
  console.log(`wrote 01 (${theme})`)
  await page.close()
}

// ---- Frame 2: no launch on record ------------------------------------------

world.jobs = []
for (const theme of ['dark', 'light']) {
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-none')
  const text = await dialog.innerText()
  if (!/Right now your crew runs only on this computer\./.test(text)) fail(`${theme}: the no-launch sentence is missing`)
  if (!(await facesLead(dialog, 'deploy-state-none'))) fail(`${theme}: the faces must sit above the sentence`)
  if ((await dialog.getByTestId('deploy-faces').locator('img, [data-testid="deploy-faces-more"], svg, span').count()) < 1) {
    fail(`${theme}: the faces row rendered nothing`)
  }
  const actions = await countActions(dialog)
  if (actions.length !== 1 || actions[0] !== 'Open Remote Crew in Settings') {
    fail(`${theme}: exactly one action, "Open Remote Crew in Settings", got ${JSON.stringify(actions)}`)
  }
  if (await dialog.getByTestId('deploy-details').count()) fail(`${theme}: no Details line without a launch`)
  // The button only opens Settings, and the line under it says so: a reader
  // who takes the label as the act itself does not press it.
  const hint = dialog.getByTestId('deploy-action-hint')
  if (!/Nothing is created until you confirm the steps there\./.test(await hint.innerText())) {
    fail(`${theme}: the line under the deploy button must carry the cost/undo fact: ${JSON.stringify(await hint.innerText())}`)
  }
  const [btnBox, hintBox] = [await dialog.getByTestId('deploy-action-deploy').boundingBox(), await hint.boundingBox()]
  if (!(hintBox.y >= btnBox.y + btnBox.height - 1)) fail(`${theme}: the where-it-leads line sits under the button`)
  await page.screenshot({ path: `${OUT}/02-none-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 02 (${theme})`)
  await page.close()
}

// ---- Frame 3: a launch in motion -------------------------------------------

world.jobs = [DEPLOYING_JOB]
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-deploying')
  const text = await dialog.innerText()
  if (!/Deploying your crew/.test(text)) fail(`${theme}: the deploying sentence is missing`)
  if (!/Step 2 of 4/.test(text)) fail(`${theme}: progress must read "Step 2 of 4": ${JSON.stringify(text)}`)
  // No duration estimate: nothing in the repo measures one.
  if (/minute/i.test(text)) fail(`${theme}: no duration estimate may be shown: ${JSON.stringify(text)}`)
  const actions = await countActions(dialog)
  // Nothing acts on the launch here; the one control is the muted link into
  // Settings, where the launch row and its Cancel live.
  if (actions.length !== 1 || actions[0] !== 'Open Remote Crew in Settings') fail(`${theme}: a moving launch offers only the link into Settings, got ${JSON.stringify(actions)}`)
  if (!(await dialog.getByTestId('deploy-action-manage').isVisible())) fail(`${theme}: the manage link is missing`)
  if (await dialog.getByTestId('deploy-address').count()) fail(`${theme}: no target row while deploying`)
  if (!/Closing this window does not stop the deploy\./.test(text)) fail(`${theme}: the close hint is missing`)
  // The step under way is named beside the counter.
  if (!/Step 2 of 4 \u00b7 Create the instance and install Kiro Crew/.test(text)) fail(`${theme}: the step under way must be named: ${JSON.stringify(text)}`)
  await page.screenshot({ path: `${OUT}/03-deploying-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 03 (${theme})`)
  await page.close()
}

// ---- Frame 4: holding for the sign-in --------------------------------------

world.jobs = [SIGNIN_JOB]
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-signin')
  const text = await dialog.innerText()
  if (!/Your crew is waiting for you to sign in\./.test(text)) fail(`${theme}: the sign-in sentence is missing`)
  const actions = await countActions(dialog)
  if (actions.length !== 1 || actions[0] !== 'Open Remote Crew in Settings to sign in') {
    fail(`${theme}: exactly one action, "Open Remote Crew in Settings to sign in", got ${JSON.stringify(actions)}`)
  }
  if (!/Closing this window does not stop the deploy\./.test(text)) fail(`${theme}: the sign-in state needs the close reassurance too`)
  await page.screenshot({ path: `${OUT}/04-signin-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 04 (${theme})`)
  await page.close()
}

// ---- Frames 5 and 6: deployed, Details collapsed then expanded -------------

world.jobs = [DEPLOYED_JOB]
world.instances = REGISTERED
for (const theme of ['dark', 'light']) {
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-deployed')
  const text = await dialog.innerText()
  if (!/Your crew is deployed in the cloud\./.test(text)) fail(`${theme}: the deployed sentence is missing`)
  // NOW_SEC is read once at start, so the minute may have turned by this frame.
  if (!/Since deploy/.test(text) || !/\b3h 1[2-4]m\b/.test(text)) fail(`${theme}: since-deploy must read about 3h 12m: ${JSON.stringify(text)}`)
  if (!/Region/.test(text) || !/us-east-1/.test(text)) fail(`${theme}: the region stat is missing`)
  // The target row: named for what it is, not "Address".
  const row = dialog.getByTestId('deploy-address')
  if (!(await row.isVisible())) fail(`${theme}: the Session Manager target row must be offered on an EC2 launch`)
  const rowText = await row.innerText()
  if (!/Session Manager target/.test(rowText)) fail(`${theme}: the row must be labelled "Session Manager target": ${JSON.stringify(rowText)}`)
  if (/^Address$/m.test(rowText)) fail(`${theme}: the label must not read "Address"`)
  if (!/i-0abc123456789def0/.test(rowText)) fail(`${theme}: the instance id must be shown in the row`)
  if (!/Paste this ID into AWS Session Manager \(in the AWS console\) to open a terminal on the machine\./.test(rowText)) {
    fail(`${theme}: the hint under the target is missing or stale: ${JSON.stringify(rowText)}`)
  }
  if (await dialog.getByTestId('deploy-task-row').count()) fail(`${theme}: the EC2 view carries no task row`)
  if (await dialog.getByTestId('deploy-earlier-live').count()) fail(`${theme}: no earlier-launch line when the newest launch is the deployed one`)
  if (!(await dialog.getByTestId('deploy-address-copy').isVisible())) fail(`${theme}: the Copy button is missing`)
  const actions = await countActions(dialog)
  if (actions.length !== 0) fail(`${theme}: a deployed crew offers Copy only, got ${JSON.stringify(actions)}`)
  // Details: present, collapsed, and BELOW the target row.
  const details = dialog.getByTestId('deploy-details')
  if (!(await details.isVisible())) fail(`${theme}: the Details line is missing`)
  if (await details.evaluate((el) => el.open)) fail(`${theme}: Details must start collapsed`)
  const [rowBox, detailsBox] = [await row.boundingBox(), await details.boundingBox()]
  if (!(detailsBox.y >= rowBox.y + rowBox.height - 1)) fail(`${theme}: Details must sit below the target row`)
  await page.screenshot({ path: `${OUT}/05-deployed-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 05 (${theme})`)

  if (theme === 'dark') {
    await details.locator('summary').click()
    await page.waitForTimeout(250)
    if (!(await details.evaluate((el) => el.open))) fail(`${theme}: Details must open on click`)
    const line = await dialog.getByTestId('deploy-launch').first().innerText()
    for (const token of ['kc-5e10bb', 'aws_ec2', 'done', 'default', 'us-east-1', 'i-0abc123456789def0']) {
      if (!line.includes(token)) fail(`${theme}: the Details line must carry ${token}: ${JSON.stringify(line)}`)
    }
    await page.screenshot({ path: `${OUT}/06-deployed-details-${theme}.png`, clip: await clipOf(dialog) })
    console.log(`wrote 06 (${theme})`)
  }
  await page.close()
}

// ---- Frame 7: a Fargate launch whose task ECS reports RUNNING -----------------

// The lane's own view. Nothing here comes from the registry (the lane registers
// nothing) and nothing is said that the ECS read did not say: "running" on a
// RUNNING, with the instant of the read beside it.

world.jobs = [FARGATE_JOB]
world.taskAnswer = null
for (const theme of ['dark', 'light']) {
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-running')
  const text = await dialog.innerText()
  if (!/Your crew is running as a container task in us-east-1\./.test(text)) fail(`${theme}: the running sentence is missing: ${JSON.stringify(text)}`)
  // No EC2 vocabulary anywhere in this view.
  if (/Your crew is deployed in the cloud\.|Session Manager|A deploy finished/.test(text)) fail(`${theme}: EC2-lane sentences must not appear on the Fargate view`)
  if (await dialog.getByTestId('deploy-address').count()) fail(`${theme}: a task has no Session Manager target row`)
  // Both stat cards, off the launch record.
  if ((await dialog.getByTestId('deploy-stat-since').innerText()) !== '2d 5h') fail(`${theme}: since-deploy must be the record's age`)
  if ((await dialog.getByTestId('deploy-stat-region').innerText()) !== 'us-east-1') fail(`${theme}: the region stat is missing`)
  // The task as ECS names it, with the full ARN to copy.
  if ((await dialog.getByTestId('deploy-task-cluster').innerText()) !== 'kc-7a21cd') fail(`${theme}: the cluster is missing`)
  if ((await dialog.getByTestId('deploy-task-id').innerText()) !== '8f3e2a1b9c0d4e5f6a7b8c9d0e1f2a3b') fail(`${theme}: the task id is missing`)
  if ((await dialog.getByTestId('deploy-task-arn').innerText()) !== FARGATE_JOB.instance_id) fail(`${theme}: the ARN must be shown whole`)
  // The instant of the read, and the way to read again.
  if (!/As of \d/.test(await dialog.getByTestId('deploy-task-read-at').innerText())) fail(`${theme}: the read instant is missing`)
  if (!(await dialog.getByTestId('deploy-task-check').isVisible())) fail(`${theme}: Check again is missing on a running task`)
  // The console link goes to THIS task's page, in a new tab.
  const link = dialog.getByTestId('deploy-task-console-link')
  const href = await link.getAttribute('href')
  if (href !== 'https://us-east-1.console.aws.amazon.com/ecs/v2/clusters/kc-7a21cd/tasks/8f3e2a1b9c0d4e5f6a7b8c9d0e1f2a3b?region=us-east-1') fail(`${theme}: the console link must open this task: ${href}`)
  if ((await link.getAttribute('target')) !== '_blank') fail(`${theme}: the console link opens a new tab`)
  // One action besides Close and Copy: Check again. No set-up button on a running task.
  const actions = await countActions(dialog)
  if (actions.length !== 1 || !/Check again/.test(actions[0])) fail(`${theme}: a running task offers Check again and nothing else, got ${JSON.stringify(actions)}`)
  if (!(await facesLead(dialog, 'deploy-task-state-running'))) fail(`${theme}: the faces must lead the state sentence`)
  await page.screenshot({ path: `${OUT}/07-task-running-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 07 (${theme})`)
  await page.close()
}

// ---- Frame 8: the last deploy did not finish -------------------------------

world.jobs = [FAILED_JOB]
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-failed')
  const text = await dialog.innerText()
  if (!/The last deploy did not finish\./.test(text)) fail(`${theme}: the failed sentence is missing`)
  const notice = dialog.getByTestId('deploy-launch-error')
  if (!(await notice.isVisible())) fail(`${theme}: the recorded error must render through ErrorNotice`)
  if (!/VcpuLimitExceeded/.test(await notice.innerText())) fail(`${theme}: the gateway's own error sentence must be shown verbatim`)
  if (await dialog.getByTestId('deploy-address').count()) fail(`${theme}: a failed launch offers no target`)
  const actions = await countActions(dialog)
  // ErrorNotice carries its own agent hand-off; the panel's single action is the deploy button.
  const own = actions.filter((a) => !/ask/i.test(a))
  if (own.length !== 1 || own[0] !== 'Open Remote Crew in Settings') {
    fail(`${theme}: exactly one panel action, "Open Remote Crew in Settings", got ${JSON.stringify(actions)}`)
  }
  if (!/Nothing is created until you confirm the steps there\./.test(text)) fail(`${theme}: the cost/undo line must accompany the deploy button here too`)
  await page.screenshot({ path: `${OUT}/08-failed-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 08 (${theme})`)
  await page.close()
}

// ---- Frame 9: the list is still being read ---------------------------------

world.jobs = []
world.launchAnswer = 'hold'
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-loading')
  const text = await dialog.innerText()
  if (!/Reading your deployments/.test(text)) fail(`${theme}: the loading sentence is missing`)
  if (/runs only on this computer/.test(text)) fail(`${theme}: a read in flight must never claim "runs only on this computer"`)
  if ((await countActions(dialog)).length !== 0) fail(`${theme}: nothing to press while the list is being read`)
  await page.screenshot({ path: `${OUT}/09-loading-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 09 (${theme})`)
  await page.close()
}

// ---- Frame 10: the list could not be read ----------------------------------

world.launchAnswer = { status: 500, body: { error: 'launch store unavailable' } }
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-error')
  const text = await dialog.innerText()
  if (!/Could not read your deployments\./.test(text)) fail(`${theme}: the read-failed sentence is missing`)
  if (/runs only on this computer/.test(text)) fail(`${theme}: a failed read must never claim "runs only on this computer"`)
  const retry = dialog.getByTestId('deploy-retry')
  if (!(await retry.isVisible())) fail(`${theme}: a failed read must offer Try again`)
  if ((await retry.innerText()).trim() !== 'Try again') fail(`${theme}: the retry must read "Try again"`)
  await page.screenshot({ path: `${OUT}/10-read-failed-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 10 (${theme})`)
  await page.close()
}
world.launchAnswer = null

// ---- Frame 11: the copy failed -------------------------------------------
//
// Both layers refuse: the async API rejects (a denied permission) and
// execCommand answers false (no clipboard at all), which is a plain-HTTP LAN
// dashboard's everyday shape. A tick over an unchanged clipboard is the worst
// affordance there is, so the row must say the copy failed, in words.

world.jobs = [DEPLOYED_JOB]
world.instances = REGISTERED
{
  const theme = 'dark'
  const page = await context.newPage()
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) },
    })
    document.execCommand = () => false
  })
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  await page.goto(`${base}/members`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-deploy-open').waitFor({ timeout: 20000 })
  const dialog = await openPanel(page, 'deploy-state-deployed')
  await dialog.getByTestId('deploy-address-copy').click()
  const notice = dialog.getByTestId('deploy-address-copy-error')
  await notice.waitFor({ timeout: 5000 })
  if (!/Copy failed\. Select the text and copy it manually\./.test(await notice.innerText())) {
    fail(`${theme}: a failed copy must say so and name the remedy: ${JSON.stringify(await notice.innerText())}`)
  }
  if (/Copied/.test(await dialog.getByTestId('deploy-address-copy').innerText())) {
    fail(`${theme}: a failed copy must not paint "Copied"`)
  }
  // The hand-off is on: nothing here is unsaved, and the agent has a remedy
  // the message does not (read the id off the record, open the session).
  if (!(await notice.getByRole('button', { name: /ask the agent/i }).count())) fail(`${theme}: the copy failure must offer the agent hand-off`)
  if (!/i-0abc123456789def0/.test(await dialog.innerText())) fail(`${theme}: the target must stay readable after a failed copy`)
  await page.screenshot({ path: `${OUT}/11-copy-failed-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 11 (${theme})`)
  await page.close()
}

// ---- Frame 12: the copy landed ---------------------------------------------
//
// The async clipboard resolves, so the button flips to Copied and no failure
// notice appears. The positive twin of frame 11.

world.jobs = [DEPLOYED_JOB]
world.instances = REGISTERED
{
  const theme = 'dark'
  const page = await context.newPage()
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.resolve() },
    })
  })
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  await page.goto(`${base}/members`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-deploy-open').waitFor({ timeout: 20000 })
  const dialog = await openPanel(page, 'deploy-state-deployed')
  const copy = dialog.getByTestId('deploy-address-copy')
  await copy.click()
  await page.waitForFunction(() => /Copied/.test(document.querySelector('[data-testid="deploy-address-copy"]')?.textContent ?? ''), null, { timeout: 5000 })
  if (await dialog.getByTestId('deploy-address-copy-error').count()) fail(`${theme}: a copy that landed must not show the failure notice`)
  await page.screenshot({ path: `${OUT}/12-copied-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 12 (${theme})`)
  await page.close()
}

// ---- Frame 13: a failed retry over an earlier launch that still runs --------
//
// The headline follows the newest launch (the failed retry); the line under
// the state names the earlier launch whose machine still runs (and bills)
// behind that headline, so the panel does not read "did not finish" over a
// live deployment.

world.jobs = [DEPLOYED_JOB, { ...FAILED_JOB, created_at: DEPLOYED_JOB.created_at + 3600 }]
world.instances = REGISTERED
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-failed')
  const line = dialog.getByTestId('deploy-earlier-live')
  if (!/An earlier deploy is still running in us-east-1\. You can delete it under Remote Crew in Settings; its identifiers are in Details\./.test(await line.innerText())) {
    fail(`${theme}: the live earlier launch must be named: ${JSON.stringify(await line.innerText())}`)
  }
  if (await dialog.getByTestId('deploy-address').count()) fail(`${theme}: the failed headline offers no target row`)
  await page.screenshot({ path: `${OUT}/13-failed-over-live-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 13 (${theme})`)
  await page.close()
}

// ---- Frame 14: the launch finished, but its machine is no longer registered --

// A Settings delete tore the stack down and unregistered the instance, and the
// launch record stayed `done`. The panel must not read "deployed" off that
// record: it says what happened, in the past tense, and offers the set-up flow.

world.jobs = [DEPLOYED_JOB]
world.instances = []
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-finished')
  const text = await dialog.innerText()
  if (/Your crew is deployed in the cloud\./.test(text)) fail(`${theme}: a torn-down machine must not read as deployed`)
  if (!/A deploy finished 3h 1[2-4]m ago in us-east-1\. Its machine is no longer in Your crews\./.test(text)) {
    fail(`${theme}: the gone sentence is missing or stale: ${JSON.stringify(text)}`)
  }
  if (await dialog.getByTestId('deploy-address').count()) fail(`${theme}: no target for a machine that is gone`)
  if (await dialog.getByTestId('deploy-check-console').count()) fail(`${theme}: no console pointer when the registry answered`)
  if (await dialog.getByTestId('deploy-earlier-live').count()) fail(`${theme}: no earlier-launch line: the only launch is the gone one`)
  const actions = await countActions(dialog)
  if (actions.length !== 1 || !/Open Remote Crew in Settings/.test(actions[0])) fail(`${theme}: the gone state offers the set-up flow, got ${JSON.stringify(actions)}`)
  if (!/Nothing is created until you confirm the steps there\./.test(text)) fail(`${theme}: the where-it-leads line is missing`)
  await page.screenshot({ path: `${OUT}/14-finished-gone-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 14 (${theme})`)
  await page.close()
}

// ---- Frame 15: the Instances feature is off, so liveness cannot be read -----

// A 403 from the registry is not rounded to either answer: the panel names the
// event and says it cannot tell, and points at where the answer lives.

world.jobs = [DEPLOYED_JOB]
world.instances = []
world.instancesAnswer = { status: 403, body: { error: 'Instances feature is disabled' } }
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-finished')
  const text = await dialog.innerText()
  if (/Your crew is deployed in the cloud\./.test(text)) fail(`${theme}: an unreadable registry must not read as deployed`)
  if (/no longer in Your crews/.test(text)) fail(`${theme}: an unreadable registry must not read as gone`)
  if (!/A deploy finished 3h 1[2-4]m ago in us-east-1\. This page cannot tell whether it is still running\./.test(text)) {
    fail(`${theme}: the unknown sentence is missing or stale: ${JSON.stringify(text)}`)
  }
  if (!(await dialog.getByTestId('deploy-check-console').isVisible())) fail(`${theme}: the console pointer is missing`)
  if ((await dialog.getByTestId('deploy-console-link').getAttribute('href')) !== 'https://us-east-1.console.aws.amazon.com/console/home?region=us-east-1') fail(`${theme}: the console sentence must link to the region's console`)
  if (await dialog.getByTestId('deploy-address').count()) fail(`${theme}: no target when the machine cannot be confirmed`)
  if (await dialog.getByTestId('deploy-action-deploy').count()) fail(`${theme}: an unknown machine does not get the deploy button`)
  await page.screenshot({ path: `${OUT}/15-finished-unknown-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 15 (${theme})`)
  await page.close()
}
world.instancesAnswer = null

// ---- Frame 16: a second registered machine behind the deployed headline -----

// A re-deploy that finished tears nothing down, so the older machine keeps
// billing behind "is deployed". The line names it there too.

const OLDER_DEPLOYED = {
  ...DEPLOYED_JOB,
  id: 'j-older',
  tag: 'kc-1f0c9a',
  instance_id: 'i-0fedcba9876543210',
  region: 'eu-west-1',
  created_at: NOW_SEC - 3 * 86400,
  updated_at: NOW_SEC - 3 * 86400 + 600,
}
world.jobs = [OLDER_DEPLOYED, DEPLOYED_JOB]
world.instances = [
  ...REGISTERED,
  { ...REGISTERED[0], id: 'inst-1f0c9a', name: 'Kiro Crew Cloud (kc-1f0c9a)', ssm_target: OLDER_DEPLOYED.instance_id, aws_region: 'eu-west-1' },
]
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-state-deployed')
  const text = await dialog.innerText()
  if (!/Your crew is deployed in the cloud\./.test(text)) fail(`${theme}: the newest registered machine is deployed`)
  const row = await dialog.getByTestId('deploy-address').innerText()
  if (!/i-0abc123456789def0/.test(row)) fail(`${theme}: the target row belongs to the newest machine: ${JSON.stringify(row)}`)
  const line = dialog.getByTestId('deploy-earlier-live')
  if (!/An earlier deploy is still running in eu-west-1\. You can delete it under Remote Crew in Settings; its identifiers are in Details\./.test(await line.innerText())) {
    fail(`${theme}: the older registered machine must be named under the deployed headline: ${JSON.stringify(await line.innerText())}`)
  }
  await page.screenshot({ path: `${OUT}/16-deployed-over-live-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 16 (${theme})`)
  await page.close()
}

// ---- Frame 17: the registry read fails outright -----------------------------

// Not a 403 (that is the feature being off, and reads as unknown): a failed
// read is an error, through the same notice and retry as a failed launch read.

world.jobs = [DEPLOYED_JOB]
world.instances = []
world.instancesAnswer = { status: 500, body: { error: 'registry unavailable' } }
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-error')
  const text = await dialog.innerText()
  if (!/Could not read your deployments\./.test(text)) fail(`${theme}: the read error sentence is missing: ${JSON.stringify(text)}`)
  if (/cannot tell whether it is still running|is deployed in the cloud|no longer in Your crews/.test(text)) fail(`${theme}: a failed registry read must not render any state`)
  if (!(await dialog.getByRole('button', { name: /ask the agent/i }).count())) fail(`${theme}: the read error must offer the agent hand-off`)
  if (!(await dialog.getByTestId('deploy-retry').isVisible())) fail(`${theme}: the read error must offer Try again`)
  await page.screenshot({ path: `${OUT}/17-registry-failed-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 17 (${theme})`)
  await page.close()
}
world.instancesAnswer = null

// ---- Frame 18: ECS reports the task STOPPED --------------------------------

// Said only on a STOPPED that ECS returned, with ECS's own reason and when. No
// "since deploy" beside "has stopped" (that reads as an uptime); Check again
// stays (ECS drops the task from its list later); the set-up door.

world.jobs = [FARGATE_JOB]
world.instances = []
world.taskAnswer = {
  status: 200,
  body: {
    ...TASK_RUNNING,
    task: {
      ...TASK_RUNNING.task,
      last_status: 'STOPPED',
      desired_status: 'STOPPED',
      stopped_at: NOW_SEC - 25 * 60,
      stopped_reason: 'Essential container in task exited',
    },
  },
}
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-stopped')
  const text = await dialog.innerText()
  if (!/Your crew's container task has stopped\./.test(text)) fail(`${theme}: the stopped sentence is missing: ${JSON.stringify(text)}`)
  if (!/It stopped 25m ago\./.test(text)) fail(`${theme}: when it stopped is missing`)
  if ((await dialog.getByTestId('deploy-task-stopped-reason').innerText()) !== 'Essential container in task exited') fail(`${theme}: ECS's own reason must be shown verbatim`)
  if (await dialog.getByTestId('deploy-stats').count()) fail(`${theme}: no since-deploy card beside "has stopped"`)
  if (!(await dialog.getByTestId('deploy-task-check').isVisible())) fail(`${theme}: Check again stays on a stopped task (ECS drops it from the list later)`)
  if (!(await dialog.getByTestId('deploy-task-read-at').isVisible())) fail(`${theme}: the read instant stays on a stopped task`)
  if (/is running|is deployed/.test(text)) fail(`${theme}: a stopped task must not read as running`)
  const actions = await countActions(dialog)
  if (actions.length !== 2 || !/Check again/.test(actions[0]) || !/Open Remote Crew in Settings/.test(actions[1])) fail(`${theme}: a stopped task offers Check again and the set-up door, nothing else, got ${JSON.stringify(actions)}`)
  await page.screenshot({ path: `${OUT}/18-task-stopped-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 18 (${theme})`)
  await page.close()
}

// ---- Frame 19: ECS no longer lists the task --------------------------------

// A read that returned no task for the ARN. Said as exactly that: not
// "stopped" (unknowable from here), not "running" (false), nothing about spend.

world.taskAnswer = { status: 200, body: { ...TASK_RUNNING, task: null } }
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-missing')
  const text = await dialog.innerText()
  if (!/ECS no longer lists your crew's container task\./.test(text)) fail(`${theme}: the unlisted sentence is missing: ${JSON.stringify(text)}`)
  if (/is running|has stopped|is deployed|billing/i.test(text)) fail(`${theme}: an unlisted task earns no claim about running, stopping or spend`)
  if (await dialog.getByTestId('deploy-task-row').count()) fail(`${theme}: no task row when ECS returned no task`)
  if (await dialog.getByTestId('deploy-task-console-link').count()) fail(`${theme}: no console link to a task ECS does not list`)
  if (!(await dialog.getByTestId('deploy-task-check').isVisible())) fail(`${theme}: Check again stays on an unlisted task, so the reader can re-verify`)
  const actions = await countActions(dialog)
  if (actions.length !== 2 || !/Check again/.test(actions[0]) || !/Open Remote Crew in Settings/.test(actions[1])) fail(`${theme}: an unlisted task offers Check again and the set-up door, nothing else, got ${JSON.stringify(actions)}`)
  await page.screenshot({ path: `${OUT}/19-task-missing-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 19 (${theme})`)
  await page.close()
}

// ---- Frame 20: the task read failed ----------------------------------------

// Not a state. The gateway's own error through the shared notice, the agent
// hand-off, and Try again.

world.taskAnswer = { status: 502, body: { error: 'aws ecs describe-tasks failed: AccessDeniedException', code: 'aws_call_failed' } }
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-unknown')
  const text = await dialog.innerText()
  if (!/This page could not read the task's status\./.test(text)) fail(`${theme}: the could-not-read sentence is missing: ${JSON.stringify(text)}`)
  if (/is running|has stopped|no longer lists|is deployed/.test(text)) fail(`${theme}: a failed read must not render any state`)
  if (!/AccessDeniedException/.test(await dialog.getByTestId('deploy-task-error').innerText())) fail(`${theme}: the gateway's error must be shown verbatim`)
  if (!(await dialog.getByRole('button', { name: /ask the agent/i }).count())) fail(`${theme}: the read error must offer the agent hand-off`)
  if (!(await dialog.getByTestId('deploy-task-retry').isVisible())) fail(`${theme}: the read error must offer Try again`)
  await page.screenshot({ path: `${OUT}/20-task-unknown-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 20 (${theme})`)
  await page.close()
}
world.taskAnswer = null

// ---- Frame 21: a Fargate launch still moving --------------------------------

// Described from the record alone (there is no task to read yet): Step N of M
// with the step's name, the close hint, and the door to the launch in Settings.

world.jobs = [{
  ...FARGATE_JOB,
  id: 'j-fargate-starting',
  instance_id: '',
  status: 'running',
  steps: [
    { key: 'preflight', label: 'Check your AWS setup', state: 'done', detail: '' },
    { key: 'provision', label: 'Start the container task', state: 'active', detail: '' },
    { key: 'signin', label: 'Sign in to Kiro', state: 'pending', detail: '' },
    { key: 'connect', label: 'Connect', state: 'pending', detail: '' },
  ],
  created_at: NOW_SEC - 70,
  updated_at: NOW_SEC - 3,
}]
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-starting')
  const text = await dialog.innerText()
  if (!/Your crew is starting as a container task\./.test(text)) fail(`${theme}: the starting sentence is missing: ${JSON.stringify(text)}`)
  if (!/Step 2 of 4/.test(text)) fail(`${theme}: the step counter is missing`)
  if (!/Start the container task/.test(text)) fail(`${theme}: the step's own name is missing`)
  if (!/Closing this window does not stop the deploy\./.test(text)) fail(`${theme}: the close hint is missing`)
  if (await dialog.getByTestId('deploy-task-read-at').count()) fail(`${theme}: no task read on a launch that has not recorded a task`)
  const actions = await countActions(dialog)
  if (actions.length !== 1 || !/Open Remote Crew in Settings/.test(actions[0])) fail(`${theme}: the starting state offers the door to Settings and nothing else, got ${JSON.stringify(actions)}`)
  await page.screenshot({ path: `${OUT}/21-task-starting-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 21 (${theme})`)
  await page.close()
}

// ---- Frame 22: the Fargate launch failed before a task existed --------------

// The gateway's own sentence through the shared notice with the agent
// hand-off, and the door back to set-up. No task read: there is no ARN.

world.jobs = [{
  ...FARGATE_JOB,
  id: 'j-fargate-failed',
  instance_id: '',
  status: 'failed',
  steps: [
    { key: 'preflight', label: 'Check your AWS setup', state: 'done', detail: '' },
    { key: 'provision', label: 'Start the container task', state: 'error', detail: '' },
    { key: 'signin', label: 'Sign in to Kiro', state: 'pending', detail: '' },
    { key: 'connect', label: 'Connect', state: 'pending', detail: '' },
  ],
  error: 'ecs run-task failed: no container instances were found in your cluster (RESOURCE:FARGATE capacity unavailable in us-east-1a).',
  created_at: NOW_SEC - 25 * 60,
  updated_at: NOW_SEC - 24 * 60,
}]
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-failed')
  const text = await dialog.innerText()
  if (!/The last deploy did not start a container task\./.test(text)) fail(`${theme}: the failed sentence is missing: ${JSON.stringify(text)}`)
  if (!/RESOURCE:FARGATE capacity unavailable/.test(await dialog.getByTestId('deploy-launch-error').innerText())) fail(`${theme}: the gateway's error must be shown verbatim`)
  if (!(await dialog.getByRole('button', { name: /ask the agent/i }).count())) fail(`${theme}: the failed launch must offer the agent hand-off`)
  if (await dialog.getByTestId('deploy-task-read-at').count()) fail(`${theme}: no task read on a launch that recorded no task`)
  if (/is running|has stopped|no longer lists/.test(text)) fail(`${theme}: a failed launch earns no task state`)
  const actions = await countActions(dialog)
  if (actions.length !== 2 || !/ask the agent/i.test(actions[0]) || !/Open Remote Crew in Settings/.test(actions[1])) fail(`${theme}: the failed launch offers the agent hand-off and the set-up door, nothing else, got ${JSON.stringify(actions)}`)
  await page.screenshot({ path: `${OUT}/22-task-failed-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 22 (${theme})`)
  await page.close()
}
world.jobs = [FARGATE_JOB]

// ---- Frame 23: ECS reports the task PENDING ---------------------------------

// A moving state read from ECS: the sentence names the region, the stats grid
// shows what is known, and Check again is offered because the answer can
// change (the view also re-reads on its own while the phase moves).

world.taskAnswer = {
  status: 200,
  body: { ...TASK_RUNNING, task: { ...TASK_RUNNING.task, last_status: 'PENDING', started_at: null } },
}
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-starting')
  const text = await dialog.innerText()
  if (!/Your crew's container task is starting in us-east-1\./.test(text)) fail(`${theme}: the ECS-starting sentence is missing: ${JSON.stringify(text)}`)
  if (!(await dialog.getByTestId('deploy-task-check').isVisible())) fail(`${theme}: a moving task must offer Check again`)
  if (!(await dialog.getByTestId('deploy-stats').isVisible())) fail(`${theme}: the stats grid must show for a moving task`)
  if (/is running|has stopped/.test(text)) fail(`${theme}: PENDING is not running and not stopped`)
  await page.screenshot({ path: `${OUT}/23-task-pending-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 23 (${theme})`)
  await page.close()
}

// ---- Frame 24: a stop ECS has accepted, before lastStatus catches up --------

// desiredStatus STOPPED with lastStatus still RUNNING is the stopping state:
// the panel never shows "running" as the last word before "stopped".

world.taskAnswer = {
  status: 200,
  body: { ...TASK_RUNNING, task: { ...TASK_RUNNING.task, last_status: 'RUNNING', desired_status: 'STOPPED' } },
}
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-stopping')
  const text = await dialog.innerText()
  if (!/Your crew's container task is stopping\./.test(text)) fail(`${theme}: the stopping sentence is missing: ${JSON.stringify(text)}`)
  if (/is running/.test(text)) fail(`${theme}: an accepted stop must not read as running`)
  if (!(await dialog.getByTestId('deploy-task-check').isVisible())) fail(`${theme}: a stopping task must offer Check again`)
  await page.screenshot({ path: `${OUT}/24-task-stopping-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 24 (${theme})`)
  await page.close()
}

// ---- Frame 25: a lifecycle word this panel does not know -------------------

// Shown verbatim, never rounded to the nearest state it might mean.

world.taskAnswer = {
  status: 200,
  body: { ...TASK_RUNNING, task: { ...TASK_RUNNING.task, last_status: 'HIBERNATING' } },
}
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-state-other')
  const text = await dialog.innerText()
  if (!/ECS reports your crew's container task as HIBERNATING\./.test(text)) fail(`${theme}: the verbatim word is missing: ${JSON.stringify(text)}`)
  if (/is running|has stopped|is starting|is stopping/.test(text)) fail(`${theme}: an unknown word must not be rounded to a known state`)
  if (!(await dialog.getByTestId('deploy-task-check').isVisible())) fail(`${theme}: an unknown word can change, so Check again is offered`)
  await page.screenshot({ path: `${OUT}/25-task-other-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 25 (${theme})`)
  await page.close()
}

// ---- Frame 26: the task read is still in flight -----------------------------

world.taskAnswer = 'hold'
{
  const theme = 'dark'
  const page = await openMembers(theme)
  const dialog = await openPanel(page, 'deploy-task-loading')
  const text = await dialog.innerText()
  if (!/Reading the task's status from ECS/.test(text)) fail(`${theme}: the loading line is missing: ${JSON.stringify(text)}`)
  if (/is running|has stopped|no longer lists|could not read/.test(text)) fail(`${theme}: a read in flight renders no state and no error`)
  await page.screenshot({ path: `${OUT}/26-task-loading-${theme}.png`, clip: await clipOf(dialog) })
  console.log(`wrote 26 (${theme})`)
  await page.close()
}
world.taskAnswer = null

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
