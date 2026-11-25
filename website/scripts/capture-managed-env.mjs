/**
 * Screenshots of the managed-environment lane's form and row actions, gateway-free.
 *
 *   01-form-<theme>        setup tab: the form, empty, with no ssh settings yet: "Allow this computer to connect" beside Launch, with the reason
 *   01b-form-preview-dark  the lines a write adds, open, with the button below them
 *   02-form-invalid-dark   a crew name that breaks the rule: the hint, Launch held
 *   03-form-valid-dark     after the ssh settings are added and a valid name: the check line, Launch enabled
 *   08-ssh-failed-dark     the ssh write refused: the error, no hand-off (the name is unsaved)
 *   04-rows-<theme>        Remote Crew rows: a running (Stop, Delete) and a stopped (Resume, Delete) environment
 *   07-delete-confirm-dark Delete asks first
 *   09-action-failed-dark  Stop refused by the service: the error on the row
 *   10-list-failed-dark    the List read failed: the error on each row
 *   11-row-gone-dark       a deleted environment: gone, no action, Remove the crew
 *   12-form-needs-ssh-dark a valid name but no ssh settings: the reason beside Launch
 *   13-row-states-dark     environments starting, being deleted and failed
 *
 * Usage (from website/):
 *   npx vite --host 127.0.0.1 --port 6893 --strictPort
 *   node scripts/capture-managed-env.mjs http://127.0.0.1:6893 /tmp/managed-env-evidence
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6893'
const OUT = process.argv[3] || '/tmp/managed-env-evidence'
mkdirSync(OUT, { recursive: true })

const crew = (id, name, host) => ({
  id, name, connection_method: 'ssh', ssh_host: host, ssm_target: '', aws_profile: '', aws_region: '',
  ssm_run_as: '', remote_port: 5476, local_port: 53710, ttl: '20h', remote_bin: '', was_connected: true,
  provisioner_id: 'kiro_managed_env',
  status: { instance_id: id, state: 'connected', local_port: 53710, remote_port: 5476 },
})
const INSTANCES = { active: true, warm_set_cap: 5, instances: [crew('a1', 'docs-crew', 'crew-sbx-a1'), crew('b2', 'test-crew', 'crew-sbx-b2')] }
const LIST = { sandboxes: [
  { sandbox_id: 'a1', status: 'RUNNING', own: true, ssh_host: 'crew-sbx-a1' },
  { sandbox_id: 'b2', status: 'STOPPED', own: true, ssh_host: 'crew-sbx-b2' },
] }
const STATES_INSTANCES = { active: true, warm_set_cap: 5, instances: [
  crew('c1', 'new-crew', 'crew-sbx-c1'), crew('c2', 'old-crew', 'crew-sbx-c2'), crew('c3', 'bad-crew', 'crew-sbx-c3'),
] }
const STATES_LIST = { sandboxes: [
  { sandbox_id: 'c1', status: 'PENDING', own: true, ssh_host: 'crew-sbx-c1' },
  { sandbox_id: 'c2', status: 'DELETING', own: true, ssh_host: 'crew-sbx-c2' },
  { sandbox_id: 'c3', status: 'FAILED', own: true, ssh_host: 'crew-sbx-c3' },
] }
const PROVISIONERS = { provisioners: [
  { id: 'aws_ec2', kind: 'aws_ec2', label: 'AWS EC2', posix_only: true, steps: [] },
  { id: 'kiro_managed_env', kind: 'kiro_managed_env', label: 'Kiro Managed Environments', posix_only: false, steps: [] },
] }

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failed = false
const check = (name, ok) => { console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'}`); if (!ok) failed = true }

async function open(theme, { sshPresent = false, sshFail = false, listFail = false, actFail = false, gone = false, states = false } = {}) {
  let ssh = sshPresent
  const page = await browser.newPage({ viewport: { width: 960, height: 640 }, deviceScaleFactor: 2 })
  const posts = []
  page.on('pageerror', e => console.log('pageerror', String(e)))
  await page.route(url => new URL(url).pathname.startsWith('/api/'), async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (req.method() === 'POST' && !path.endsWith('/ssh-config')) posts.push(path)
    if (path === '/api/instances' && req.method() === 'GET') return json(states ? STATES_INSTANCES : INSTANCES)
    if (path === '/api/managed-environments/list') {
      if (listFail) return json({ code: 'service', error: 'ListSandboxes failed (HTTP 502)' }, 502)
      if (states) return json(STATES_LIST)
      // A deleted own id the List no longer shows comes back as GONE, with no ssh host.
      return json(gone ? { sandboxes: [LIST.sandboxes[0], { sandbox_id: 'b2', status: 'GONE', own: true }] } : LIST)
    }
    if (path === '/api/managed-environments/ssh-config') {
      if (req.method() === 'POST') {
        if (sshFail) return json({ code: 'ssh_config_unwritable', error: 'cannot read /home/you/.ssh/config (PermissionError)' }, 409)
        ssh = true
      }
      return json({ present: ssh, path: '/home/you/.ssh/config', block: "# >>> kirocrew crew-sbx (managed block; do not edit between the markers) >>>\nHost crew-sbx-*\n  User root\n  ProxyCommand /home/you/.local/share/kirocrew-venv-current/bin/python -P -m kiro_crew.managed_env.mint_and_attach %n\n  PreferredAuthentications none\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n  ForwardAgent no\n  ForwardX11 no\n  GatewayPorts no\n  ServerAliveInterval 15\n  ConnectTimeout 60\n  LogLevel ERROR\nHost *\n# <<< kirocrew crew-sbx <<<\n" })
    }
    if (/^\/api\/managed-environments\/[^/]+\/(stop|resume|delete)$/.test(path)) {
      if (actFail) return json({ code: 'backoff', error: 'The service is pausing requests for 60 s after an error. Try again then.' }, 409)
      return json({ status: 'STOPPING' })
    }
    if (path === '/api/cloud/provisioners') return json(PROVISIONERS)
    if (path === '/api/cloud/launch') return json({ jobs: [] })
    if (path === '/api/instances/versions') return json({ local_version: '0.9.1', instances: [] })
    if (path === '/api/auth/me') return json({ owner_ok: true })
    return json({})
  })
  await page.goto(`${BASE}/capture/managed-env.html?theme=${theme}`, { timeout: 240_000 })
  await page.waitForSelector('[data-capture-root]')
  return { page, posts }
}

async function expandRows(page, ready = '[data-state="stopped"]') {
  for (const name of ['docs-crew', 'test-crew']) await page.getByRole('button', { name: `Show details for ${name}` }).click()
  await page.locator(ready).first().waitFor({ timeout: 20_000 })
  // Off the toggles, so no hover tooltip is in the frame.
  await page.mouse.move(900, 620)
  await page.waitForTimeout(800)
}

async function openSetup(page) {
  await page.getByRole('button', { name: /add crew/i }).first().click()
  await page.waitForTimeout(400)
  await page.getByRole('button', { name: /set up|launch a new/i }).first().click()
  await page.getByText('Crew name', { exact: true }).waitFor({ timeout: 20_000 })
  await page.waitForTimeout(400)
}

for (const theme of ['dark', 'light']) {
  const { page, posts } = await open(theme)
  await page.getByText('docs-crew').first().waitFor({ timeout: 20_000 })
  await expandRows(page)
  check(`04-rows-${theme} running state shown`, (await page.locator('[data-state="running"]').count()) === 1)
  check(`04-rows-${theme} stopped state shown`, (await page.locator('[data-state="stopped"]').count()) === 1)
  check(`04-rows-${theme} no overflow menu`, (await page.getByRole('button', { name: /More actions for/ }).count()) === 0)
  check(`04-rows-${theme} Stop and Resume`, (await page.getByRole('button', { name: 'Stop environment' }).count()) === 1 && (await page.getByRole('button', { name: 'Resume environment' }).count()) === 1)
  await page.screenshot({ path: `${OUT}/04-rows-${theme}.png` })
  await openSetup(page)
  const launch = page.getByRole('button', { name: /^Launch$/ })
  check(`01-form-${theme} Launch held while empty`, await launch.isDisabled())
  if (theme === 'dark') { await page.getByText('See the lines it adds').click(); await page.waitForTimeout(300) }
  await page.screenshot({ path: `${OUT}/01-form-${theme}.png` })
  if (theme === 'dark') {
    await page.getByRole('button', { name: /^Launch$/ }).scrollIntoViewIfNeeded()
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/01b-form-preview-dark.png` })
  }
  if (theme === 'dark') {
    await page.getByPlaceholder(/./).last().fill('Docs--Crew')
    await page.waitForTimeout(300)
    check('02-form-invalid Launch held', await launch.isDisabled())
    await page.screenshot({ path: `${OUT}/02-form-invalid-dark.png` })
    await page.getByRole('button', { name: 'Allow this computer to connect' }).click()
    await page.getByText('/home/you/.ssh/config has the ssh settings').waitFor({ timeout: 10_000 })
    await page.getByPlaceholder(/./).last().fill('docs-crew-2')
    await page.waitForTimeout(300)
    check('03-form-valid Launch enabled', await launch.isEnabled())
    await page.screenshot({ path: `${OUT}/03-form-valid-dark.png` })
  }
  check(`${theme} no POST`, posts.length === 0)
  await page.close()
}

{
  const { page, posts } = await open('dark')
  await page.getByText('docs-crew').first().waitFor({ timeout: 20_000 })
  await expandRows(page)
  await page.getByRole('button', { name: 'Delete environment' }).first().click()
  await page.getByRole('dialog').waitFor({ timeout: 10_000 })
  check('07-delete-confirm no POST before confirm', posts.length === 0)
  await page.screenshot({ path: `${OUT}/07-delete-confirm-dark.png` })
  await page.keyboard.press('Escape')
  await page.close()
}

{
  const { page } = await open('dark', { sshFail: true })
  await page.getByText('docs-crew').first().waitFor({ timeout: 20_000 })
  await openSetup(page)
  await page.getByRole('button', { name: 'Allow this computer to connect' }).click()
  await page.getByText("Couldn't add the ssh settings").waitFor({ timeout: 10_000 })
  check('08-ssh-failed Launch held', await page.getByRole('button', { name: /^Launch$/ }).isDisabled())
  await page.screenshot({ path: `${OUT}/08-ssh-failed-dark.png` })
  await page.close()
}

{
  const { page } = await open('dark', { actFail: true })
  await page.getByText('docs-crew').first().waitFor({ timeout: 20_000 })
  await expandRows(page)
  await page.getByRole('button', { name: 'Stop environment' }).click()
  await page.getByText("Couldn't stop the environment of docs-crew").waitFor({ timeout: 10_000 })
  await page.mouse.move(900, 620)
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/09-action-failed-dark.png` })
  await page.close()
}

{
  const { page } = await open('dark', { listFail: true })
  await page.getByText('docs-crew').first().waitFor({ timeout: 20_000 })
  await expandRows(page, 'text=ListSandboxes failed')
  check('10-list-failed shown on both rows', (await page.getByText('ListSandboxes failed (HTTP 502)').count()) === 2)
  await page.screenshot({ path: `${OUT}/10-list-failed-dark.png` })
  await page.close()
}

{
  const { page } = await open('dark', { gone: true })
  await page.getByText('docs-crew').first().waitFor({ timeout: 20_000 })
  await expandRows(page, 'text=no longer in your managed environments')
  await page.screenshot({ path: `${OUT}/11-row-gone-dark.png` })
  await page.close()
}

{
  const { page } = await open('dark')
  await page.getByText('docs-crew').first().waitFor({ timeout: 20_000 })
  await openSetup(page)
  await page.getByPlaceholder(/./).last().fill('docs-crew-2')
  await page.getByText('Allow this computer to connect, then launch.').waitFor({ timeout: 10_000 })
  await page.mouse.move(900, 620)
  await page.screenshot({ path: `${OUT}/12-form-needs-ssh-dark.png` })
  await page.close()
}

{
  const { page } = await open('dark', { states: true })
  await page.getByText('new-crew').first().waitFor({ timeout: 20_000 })
  for (const name of ['new-crew', 'old-crew', 'bad-crew']) await page.getByRole('button', { name: `Show details for ${name}` }).click()
  await page.locator('[data-state="error"]').waitFor({ timeout: 20_000 })
  check('13-row-states shown', (await page.locator('[data-state="creating"], [data-state="deleting"], [data-state="error"]').count()) === 3)
  await page.mouse.move(900, 620)
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/13-row-states-dark.png`, fullPage: true })
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
