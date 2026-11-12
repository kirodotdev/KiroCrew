/**
 * Screenshots of a Remote Crew update that waits for the crew's running work:
 * the waiting phase with its counts, the two ways a job stops short of the
 * restart (still busy at the ceiling, a crew too old to wait), and the
 * confirmation "Restart now" asks before it restarts without waiting.
 *
 * Gateway-free: every /api call is answered by route interception below. Each
 * frame asserts what it must show before it is written, and the script fails
 * if any POST other than the confirmed forced update reaches the wire.
 *
 *   01-waiting-dark            a running job in waiting_for_idle, with its counts
 *   02-still-busy-<theme>      a job that gave up waiting: the message and Restart now
 *   03-cannot-wait-dark        a crew too old to wait: the message and Restart now
 *   04-restart-now-confirm-dark  after clicking Restart now: the confirmation
 *   05-restart-now-sent-dark   after confirming: exactly one POST, with force
 *   06-idle-check-failed-dark  a crew that could not check its work: the reason and Restart now
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort
 *   node scripts/capture-29435-remote-update-wait.mjs http://127.0.0.1:6841 ../temp-screenshots/29435-remote-update-wait
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/29435-remote-update-wait'
mkdirSync(OUT, { recursive: true })

const crew = (id, name, host) => ({
  id, name, connection_method: 'ssh', ssh_host: host, ssm_target: '', aws_profile: '', aws_region: '',
  ssm_run_as: '', remote_port: 5476, local_port: 53710, ttl: '20h', remote_bin: '', was_connected: true,
  status: { instance_id: id, state: 'connected', local_port: 53710, remote_port: 5476 },
})
const INSTANCES = { active: true, warm_set_cap: 5, instances: [crew('dev', 'dev-box', 'dev-box')] }
const VERSIONS = {
  local_version: '0.11.2',
  instances: [{ id: 'dev', name: 'dev-box', version: '0.11.1', behind: true, updatable: true }],
}
const job = (over) => ({
  state: 'running', phase: 'updating', started_at: 1, finished_at: null, elapsed_secs: 40,
  error: '', code: '', waiting: { turns: 0, background: 0 }, force: false,
  version_before: '0.11.1', version_after: '', ...over,
})

const browser = await chromium.launch()
let failed = false
function check(name, ok) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) failed = true
}

async function open(theme, jobState) {
  const page = await browser.newPage({ viewport: { width: 1000, height: 620 }, deviceScaleFactor: 2 })
  const posts = []
  // A predicate, not a `**/api/**` glob: the glob also matches the dev server's
  // own `/src/api/...` modules and would answer them with JSON.
  await page.route(url => new URL(url).pathname.startsWith('/api/'), async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (req.method() === 'POST') posts.push({ path, body: req.postData() })
    if (path === '/api/instances' && req.method() === 'GET') return json(INSTANCES)
    if (path === '/api/instances/versions') return json(VERSIONS)
    if (path === '/api/instances/dev/update' && req.method() === 'POST') {
      return json({ ok: true, job: job({ phase: 'restarting', force: true }) }, 202)
    }
    if (path === '/api/instances/dev/update') return json({ job: jobState })
    if (path === '/api/cloud/launch') return json({ jobs: [] })
    if (path === '/api/cloud/provisioners') return json({ provisioners: [] })
    if (path === '/api/cloud/identity') return json({ identity: { account_type: 'BuilderId' }, suggested_target: { license: '', start_url: '', region: '' } })
    return json({})
  })
  await page.goto(`${BASE}/capture/remote-crew-update.html?view=panel&theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  return { page, posts }
}

{
  const { page, posts } = await open('dark', job({ phase: 'waiting_for_idle', waiting: { turns: 2, background: 3 } }))
  await page.getByText(/Waiting for dev-box to finish its running work/).waitFor({ timeout: 20_000 })
  check('01-waiting-dark shows the counts', (await page.getByText(/chat turns: 2, background tasks: 3/).count()) === 1)
  check('01-waiting-dark no Update while it runs', (await page.getByRole('button', { name: 'Update dev-box' }).count()) === 0)
  check('01-waiting-dark no POST', posts.length === 0)
  await page.screenshot({ path: `${OUT}/01-waiting-dark.png` })
  await page.close()
}

const STOPPED = [
  ['02-still-busy', 'still_busy', /still running work after 10 minutes, so it was not restarted/],
  ['03-cannot-wait', 'peer_cannot_wait', /cannot report its running work, so it was not restarted/],
  ['06-idle-check-failed', 'idle_check_failed', /could not check its running work, so it was not restarted/],
]
for (const [name, code, text] of STOPPED) {
  for (const theme of name === '02-still-busy' ? ['dark', 'light'] : ['dark']) {
    const state = job({ state: 'failed', phase: 'waiting_for_idle', code, finished_at: 2, waiting: { turns: 1, background: 1 } })
    const { page, posts } = await open(theme, state)
    await page.getByText(text).waitFor({ timeout: 20_000 })
    check(`${name}-${theme} Restart now offered`, (await page.getByRole('button', { name: 'Restart now' }).count()) === 1)
    // A crew too old to wait gets no Try again: a retry ends the same way.
    const tryAgain = code === 'peer_cannot_wait' ? 0 : 1
    check(`${name}-${theme} Try again offered ${tryAgain}x`, (await page.getByRole('button', { name: 'Update dev-box' }).count()) === tryAgain)
    check(`${name}-${theme} no generic next step`, (await page.getByText(/Fix the cause on dev-box/).count()) === 0)
    check(`${name}-${theme} no POST`, posts.length === 0)
    await page.screenshot({ path: `${OUT}/${name}-${theme}.png` })
    if (name === '02-still-busy' && theme === 'dark') {
      await page.getByRole('button', { name: 'Restart now' }).click()
      const confirm = page.getByRole('button', { name: /Restart “dev-box” and stop its work/ })
      await confirm.waitFor()
      check('04-restart-now-confirm-dark no POST yet', posts.length === 0)
      await page.screenshot({ path: `${OUT}/04-restart-now-confirm-dark.png` })
      await confirm.click()
      await page.getByText('Restarting dev-box…').waitFor({ timeout: 20_000 })
      check(
        '05-restart-now-sent-dark exactly one forced POST',
        posts.length === 1 && posts[0].path === '/api/instances/dev/update' && JSON.parse(posts[0].body || '{}').force === true,
      )
      await page.screenshot({ path: `${OUT}/05-restart-now-sent-dark.png` })
    }
    await page.close()
  }
}

await browser.close()
if (failed) {
  console.error('one or more frames did not show what they must')
  process.exit(1)
}
