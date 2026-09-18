/**
 * Screenshot harness for "recover a cloud crew that was created unsigned".
 *
 * The dead end: a launch registered the instance, the card went green, Connect
 * was offered, and the remote dashboard then answered every chat with "not
 * logged in" — fixable only from a terminal that dashboard does not have. Five
 * states, because the recovery is only legible if all five read correctly:
 *
 *  1. The unsigned crew in Your instances — the `Needs sign-in` badge, the held
 *     Connect, the row-level way to get a code, and the hint that says what that
 *     button produces. The state the old flow reported as done and green.
 *  2. A LIVE code — and deliberately no replace button, because restarting the
 *     login mid-approval retires the code the reader is typing into a browser.
 *  3. A STALE code — named stale, replaceable, click-to-copy, its outcome hint,
 *     both waiting step icons, and the identity-named title.
 *  4. A restarted sign-in with no code yet — the gap between asking and holding.
 *  5. A job that was already awaiting approval when the tab opened — the prompt
 *     lives on the gateway, so the card offers to FETCH rather than a code.
 *
 * The launch form's identity question (Personal vs Company SSO) is a separate,
 * already-shipped feature with its own coverage; it is not shot here.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures — no gateway, no AWS, no
 * kiro-cli. What it therefore does NOT show is a real device code arriving from
 * an instance; that needs an EC2 box and a company SSO account.
 *
 * Every claim is ASSERTED before its PNG is written: a harness that only writes
 * PNGs can hand a PR a picture of an error boundary as evidence.
 *
 * Usage: npm run build && node scripts/capture-remote-crew-signin-recovery.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/remote-crew-signin-recovery'
mkdirSync(OUT, { recursive: true })

const START_URL = 'https://amzn.awsapps.com/start'

/** A crew whose launch finished but whose Kiro sign-in never confirmed. */
const UNSIGNED_INSTANCE = {
  id: 'kc1',
  name: 'Kiro Crew Cloud (kc-5e10bb)',
  connection_method: 'ssm',
  ssm_target: 'i-0abc123456789def0',
  ssm_run_as: '',
  ssh_host: '',
  aws_profile: '',
  aws_region: 'us-east-1',
  provisioner_id: 'aws_ec2',
  remote_port: 8765,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'kc1', state: 'disconnected' },
}

const STEPS_REGISTERED = [
  { key: 'preflight', label: 'Check your AWS setup', state: 'done', detail: '' },
  { key: 'provision', label: 'Create the instance and install Kiro Crew', state: 'done', detail: 'i-0abc123456789def0' },
  {
    key: 'signin',
    label: 'Sign in to Kiro',
    state: 'skipped',
    // Verbatim from `launch_job.py` (`run_launch` and `run_signin_retry`). A frame
    // captured against invented copy is evidence for a screen that does not ship.
    detail: 'Not signed in yet — finish it in the sign-in box below.',
  },
  {
    key: 'connect',
    label: 'Connect',
    state: 'done',
    detail: 'Added to your instances. Finish the Kiro sign-in before connecting.',
  },
]

/** Registered, terminal, never signed in — and launched through a company portal,
 *  so the code box names the account the code is approved with. */
const UNSIGNED_JOB = {
  id: 'j-unsigned',
  provider_id: 'aws_ec2',
  tag: 'kc-5e10bb',
  instance_id: 'i-0abc123456789def0',
  profile: '',
  region: 'us-east-1',
  size_key: 'balanced',
  status: 'done',
  steps: STEPS_REGISTERED,
  signin: null,
  signin_detected: false,
  login_target: { license: 'pro', start_url: START_URL, region: 'us-east-1' },
  error: '',
  created_at: 0,
  updated_at: 0,
}

/** The gateway is polling THIS code: it is live, and must not be replaceable. */
const AWAITING_JOB = {
  ...UNSIGNED_JOB,
  id: 'j-awaiting',
  status: 'awaiting_signin',
  steps: STEPS_REGISTERED.map(s => (s.key === 'signin' ? { ...s, state: 'active', detail: '' } : s)),
  signin: { url: `${START_URL}/#/device?user_code=GFXK-MNKS`, code: 'GFXK-MNKS', ports: [] },
}

/** A code the gateway kept after the wait ran out. It MAY still work, so it stays
 *  on screen — with the way to replace it when it does not. */
const STALE_JOB = {
  ...UNSIGNED_JOB,
  id: 'j-stale',
  signin: { url: `${START_URL}/#/device?user_code=OLD-CODE`, code: 'OLD-CODE', ports: [] },
}

/**
 * The gap between asking for a sign-in and holding a code: the step is running on
 * the instance and nothing has printed one yet. `signin` is null while `status` is
 * `running`, which is what makes this a distinct state rather than frame 2 without
 * its code.
 *
 * The crew is REGISTERED here, and deliberately so: this is the restart route
 * (`/signin/restart` on a crew that exists), which is the only place the state is
 * reachable. On an initial launch the connect step has not run, so the card shows
 * launch progress and no prompt block — there is no code to hold and no button
 * that would work, so nothing is being hidden.
 */
const STARTING_JOB = {
  ...UNSIGNED_JOB,
  id: 'j-starting',
  status: 'running',
  steps: STEPS_REGISTERED.map(s => (s.key === 'signin' ? { ...s, state: 'active', detail: '' } : s)),
  signin: null,
}

/**
 * A job that reached `awaiting_signin` before this tab was open. The prompt lives
 * on the gateway, so the card has nothing to render until the dashboard asks for
 * it — hence a primary "Show the sign-in code", which is what the click produces.
 * It is deliberately NOT "Open sign-in page" with an external-link icon: this
 * click opens nothing.
 */
const AWAITING_NO_CODE_JOB = { ...AWAITING_JOB, id: 'j-awaiting-nocode', signin: null }

/**
 * An initial launch still creating its instance: nothing is registered, so the
 * cancel here removes the instance being created. This is the OTHER half of the
 * cancel split -- frames 2-5 show only "Stop sign-in, keep the instance".
 */
const LAUNCHING_JOB = {
  ...UNSIGNED_JOB,
  id: 'j-launching',
  instance_id: '',
  status: 'running',
  steps: [
    { key: 'preflight', label: 'Check your AWS setup', state: 'done', detail: '' },
    { key: 'provision', label: 'Create the instance and install Kiro Crew', state: 'active', detail: '' },
    { key: 'signin', label: 'Sign in to Kiro', state: 'pending', detail: '' },
    { key: 'connect', label: 'Connect', state: 'pending', detail: '' },
  ],
  signin: null,
}

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
  steps: STEPS_REGISTERED.map(({ key, label }) => ({ key, label, state: 'pending', detail: '' })),
}

/**
 * The fixture world, switched per frame. `jobs` decides whether a crew is
 * unsigned and whether a code is live; `instances` decides whether Your instances
 * has a row at all.
 */
const world = { jobs: [], instances: [], signinAnswer: null }

const extra = async (path, route) => {
  if (path === '/api/instances') {
    return json(route, { active: true, warm_set_cap: 5, instances: world.instances }), true
  }
  if (path === '/api/cloud/launch') return json(route, { jobs: world.jobs }), true
  if (/^\/api\/cloud\/launch\/[^/]+\/signin$/.test(path) && world.signinAnswer) {
    return json(route, world.signinAnswer.body, world.signinAnswer.status), true
  }
  const one = /^\/api\/cloud\/launch\/([^/]+)$/.exec(path)
  if (one) {
    const found = world.jobs.find(j => j.id === decodeURIComponent(one[1]))
    return json(route, found || world.jobs[0] || {}), true
  }
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
const VIEW = { width: 1220, height: 940 }
const context = await browser.newContext({
  viewport: VIEW,
  deviceScaleFactor: 2, // the 12px hint copy renders soft at 1x on GitHub
})

/** A page on the current fixture world, in the given theme. */
const openPage = async theme => {
  const page = await context.newPage()
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  return page
}

/** Open the Set-up tab, where the launch progress card lives. */
const openSetup = async page => {
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /Set up a new one/i }).click()
  await page.locator('[data-testid="signin-prompt"]').waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
}

/**
 * A clip around one element, with room for its own copy.
 *
 * Scrolls first, then measures: these surfaces sit below the size cards, so an
 * unscrolled box starts past the viewport bottom and Playwright rejects the clip
 * as outside the image. Clamped on both axes for the same reason.
 */
const clipOf = async (page, locator, pad = 14, extraBottom = 10) => {
  await locator.scrollIntoViewIfNeeded()
  await page.waitForTimeout(250)
  const box = await locator.boundingBox()
  const x = Math.max(0, Math.min(box.x - pad, VIEW.width - 1))
  const y = Math.max(0, Math.min(box.y - pad, VIEW.height - 1))
  return {
    x,
    y,
    width: Math.max(1, Math.min(VIEW.width - x, box.width + 2 * pad)),
    height: Math.max(1, Math.min(VIEW.height - y, box.height + 2 * pad + extraBottom)),
  }
}

// ---- Frame 1: a crew that exists but is not signed in ----------------------

world.jobs = [UNSIGNED_JOB]
world.instances = [UNSIGNED_INSTANCE]
for (const theme of ['dark', 'light']) {
  const page = await openPage(theme)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  const row = page.locator('[data-crew-id="kc1"]')
  await row.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)

  const rowText = await row.innerText()
  if (!/Needs sign-in/i.test(rowText)) fail(`${theme}: the unsigned crew must be badged: ${JSON.stringify(rowText)}`)
  const held = row.getByRole('button', { name: /Connect after sign-in/i })
  if (!(await held.isVisible())) fail(`${theme}: Connect must be held back on an unsigned crew`)
  if (!(await held.isDisabled())) fail(`${theme}: the held Connect button must be disabled`)
  if (await row.getByRole('button', { name: /^Connect$/ }).count()) {
    fail(`${theme}: the ordinary Connect must not be offered on an unsigned crew`)
  }
  if (!(await row.getByRole('button', { name: /Start sign-in/i }).isVisible())) {
    fail(`${theme}: the crew row must offer the sign-in without going back to Set up`)
  }
  // The button read right only as a guess: nothing said whether it resumes a
  // flow or begins one. The hint names the outcome.
  const rowHint = row.locator('[data-testid="signin-recovery-hint"]')
  if (!(await rowHint.isVisible())) fail(`${theme}: the row's recovery button must say what it produces`)
  if (!/Starts the sign-in on the instance and shows a new code here\./i.test(await rowHint.innerText())) {
    fail(`${theme}: the row hint must describe a fresh sign-in, not a resumed one`)
  }
  // Above the crew's name and titled "This crew", the block read as a page-level
  // warning banner — and with several rows it attributed to the wrong crew. It
  // names the crew, and follows the row header.
  const rowPrompt = row.locator('[data-testid="signin-prompt"]')
  // No code on this row yet, so the title names the ACTION and the crew -- not a
  // code the reader has not been given.
  if (!/Get a sign-in code for Kiro Crew Cloud \(kc-5e10bb\)/i
    .test(await rowPrompt.innerText())) {
    fail(`${theme}: the row's sign-in block must name the crew it belongs to`)
  }
  const promptFollowsName = await row.evaluate(el => {
    const name = [...el.querySelectorAll('div')].find(d => d.textContent.trim() === 'Kiro Crew Cloud (kc-5e10bb)')
    const block = el.querySelector('[data-testid="signin-prompt"]')
    return !!name && !!block
      && !!(name.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING)
  })
  if (!promptFollowsName) fail(`${theme}: the sign-in block must render below the row header, not above it`)
  await page.screenshot({ path: `${OUT}/01-needs-signin-row-${theme}.png`, clip: await clipOf(page, row, 12, 4) })
  console.log(`wrote 01 (${theme})`)
  await page.close()
}

// ---- Frame 2: the device code, live ---------------------------------------

world.jobs = [AWAITING_JOB]
world.instances = []
for (const theme of ['dark', 'light']) {
  const page = await openPage(theme)
  await openSetup(page)
  const prompt = page.locator('[data-testid="signin-prompt"]')
  const text = await prompt.innerText()
  if (!/GFXK-MNKS/.test(text)) fail(`${theme}: the live device code must be on the card: ${JSON.stringify(text)}`)
  // While the gateway is polling THIS code, offering a replacement would
  // invalidate the code the user is in the middle of typing.
  if (await prompt.getByRole('button', { name: /Start over with a new code/i }).count()) {
    fail(`${theme}: no replace button while the shown code is still being polled`)
  }
  // And no outcome hint either — it would describe a click that is not offered.
  if (await prompt.locator('[data-testid="signin-recovery-hint"]').count()) {
    fail(`${theme}: no outcome hint while no recovery button is offered`)
  }
  // Same button, two blast radii: this job is a registered crew, so Cancel only
  // stops the sign-in. The reader would not click a Cancel that might also throw
  // away the instance that already exists.
  const cancel = page.getByRole('button', { name: /Stop the sign-in on kc-5e10bb and keep the instance/i })
  if (!(await cancel.isVisible())) fail(`${theme}: Cancel must say it keeps the instance on a sign-in retry`)
  if (!/Stop sign-in, keep the instance/i.test(await cancel.innerText())) {
    fail(`${theme}: the visible Cancel copy must name its blast radius too`)
  }
  // The panel calls these boxes instances everywhere else, so "crew" here read as
  // a different, second thing the reader had to guess at.
  if (/\bcrew\b/i.test(await cancel.innerText())) {
    fail(`${theme}: the Cancel copy must not call the instance a crew`)
  }
  // Not danger-toned: this click keeps the instance and the crew row, and toning
  // both would leave the tone meaning nothing.
  if (await cancel.evaluate(el => /danger/.test(el.className))) {
    fail(`${theme}: the keep-the-instance cancel must NOT wear the danger tone`)
  }
  // And the card must not claim it is busy launching while it is in fact blocked
  // on the reader approving the code below it.
  const awaitingBadge = page.getByText(/Waiting for your approval/i)
  if (!(await awaitingBadge.isVisible())) {
    fail(`${theme}: a job awaiting the sign-in must be badged as waiting, not launching`)
  }
  if (await page.getByText(/Launching…|Setting up/).count()) {
    fail(`${theme}: an in-progress badge must not sit above a card asking the reader to approve a code`)
  }
  const card = prompt.locator('xpath=..')
  await page.screenshot({ path: `${OUT}/02-live-code-${theme}.png`, clip: await clipOf(page, card, 10, 0) })
  console.log(`wrote 02 (${theme})`)
  await page.close()
}

// ---- Frame 3: a stale code, and the way to replace it ---------------------

world.jobs = [STALE_JOB]
world.instances = []
for (const theme of ['dark', 'light']) {
  const page = await openPage(theme)
  await openSetup(page)
  const prompt = page.locator('[data-testid="signin-prompt"]')
  const text = await prompt.innerText()
  if (!/OLD-CODE/.test(text)) fail(`${theme}: the surviving code must stay visible: ${JSON.stringify(text)}`)
  if (!/could not confirm the sign-in/i.test(text)) fail(`${theme}: a stale code must be called stale: ${JSON.stringify(text)}`)
  const replace = prompt.getByRole('button', { name: /Start over with a new code/i })
  if (!(await replace.isVisible())) {
    fail(`${theme}: a stale code must come with a way to replace it`)
  }
  // It lives INSIDE the hint sentence that warns what it costs, not as a fourth
  // peer in the action row. Four sibling actions with no ranking is what the
  // reader was choosing between.
  if (!(await prompt.locator('[data-testid="signin-recovery-hint"] [data-testid="signin-get-new-code"]').count())) {
    fail(`${theme}: the replacement must be the hint's inline link, not a button in the action row`)
  }
  const rowActions = await prompt.evaluate(el => {
    const hint = el.querySelector('[data-testid="signin-recovery-hint"]')
    return [...el.querySelectorAll('button, a')]
      .filter(n => !hint || !hint.contains(n))
      .map(n => n.getAttribute('data-testid') || n.innerText.trim())
  })
  // The chip is the copy affordance and the anchor is the code's own target, so
  // what is left must be exactly one primary.
  const primaries = rowActions.filter(a => a !== 'signin-code-copy' && !/Open sign-in page/i.test(a))
  if (primaries.length !== 1) {
    fail(`${theme}: the action row must hold one primary beside the code, got ${JSON.stringify(rowActions)}`)
  }
  // And that primary is NOT in the code's row: chip + page link + recheck as
  // three peers in one flex row is what the reader was ranking. Two rows, at
  // most two actions each.
  const codeRow = prompt.locator('[data-testid="signin-code-row"]')
  const actionRow = prompt.locator('[data-testid="signin-action-row"]')
  if ((await codeRow.locator('button, a').count()) !== 2) {
    fail(`${theme}: the code row must hold exactly the copy chip and the page link`)
  }
  if ((await actionRow.locator('button, a').count()) !== 1) {
    fail(`${theme}: the action row must hold exactly the recheck primary`)
  }
  const [codeBox, actionBox] = [await codeRow.boundingBox(), await actionRow.boundingBox()]
  if (!(actionBox.y >= codeBox.y + codeBox.height - 1)) {
    fail(`${theme}: the recheck must sit on its own row below the code`)
  }
  // The second "sign-in" is the company SSO one, and the card now says so
  // instead of leaving the reader to assume the two are the same act.
  if (!/Approve with your company SSO account/i.test(text)) {
    fail(`${theme}: the code box must name the identity it approves: ${JSON.stringify(text)}`)
  }
  // The code is a value to type elsewhere; the reader tried clicking it.
  const copyBtn = prompt.locator('[data-testid="signin-code-copy"]')
  if (!(await copyBtn.isVisible())) fail(`${theme}: the code must be copyable`)
  if (!/Copy your sign-in code OLD-CODE/i.test(await copyBtn.getAttribute('aria-label'))) {
    fail(`${theme}: the copy control must name the code it copies`)
  }
  // And it says so in words: the glyph alone left the reader guessing that the
  // chip copies at all.
  if (!/\bCopy\b/i.test(await copyBtn.innerText())) {
    fail(`${theme}: the code chip must carry the word Copy, not only the icon`)
  }
  // Replacing is irreversible for the code on screen, so say what the click does.
  const hint = prompt.locator('[data-testid="signin-recovery-hint"]')
  // One hint slot serves both buttons in this state: the recheck primary and the
  // replace secondary. It must carry BOTH facts -- that the code survives a
  // recheck, and that starting over with a new one retires it.
  const hintText = await hint.innerText()
  if (!/keeping the code above/i.test(hintText)) {
    fail(`${theme}: the recheck must say it keeps the code: ${JSON.stringify(hintText)}`)
  }
  if (!/Starting over with a new code retires the code above/i.test(hintText)) {
    fail(`${theme}: the replace button must say the shown code is retired`)
  }
  // The steps the card is asking you to finish must not read as done or as
  // not-started.
  const card = prompt.locator('xpath=..')
  if (!(await card.locator('[data-testid="step-waiting-signin"]').isVisible())) {
    fail(`${theme}: the unconfirmed sign-in step must show the waiting icon, not an empty circle`)
  }
  if (!(await card.locator('[data-testid="step-waiting-connect"]').isVisible())) {
    fail(`${theme}: the held connect step must keep its waiting icon, not a green check`)
  }
  await page.screenshot({ path: `${OUT}/03-stale-code-replaceable-${theme}.png`, clip: await clipOf(page, card, 10, 0) })
  console.log(`wrote 03 (${theme})`)
  await page.close()
}

// ---- Frame 4: a restarted sign-in, before any code exists -----------------

world.jobs = [STARTING_JOB]
world.instances = []
for (const theme of ['dark', 'light']) {
  const page = await openPage(theme)
  await openSetup(page)
  const prompt = page.locator('[data-testid="signin-prompt"]')
  const text = await prompt.innerText()
  if (!/Starting the Kiro sign-in on the instance\./i.test(text)) {
    fail(`${theme}: the starting state must say the sign-in is starting: ${JSON.stringify(text)}`)
  }
  // The CARD's badge carries "Getting your sign-in code…"; the body line says what
  // the badge cannot, so the same fact is not on screen twice (frame 07's reader
  // flagged the repetition).
  if (!/takes a few seconds/i.test(text)) {
    fail(`${theme}: the starting state must say how long to wait: ${JSON.stringify(text)}`)
  }
  if ((await page.getByText(/Getting your sign-in code…/).count()) !== 1) {
    fail(`${theme}: "Getting your sign-in code…" must appear once, in the badge`)
  }
  // Nothing to approve and nothing to replace yet: offering either would be a
  // button that cannot work.
  if (await prompt.getByRole('button', { name: /Start over with a new code|Show the sign-in code/i }).count()) {
    fail(`${theme}: no code button before a code exists`)
  }
  await page.screenshot({
    path: `${OUT}/04-signin-starting-${theme}.png`,
    clip: await clipOf(page, prompt.locator('xpath=..'), 10, 0),
  })
  console.log(`wrote 04 (${theme})`)
  await page.close()
}

// ---- Frame 5: awaiting approval, code not yet fetched ---------------------

world.jobs = [AWAITING_NO_CODE_JOB]
world.instances = []
for (const theme of ['dark', 'light']) {
  const page = await openPage(theme)
  await openSetup(page)
  const prompt = page.locator('[data-testid="signin-prompt"]')
  const fetchBtn = prompt.getByRole('button', { name: /Show the sign-in code/i })
  // The click renders the code inline, so an external-link label promised a tab
  // that never opened. The page link is the anchor that appears with a code.
  if (await prompt.getByRole('button', { name: /Open sign-in page/i }).count()) {
    fail(`${theme}: the fetch button must not be labelled as opening a page`)
  }
  if (!(await fetchBtn.isVisible())) {
    fail(`${theme}: a job awaiting sign-in with no local code must offer to fetch the prompt`)
  }
  if (!(await fetchBtn.isEnabled())) fail(`${theme}: the fetch button must be usable`)
  // The gateway holds a code and the button shows it, so the title must say the
  // code is ready -- "Get a sign-in code" over a button that starts nothing
  // contradicted itself.
  if (!/sign-in code .*is ready/i.test(await prompt.innerText())) {
    fail(`${theme}: an awaiting no-code prompt must say the code is ready`)
  }
  // Restarting here would throw away a code the user may already be typing.
  if (await prompt.getByRole('button', { name: /Start over with a new code/i }).count()) {
    fail(`${theme}: no replace button while the gateway is still polling this login`)
  }
  await page.screenshot({
    path: `${OUT}/05-awaiting-fetch-prompt-${theme}.png`,
    clip: await clipOf(page, prompt.locator('xpath=..'), 10, 0),
  })
  console.log(`wrote 05 (${theme})`)
  await page.close()
}

// ---- Frame 6: an unregistered launch, and the cancel that removes it -------

world.jobs = [LAUNCHING_JOB]
world.instances = []
for (const theme of ['dark', 'light']) {
  const page = await openPage(theme)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: /Set up a new one/i }).click()
  // No instance exists yet, so this Cancel says it removes the one being created
  // -- the destructive half of the split, never shown in frames 2-5.
  const cancel = page.getByRole('button', { name: /Cancel setup of kc-5e10bb and remove the instance/i })
  await cancel.waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  if (!/Cancel and remove the instance…/.test(await cancel.innerText())) {
    fail(`${theme}: the copy must name what it removes AND end in the panel's confirm ellipsis`)
  }
  // And it does not LOOK like the cancel that keeps the crew (frame 02): reading
  // the label was the only way to tell them apart, before a click that deletes a
  // machine.
  const dangerToned = await cancel.evaluate(el => /danger/.test(el.className))
  if (!dangerToned) {
    fail(`${theme}: the cancel that removes the instance must carry the danger tone`)
  }
  if (await page.getByRole('button', { name: /keep the instance/i }).count()) {
    fail(`${theme}: an unregistered launch must not offer the keep-the-instance cancel`)
  }
  // Nothing to sign in to yet: no prompt block, and the card says it is setting up.
  if (await page.locator('[data-testid="signin-prompt"]').count()) {
    fail(`${theme}: a launch still creating its instance has no sign-in block`)
  }
  // The CARD's badge, in the row pill's own words (frame 09 captures that pill on
  // the same job): one job in progress must not wear two names. Opening the
  // set-up form replaces the list, so only one of the two surfaces is on screen
  // here -- which is why each frame asserts its own.
  if (!(await page.getByText('Setting up').first().isVisible())) {
    fail(`${theme}: the progress card must be badged "Setting up"`)
  }
  if (await page.getByText(/Launching…/).count()) {
    fail(`${theme}: "Launching…" must not be a second name for the same state`)
  }
  // It asks before it destroys, like the crew row's Delete: one click arms and
  // warns, a second one fires. Asserted (and captured) LAST, because arming
  // replaces the button every assertion above reads.
  await cancel.click()
  const warning = page.locator('[data-testid="cancel-remove-warning"]')
  await warning.waitFor({ timeout: 5000 })
  if (!/cannot be undone/i.test(await warning.innerText())) {
    fail(`${theme}: the warning must say the removal cannot be undone`)
  }
  // The operand is quoted per locale, so match the verb and check the name.
  const confirmBtn = page.getByRole('button', { name: /Yes, remove/i })
  if (!(await confirmBtn.isVisible())) {
    fail(`${theme}: the armed state needs a confirm`)
  }
  if (!/kc-5e10bb/.test(await confirmBtn.innerText())) {
    fail(`${theme}: the confirm must name the instance it removes`)
  }
  if (!(await page.getByRole('button', { name: /Keep setting up/i }).isVisible())) {
    fail(`${theme}: the armed state needs a way out`)
  }
  await page.waitForTimeout(250)
  // The card is the badge row's PARENT, and the warning sits inside that row --
  // so the card is the warning's grandparent. Read from the warning rather than
  // from the cancel, which arming replaces.
  const card = warning.locator('xpath=../..')
  await page.screenshot({
    path: `${OUT}/06-launching-cancel-removes-${theme}.png`,
    clip: await clipOf(page, card, 10, 0),
  })
  console.log(`wrote 06 (${theme})`)
  await page.close()
}

// ---- Frames 7 and 8: the code chip's copy feedback, success and failure -----
//
// The chip is a copy button. Its two outcomes are shown, not only the idle icon:
// a tick over an unchanged clipboard is the worst affordance there is, so the
// failed copy must SAY so, in the panel's own error surface.

world.jobs = [STALE_JOB]
world.instances = []
for (const theme of ['dark', 'light']) {
  // 7: the copy succeeded. Loopback is a secure context, so the async API runs
  // once the permission is granted.
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base })
  let page = await openPage(theme)
  await openSetup(page)
  let prompt = page.locator('[data-testid="signin-prompt"]')
  await prompt.locator('[data-testid="signin-code-copy"]').click()
  const chip = prompt.locator('[data-testid="signin-code-copy"]')
  await chip.filter({ hasText: /Copied/i }).waitFor({ timeout: 5000 })
  if (!/Copied/i.test(await chip.innerText())) {
    fail(`${theme}: a successful copy must say "Copied": ${JSON.stringify(await chip.innerText())}`)
  }
  if (await prompt.locator('[data-testid="signin-copy-error"]').count()) {
    fail(`${theme}: a successful copy must not also show the failure notice`)
  }
  await page.screenshot({
    path: `${OUT}/07-code-copied-${theme}.png`,
    clip: await clipOf(page, prompt.locator('xpath=..'), 10, 0),
  })
  console.log(`wrote 07 (${theme})`)
  await page.close()
  await context.clearPermissions()

  // 8: the copy failed -- both layers. The async API rejects (a refused
  // permission) and execCommand answers false (no clipboard at all), which is a
  // plain-HTTP LAN dashboard's everyday shape.
  page = await context.newPage()
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) },
    })
    document.execCommand = () => false
  })
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  await openSetup(page)
  prompt = page.locator('[data-testid="signin-prompt"]')
  await prompt.locator('[data-testid="signin-code-copy"]').click()
  const notice = prompt.locator('[data-testid="signin-copy-error"]')
  await notice.waitFor({ timeout: 5000 })
  if (!/Copy failed\. Select the text and copy it manually\./i.test(await notice.innerText())) {
    fail(`${theme}: a failed copy must say so and name the remedy: ${JSON.stringify(await notice.innerText())}`)
  }
  if (/Copied/i.test(await prompt.locator('[data-testid="signin-code-copy"]').innerText())) {
    fail(`${theme}: a failed copy must not paint "Copied"`)
  }
  // Under the code row, not inside it: between chip and link the notice shifted
  // the page link mid-task.
  if (await prompt.locator('[data-testid="signin-code-row"] [data-testid="signin-copy-error"]').count()) {
    fail(`${theme}: the copy failure must sit under the code row, not in it`)
  }
  // The message is the whole remedy. An "Ask the agent" beside a credential-like
  // code read as "no idea where my code would end up" -- and an agent cannot
  // supply a clipboard the browser refused.
  if (await notice.getByRole('button', { name: /ask the agent/i }).count()) {
    fail(`${theme}: the copy failure must not offer an agent hand-off`)
  }
  await page.screenshot({
    path: `${OUT}/08-code-copy-failed-${theme}.png`,
    clip: await clipOf(page, prompt.locator('xpath=..'), 10, 0),
  })
  console.log(`wrote 08 (${theme})`)
  await page.close()
}

// ---- Frame 9: the Setting-up row at phone width -----------------------------
//
// The row's cancel carries the long "Cancel and remove the instance" label in a
// shrink-0 slot; the card frames (06) do not show whether it fits a narrow row.

world.jobs = [LAUNCHING_JOB]
world.instances = []
world.signinAnswer = null
const narrow = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 })
for (const theme of ['dark', 'light']) {
  const page = await narrow.newPage()
  await stubDashboardApi(page, { extra, theme })
  logPageProblems(page)
  await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
  const cancel = page.getByRole('button', { name: /Cancel setup of kc-5e10bb and remove the instance/i })
  await cancel.waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
  const row = cancel.locator('xpath=../..')
  await row.scrollIntoViewIfNeeded()
  // Fully inside the viewport and the row, label unclipped.
  const fit = await cancel.evaluate(el => ({
    right: el.getBoundingClientRect().right,
    clipped: el.scrollWidth > el.clientWidth + 1,
  }))
  if (fit.right > 390) fail(`${theme}: the row's cancel runs off a 390px viewport (right=${fit.right})`)
  if (fit.clipped) fail(`${theme}: the row's cancel label is clipped at 390px`)
  // The row pill, in the same words as the card badge asserted in frame 06.
  if (!(await row.getByText('Setting up').isVisible())) {
    fail(`${theme}: the row pill must say "Setting up"`)
  }
  if (await row.getByText(/Launching…/).count()) {
    fail(`${theme}: the row must not use a second name for the state`)
  }
  const nameEl = row.getByText(/Kiro Crew Cloud \(kc-5e10bb\)/i)
  if (!(await nameEl.isVisible())) fail(`${theme}: the crew name must survive beside the cancel at 390px`)
  // Stacked, not squeezed. The long label in a shrink-0 slot crushed the name and
  // the step line to one word per line for the whole provisioning wait, so below
  // `sm` the button takes its own line and the text keeps the width.
  const cancelBox = await cancel.boundingBox()
  const nameBox = await nameEl.boundingBox()
  if (!(cancelBox.y >= nameBox.y + nameBox.height - 1)) {
    fail(`${theme}: at 390px the cancel must sit BELOW the text, not beside it`)
  }
  const step = row.getByText(/Step \d+ of \d+/)
  const stepBox = await step.boundingBox()
  // A crushed column is narrow; the text block must keep most of the row's width.
  if (stepBox.width < 200) {
    fail(`${theme}: the step line is squeezed to ${Math.round(stepBox.width)}px at 390px`)
  }
  await page.screenshot({
    path: `${OUT}/09-setting-up-row-narrow-${theme}.png`,
    clip: await (async () => {
      await page.waitForTimeout(250)
      const box = await row.boundingBox()
      return { x: 0, y: Math.max(0, box.y - 10), width: 390, height: Math.min(844 - Math.max(0, box.y - 10), box.height + 20) }
    })(),
  })
  console.log(`wrote 09 (${theme})`)
  await page.close()
}
await narrow.close()

// ---- Frame 10: a recheck that finds the approval not landed yet -------------
//
// 409 `no_signin_pending` is the gateway saying it re-probed the box and the
// approval has not landed. Re-rendering the identical screen told the reader
// nothing had run; a red banner told them something broke. The block answers.

world.jobs = [STALE_JOB]
world.instances = []
world.signinAnswer = { status: 409, body: { error: 'no sign-in pending', code: 'no_signin_pending' } }
for (const theme of ['dark', 'light']) {
  const page = await openPage(theme)
  await openSetup(page)
  const prompt = page.locator('[data-testid="signin-prompt"]')
  await prompt.getByRole('button', { name: /I approved it/i }).click()
  const result = prompt.locator('[data-testid="signin-recheck-result"]')
  await result.waitFor({ timeout: 5000 })
  if (!/Not signed in yet/i.test(await result.innerText())) {
    fail(`${theme}: a recheck with no approval yet must say so: ${JSON.stringify(await result.innerText())}`)
  }
  if (await page.getByText(/no sign-in pending/i).count()) {
    fail(`${theme}: the gateway's 409 text must not surface as a red banner`)
  }
  // The code is still there to approve; and the result sits beside the button.
  if (!/OLD-CODE/.test(await prompt.innerText())) fail(`${theme}: the code must survive a recheck`)
  if (!(await prompt.locator('[data-testid="signin-action-row"] [data-testid="signin-recheck-result"]').count())) {
    fail(`${theme}: the recheck result must sit in the row with the button that produced it`)
  }
  await page.screenshot({
    path: `${OUT}/10-recheck-not-yet-${theme}.png`,
    clip: await clipOf(page, prompt.locator('xpath=..'), 10, 0),
  })
  console.log(`wrote 10 (${theme})`)
  await page.close()
}
world.signinAnswer = null

// ---- Frame 11: a fetch/recheck that failed ---------------------------------
//
// The peer of frames 08 (copy failed) and 10 (not signed in yet): the gateway
// call itself failed. Reported beside the button that made it, in plain words --
// the raw transport text ("Failed to fetch") is mapped before it reaches the copy.

world.jobs = [STALE_JOB]
world.instances = []
// An ANSWER that failed (502), not a dead gateway: the agent chat still loads, so
// this frame is the hand-off-present case. "Failed to fetch" here would read as
// the gateway being gone, which is the other branch.
world.signinAnswer = { status: 504, body: { error: 'HTTP 504 Gateway Timeout', code: 'gateway_timeout' } }
for (const theme of ['dark', 'light']) {
  const page = await openPage(theme)
  await openSetup(page)
  const prompt = page.locator('[data-testid="signin-prompt"]')
  await prompt.getByRole('button', { name: /I approved it/i }).click()
  const notice = prompt.locator('[data-testid="signin-fetch-error"]')
  await notice.waitFor({ timeout: 5000 })
  const text = await notice.innerText()
  if (!/didn.t answer in time/i.test(text)) {
    fail(`${theme}: a failed recheck must say so, in plain words: ${JSON.stringify(text)}`)
  }
  // Plain language, not the transport's own words.
  if (/504|Gateway Timeout/i.test(text)) {
    fail(`${theme}: the raw gateway error reached the user copy: ${JSON.stringify(text)}`)
  }
  // 502 means the gateway ANSWERED, badly: the agent chat still loads, so the
  // hand-off stays. It is dropped only when nothing answered at all.
  if (!(await notice.getByRole('button', { name: /ask the agent/i }).isVisible())) {
    fail(`${theme}: a gateway that answered badly keeps the agent hand-off`)
  }
  // In the action row, beside the button that produced it -- and the code stays.
  if (!(await prompt.locator('[data-testid="signin-action-row"] [data-testid="signin-fetch-error"]').count())) {
    fail(`${theme}: the failure must sit in the row with the button that made the call`)
  }
  if (!/OLD-CODE/.test(await prompt.innerText())) {
    fail(`${theme}: the preserved code must survive a failed recheck`)
  }
  await page.screenshot({
    path: `${OUT}/11-recheck-failed-${theme}.png`,
    clip: await clipOf(page, prompt.locator('xpath=..'), 10, 0),
  })
  console.log(`wrote 11 (${theme})`)
  await page.close()
}
world.signinAnswer = null

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
