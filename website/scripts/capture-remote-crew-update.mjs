/**
 * Screenshots of a remote crew that runs an older version: the Remote Crew
 * row's "Behind" badge and its Update action through confirm, each job phase
 * and each outcome, the crew-window header badge, and the offer that lists
 * behind crews after this machine updates.
 *
 * Gateway-free: every /api call is answered by route interception below. Each
 * frame asserts what it must show before it is written, and the script fails
 * if any POST other than the confirmed update reaches the wire.
 *
 *   01-row-behind-<theme>     dev-box behind (badge + Update); build-box current
 *   02-row-confirm-dark       after the first click: the confirmation naming the crew
 *   03-row-updating-dark      after the confirming click: the update phase
 *   04-row-<phase>-dark       a running job in its restart and version-check phases
 *   05-row-<outcome>-dark     succeeded (with and without a version) / unchanged /
 *                             unverified / failed / restart-failed
 *   06-offer-<theme>          the post-update offer listing only the behind crew
 *   07-window-<width>-dark    the crew window header at a wide and a 408px pane
 *   08-row-read-failed-dark   the versions read failed: an error, not a quiet row
 *   09-row-refused-dark       the gateway refused the start (a job already runs)
 *   10-offer-read-failed-dark the post-update offer when the versions read failed
 *   11-offer-narrow-failed-dark  the offer on a 320x568 phone with three failed
 *                             updates and long error tails: the card stays on
 *                             screen, Dismiss stays visible, and the body scrolls
 *   12-offer-long-name-dark   the offer on a 320x568 phone, armed for a crew with
 *                             a 128-character unbroken name: confirm and Cancel
 *                             stay on screen
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort
 *   node scripts/capture-remote-crew-update.mjs http://127.0.0.1:6841 ../temp-screenshots/remote-crew-update
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/remote-crew-update'
mkdirSync(OUT, { recursive: true })

const crew = (id, name, host) => ({
  id, name, connection_method: 'ssh', ssh_host: host, ssm_target: '', aws_profile: '', aws_region: '',
  ssm_run_as: '', remote_port: 5476, local_port: 53710, ttl: '20h', remote_bin: '', was_connected: true,
  status: { instance_id: id, state: 'connected', local_port: 53710, remote_port: 5476 },
})
const INSTANCES = { active: true, warm_set_cap: 5, instances: [crew('dev', 'dev-box', 'dev-box'), crew('build', 'build-box', 'build-box')] }
const VERSIONS = {
  local_version: '0.9.1',
  instances: [
    { id: 'dev', name: 'dev-box', version: '0.9.0', behind: true, updatable: true },
    { id: 'build', name: 'build-box', version: '0.9.1', behind: false, updatable: true },
  ],
}
// Three behind crews for the narrow offer frame, each with a failed update.
const MANY_BEHIND = {
  local_version: '0.9.1',
  instances: ['dev-box', 'build-box', 'test-box'].map(name => ({ id: name, name, version: '0.9.0', behind: true, updatable: true })),
}
const LONG_TAIL = 'Collecting kiro-crew==0.9.1 ... ERROR: Could not find a version that satisfies the requirement '
  + 'kiro-crew==0.9.1 (from versions: 0.8.4, 0.8.5, 0.9.0). ERROR: No matching distribution found for kiro-crew==0.9.1. '
  + 'The release feed answered 404 for the requested build; check that this crew can reach the release channel '
  + 'and that its configured index serves the new version. Retrying the download after a network timeout: '
  + 'connection to the package index timed out after 30 seconds while fetching the wheel metadata for kiro-crew. '
  + 'Update aborted; the installed version is unchanged and the gateway keeps running.'
const CAPS = {
  instance_id: 'dev', version: '0.9.0', local_version: '0.9.1', version_match: true, behind: true,
  agents: [], default_agent: '', models: [], effort_levels: [], workspaces: [], default_workspace: '', unavailable: {},
}
const SLOT = { key: 'release-notes', title: 'Draft the release notes', running: false }
const DETAIL = {
  title: 'Draft the release notes', running: false,
  messages: [
    { role: 'user', content: 'Draft the release notes for the next build.', ts: 't1' },
    { role: 'assistant', content: 'Here is a first draft, grouped by area.', ts: 't2' },
  ],
}
const job = (over) => ({
  state: 'running', phase: 'updating', started_at: 1, finished_at: null,
  elapsed_secs: 12, error: '', version_before: '0.9.0', version_after: '', ...over,
})

const browser = await chromium.launch()
let failed = false
function check(name, ok) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) failed = true
}

async function open(view, theme, { jobState = null, width = 1000, height = 620, versionsFail = false, refuse = false, many = false, longName = '' } = {}) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 })
  const posts = []
  // A finished update re-reads the versions; the crew is current from then on,
  // as it would be once it came back on the new version.
  let versionReads = 0
  const versionsNow = () => {
    versionReads += 1
    if (many) return MANY_BEHIND
    if (longName) return { ...VERSIONS, instances: [{ id: 'dev', name: longName, version: '0.9.0', behind: true, updatable: true }] }
    if (jobState?.state !== 'succeeded' || versionReads === 1) return VERSIONS
    return { ...VERSIONS, instances: VERSIONS.instances.map(r => (r.id === 'dev' ? { ...r, version: '0.9.1', behind: false } : r)) }
  }
  // A predicate, not a `**/api/**` glob: the glob also matches the dev server's
  // own `/src/api/...` modules and would answer them with JSON.
  await page.route(url => new URL(url).pathname.startsWith('/api/'), async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (req.method() === 'POST') posts.push(path)
    if (path === '/api/instances' && req.method() === 'GET') return json(INSTANCES)
    if (path === '/api/instances/versions') {
      return versionsFail
        ? json({ error: 'dev-box did not answer its version read', code: 'capability_unreachable' }, 502)
        : json(versionsNow())
    }
    if (path === '/api/instances/dev/update' && req.method() === 'POST') {
      return refuse
        ? json({ ok: false, code: 'already_running', error: 'dev-box is already updating' }, 409)
        : json({ ok: true, job: job({}) }, 202)
    }
    if (/^\/api\/instances\/[^/]+\/update$/.test(path)) return json({ job: many || path.includes('/dev/') ? jobState : null })
    if (path === '/api/instances/dev/capabilities') return json(CAPS)
    if (path === '/api/instances/dev/proxy/api/chat/slots') return json([SLOT])
    if (path === '/api/instances/dev/proxy/api/chat/slots/release-notes') return json(DETAIL)
    if (path === '/api/instances/dev/proxy/api/stream') return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': ok\n\n' })
    if (path === '/api/cloud/launch') return json({ jobs: [] })
    if (path === '/api/cloud/provisioners') return json({ provisioners: [] })
    if (path === '/api/cloud/identity') return json({ identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' } })
    return json({})
  })
  await page.goto(`${BASE}/capture/remote-crew-update.html?view=${view}&theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  return { page, posts }
}

for (const theme of ['dark', 'light']) {
  const { page, posts } = await open('panel', theme)
  await page.getByText('Behind: remote 0.9.0, local 0.9.1').waitFor({ timeout: 20_000 })
  check(`01-row-behind-${theme} one badge`, (await page.getByTestId('remote-behind-badge').count()) === 1)
  check(`01-row-behind-${theme} Update offered`, (await page.getByRole('button', { name: 'Update dev-box' }).count()) === 1)
  check(`01-row-behind-${theme} no POST`, posts.length === 0)
  await page.screenshot({ path: `${OUT}/01-row-behind-${theme}.png` })
  if (theme === 'dark') {
    await page.getByRole('button', { name: 'Update dev-box' }).click()
    await page.getByText('Runs the update on “dev-box”, then restarts it.').waitFor()
    check('02-row-confirm-dark no POST yet', posts.length === 0)
    await page.screenshot({ path: `${OUT}/02-row-confirm-dark.png` })
    await page.getByRole('button', { name: /Update “dev-box” now/ }).click()
    await page.getByText('Updating dev-box…').waitFor()
    check('03-row-updating-dark exactly one POST', posts.length === 1 && posts[0] === '/api/instances/dev/update')
    await page.screenshot({ path: `${OUT}/03-row-updating-dark.png` })
  }
  await page.close()
}

const FRAMES = [
  ['04-row-restarting-dark', job({ phase: 'restarting' }), 'Restarting dev-box…', true],
  ['04-row-verifying-dark', job({ phase: 'verifying' }), "Checking dev-box's version…", true],
  ['05-row-succeeded-dark', job({ state: 'succeeded', phase: 'done', finished_at: 2, version_after: '0.9.1' }), 'dev-box now runs 0.9.1.', false],
  ['05-row-unchanged-dark', job({ state: 'unchanged', phase: 'done', finished_at: 2, version_after: '0.9.0' }), /still runs 0\.9\.0: its updater installed nothing/, true],
  ['05-row-succeeded-unknown-dark', job({ state: 'succeeded', phase: 'done', finished_at: 2, version_before: '', version_after: '' }), 'dev-box is updated and restarted. It reconnects on its own.', false],
  ['05-row-unverified-dark', job({ state: 'unverified', phase: 'done', finished_at: 2 }), /has not reconnected yet to confirm its version/, false],
  ['05-row-failed-dark', job({ state: 'failed', finished_at: 2, error: 'Could not reach the release feed: connection timed out' }), /The update on dev-box failed/, true],
  ['05-row-restart-failed-dark', job({ state: 'failed', phase: 'restarting', finished_at: 2, error: 'Failed to restart kirocrew.service: Access denied' }), /The new version is installed on dev-box, but its restart failed/, true],
]
for (const [name, state, text, offersUpdate] of FRAMES) {
  const { page, posts } = await open('panel', 'dark', { jobState: state })
  await page.getByText(text).first().waitFor({ timeout: 20_000 })
  const updates = await page.getByRole('button', { name: 'Update dev-box' }).count()
  const running = state.state === 'running'
  check(`${name} Update ${offersUpdate && !running ? 'offered' : 'withheld'}`, running ? updates === 0 : (updates === 1) === offersUpdate)
  check(`${name} no POST`, posts.length === 0)
  if (state.state === 'failed') {
    const step = state.phase === 'restarting' ? /keeps running its old version until it restarts/ : /then select Try again/
    check(`${name} next step`, (await page.getByText(step).count()) === 1)
  }
  if (state.state === 'unverified') check(`${name} stale badge hidden`, (await page.getByTestId('remote-behind-badge').count()) === 0)
  if (state.state === 'succeeded') {
    await page.getByTestId('remote-behind-badge').waitFor({ state: 'detached', timeout: 20_000 })
    check(`${name} outcome stays after the badge goes`, (await page.getByText(text).count()) === 1)
  }
  await page.screenshot({ path: `${OUT}/${name}.png` })
  await page.close()
}

{
  const { page, posts } = await open('panel', 'dark', { versionsFail: true })
  await page.getByText("Could not read your remote crews' versions").first().waitFor({ timeout: 20_000 })
  check('08-row-read-failed-dark no badge', (await page.getByTestId('remote-behind-badge').count()) === 0)
  check('08-row-read-failed-dark no POST', posts.length === 0)
  await page.screenshot({ path: `${OUT}/08-row-read-failed-dark.png` })
  await page.close()
}

{
  const { page, posts } = await open('panel', 'dark', { refuse: true })
  await page.getByRole('button', { name: 'Update dev-box' }).click({ timeout: 20_000 })
  await page.getByRole('button', { name: /Update “dev-box” now/ }).click()
  await page.getByText('dev-box is already updating').waitFor({ timeout: 20_000 })
  check('09-row-refused-dark one POST, refused', posts.length === 1)
  await page.screenshot({ path: `${OUT}/09-row-refused-dark.png` })
  await page.close()
}

for (const theme of ['dark', 'light']) {
  const { page, posts } = await open('offer', theme, { height: 360 })
  const offer = page.getByTestId('remote-update-offer')
  await offer.waitFor({ timeout: 20_000 })
  // The card rises in; shoot it settled, not mid-fade.
  await offer.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)))
  const text = await offer.innerText()
  check(`06-offer-${theme} lists dev-box`, text.includes('dev-box: 0.9.0'))
  check(`06-offer-${theme} omits build-box`, !text.includes('build-box'))
  check(`06-offer-${theme} says it confirms first`, text.includes('Each update asks you to confirm first.'))
  check(`06-offer-${theme} no POST`, posts.length === 0)
  await page.screenshot({ path: `${OUT}/06-offer-${theme}.png` })
  await page.close()
}

{
  const { page, posts } = await open('offer', 'dark', { height: 360, versionsFail: true })
  const offer = page.getByTestId('remote-update-offer')
  await offer.waitFor({ timeout: 20_000 })
  await offer.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)))
  const text = await offer.innerText()
  check('10-offer-read-failed-dark says the check failed', text.includes("Could not check your remote crews' versions after this update"))
  check('10-offer-read-failed-dark no POST', posts.length === 0)
  await page.screenshot({ path: `${OUT}/10-offer-read-failed-dark.png` })
  await page.close()
}

{
  const W = 320
  const H = 568
  const failedJob = job({ state: 'failed', finished_at: 2, error: LONG_TAIL })
  const { page, posts } = await open('offer', 'dark', { width: W, height: H, many: true, jobState: failedJob })
  const offer = page.getByTestId('remote-update-offer')
  await offer.waitFor({ timeout: 20_000 })
  await page.getByText(/The update on test-box failed/).waitFor({ timeout: 20_000 })
  await offer.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)))
  const card = await offer.boundingBox()
  check('11-offer-narrow-failed-dark card inside the viewport', !!card && card.y >= 0 && card.y + card.height <= H)
  const dismiss = await page.getByRole('button', { name: 'Dismiss' }).boundingBox()
  check('11-offer-narrow-failed-dark Dismiss visible', !!dismiss && dismiss.y >= 0 && dismiss.y + dismiss.height <= H)
  const body = page.getByTestId('remote-update-offer-body')
  check('11-offer-narrow-failed-dark body scrolls', await body.evaluate(el => el.scrollHeight > el.clientHeight))
  check('11-offer-narrow-failed-dark no POST', posts.length === 0)
  await page.screenshot({ path: `${OUT}/11-offer-narrow-failed-dark.png` })
  // The last crew's Try again is reachable by scrolling the body. Its accessible
  // name is "Update <crew>" whichever label it shows.
  const last = page.getByRole('button', { name: 'Update test-box' })
  await last.scrollIntoViewIfNeeded()
  const lastBox = await last.boundingBox()
  check('11-offer-narrow-failed-dark last Try again reachable', !!lastBox && lastBox.y >= 0 && lastBox.y + lastBox.height <= H)
  await page.screenshot({ path: `${OUT}/11-offer-narrow-failed-scrolled-dark.png` })
  await page.close()
}

{
  const W = 320
  const H = 568
  const longName = 'x'.repeat(128)
  const { page, posts } = await open('offer', 'dark', { width: W, height: H, longName })
  const offer = page.getByTestId('remote-update-offer')
  await offer.waitFor({ timeout: 20_000 })
  await offer.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)))
  await page.getByRole('button', { name: `Update ${longName}` }).click()
  const confirm = page.getByRole('button', { name: new RegExp(`Update “${longName}” now`) })
  await confirm.waitFor()
  const inside = box => !!box && box.x >= 0 && box.x + box.width <= W && box.y >= 0 && box.y + box.height <= H
  check('12-offer-long-name-dark confirm inside the viewport', inside(await confirm.boundingBox()))
  check('12-offer-long-name-dark Cancel inside the viewport', inside(await page.getByRole('button', { name: 'Cancel' }).boundingBox()))
  check('12-offer-long-name-dark card inside the viewport', inside(await offer.boundingBox()))
  check('12-offer-long-name-dark no POST', posts.length === 0)
  await page.screenshot({ path: `${OUT}/12-offer-long-name-dark.png` })
  await page.close()
}

for (const [label, width] of [['wide', 1000], ['narrow', 408]]) {
  const { page } = await open('window', 'dark', { width, height: 420 })
  await page.getByText('Behind: remote 0.9.0, local 0.9.1').waitFor({ timeout: 20_000 })
  const close = await page.getByRole('button', { name: 'Close' }).boundingBox()
  check(`07-window-${label}-dark Close fully visible`, !!close && close.x >= 0 && close.x + close.width <= width)
  check(`07-window-${label}-dark links to Remote Crew`, (await page.getByRole('link', { name: 'Update dev-box in Remote Crew' }).count()) === 1)
  await page.screenshot({ path: `${OUT}/07-window-${label}-dark.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('one or more frames did not show what they must')
  process.exit(1)
}
