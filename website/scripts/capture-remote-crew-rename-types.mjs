/**
 * Screenshot harness for the Remote Crew list's source/transport badges, the
 * Edit-settings rename path, and the two in-flight refusals.
 *
 * Five frames, one per claim a reader has to be able to verify:
 *
 *  1. The row menu beside rows of every kind — an EC2-provisioned crew over SSM,
 *     an EC2-provisioned crew over SSH, and a hand-added SSH crew — so the source
 *     badge and the transport badge can be told apart. The menu carries no
 *     Rename item: renaming lives in Edit settings. The stamped EC2 rows show
 *     the stamped caption, remedy included ("delete it in the AWS console"),
 *     never the hedging "cannot verify" copy.
 *  2. Edit settings on the stamped EC2 row: the full edit form under an
 *     "Edit <crew>" heading with the Name field editable — renaming is this
 *     form's Name field, not a separate mode.
 *  3. The draft refusal: one row holds typed changes, Edit settings on another
 *     row is refused at THAT row with the save-or-cancel wording.
 *  4. The frozen form: a save is in flight (its PATCH never answers here), so
 *     the live edit form's fields and Save are disabled and look it; Stop waiting
 *     stays live as the way out.
 *     Nothing else on the page is locked; leaving the page aborts the request.
 *  5. After Stop waiting: the same form back in the editable state — fields
 *     enabled, the typed Name and host drafts kept, a role="status" note
 *     ("Stopped waiting…") naming the outcome, and the exit button reading
 *     Cancel again. The PATCH was aborted client-side; the stub never answered
 *     it. Frame 4 is also captured once in the LIGHT theme (04b), on a second
 *     page, as the frozen-form claim is theme-independent but the evidence
 *     should exist in both palettes.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures — no gateway, no crews.
 *
 * Usage: npm run build && node scripts/capture-remote-crew-rename-types.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/remote-crew-rename-types'
mkdirSync(OUT, { recursive: true })

const crew = (id, name, extra) => ({
  id, name, ssh_host: '', remote_port: 5476, local_port: 0, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: id, state: 'disconnected', local_port: 0, remote_port: 5476 },
  ...extra,
})

const CREWS = [
  crew('ec2-ssm', 'build-farm', {
    connection_method: 'ssm', ssm_target: 'i-0a1b2c3d4e5f60718', ssm_run_as: 'ec2-user',
    aws_profile: 'dev', aws_region: 'us-west-2', provisioner_id: 'aws_ec2',
  }),
  crew('ec2-ssh', 'gpu-box', { ssh_host: 'gpu-box.internal', provisioner_id: 'aws_ec2' }),
  crew('manual', 'dev-box-1', { ssh_host: 'dev-box-1' }),
  crew('manual-2', 'dev-box-2', { ssh_host: 'dev-box-2', remote_port: 7788 }),
]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

// Frame 4 needs a save whose request never settles. The route handler simply
// never answers a PATCH once this flag is up.
let holdPatch = false
const extra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/instances' && method === 'GET') {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ active: true, instances: CREWS, warm_set_cap: 10, sso: SSO }),
    })
    return true
  }
  const one = /^\/api\/instances\/([^/]+)$/.exec(path)
  if (one && method === 'PATCH') {
    if (holdPatch) return true // deliberately left pending
    const found = CREWS.find((c) => c.id === decodeURIComponent(one[1]))
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(found) })
    return true
  }
  if (path === '/api/cloud/launch') {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: [] }) })
    return true
  }
  if (path === '/api/cloud/provisioners') {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ provisioners: [{ id: 'aws_ec2', kind: 'aws_ec2', label: 'Amazon EC2', posix_only: true, steps: [] }] }),
    })
    return true
  }
  if (path.startsWith('/api/cloud/')) {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    return true
  }
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1200, height: 860 }, deviceScaleFactor: 2 })
const page = await context.newPage()
await stubDashboardApi(page, { extra })
logPageProblems(page)

await page.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
await page.getByText('dev-box-1', { exact: true }).first().waitFor({ timeout: 20000 })
await page.waitForTimeout(500)

const rowOf = (name) => page.locator('[data-crew-id]').filter({ hasText: name }).first()
const openMenu = async (name) => {
  await page.getByRole('button', { name: `More actions for ${name}` }).click()
  await page.getByRole('menuitem', { name: 'Edit settings', exact: true }).waitFor({ timeout: 10000 })
  await page.waitForTimeout(300)
}
const listClip = async (padBottom = 20, pg = page) => {
  const rows = pg.locator('[data-crew-id]')
  const first = await rows.first().boundingBox()
  const last = await rows.last().boundingBox()
  const pad = 12
  return {
    x: Math.max(0, first.x - pad), y: Math.max(0, first.y - 48),
    width: Math.min(1200, first.width + 2 * pad), height: Math.min(860, last.y + last.height + padBottom) - Math.max(0, first.y - 48),
  }
}

// ---- Frame 1: badges, captions, and the row menu ---------------------------
const badgesText = await rowOf('build-farm').innerText()
if (!/EC2/.test(badgesText) || !/SSM/.test(badgesText)) fail(`build-farm row shows neither EC2 nor SSM badge: ${JSON.stringify(badgesText)}`)
const ec2SshText = await rowOf('gpu-box').innerText()
if (!/EC2/.test(ec2SshText) || !/SSH/.test(ec2SshText)) fail(`gpu-box row must carry both the EC2 and SSH badges: ${JSON.stringify(ec2SshText)}`)
const manualText = await rowOf('dev-box-1').innerText()
if (/EC2/.test(manualText)) fail('hand-added dev-box-1 must not carry an EC2 badge')
if (!/SSH/.test(manualText)) fail('hand-added dev-box-1 must carry the SSH badge')
// The stamped EC2 row's caption names what Remove does not do AND the remedy,
// and never hedges with the "cannot verify" copy.
if (!/Launched by the EC2 launcher\./.test(ec2SshText)) fail(`gpu-box row must show the stamped EC2 caption: ${JSON.stringify(ec2SshText)}`)
if (!/To stop the billing, delete it in the AWS console\./.test(ec2SshText)) fail(`the stamped caption must end with the AWS-console remedy: ${JSON.stringify(ec2SshText)}`)
if (/cannot verify/i.test(ec2SshText)) fail('a stamped EC2 row must never show the "cannot verify" caption')
if (/cannot verify/i.test(await page.locator('body').innerText())) fail('no row on this page may show the "cannot verify" caption')
await openMenu('gpu-box')
const items = (await page.getByRole('menuitem').allInnerTexts()).map((s) => s.trim())
console.log('MENU', JSON.stringify(items))
if (items.some((s) => /^Rename$/.test(s))) fail(`the row menu must not offer Rename: ${JSON.stringify(items)}`)
if (!items.includes('Edit settings')) fail(`the row menu must offer Edit settings: ${JSON.stringify(items)}`)
await page.screenshot({ path: `${OUT}/01-types-and-row-menu.png`, clip: await listClip(200) })
console.log('wrote 01')

// ---- Frame 2: Edit settings on the stamped EC2 row — Name is the rename path
await page.getByRole('menuitem', { name: 'Edit settings', exact: true }).click()
const editGpu = page.getByRole('group', { name: 'Edit gpu-box' })
await editGpu.waitFor({ timeout: 10000 })
await page.waitForTimeout(300)
const nameBox = editGpu.getByRole('textbox', { name: 'Name', exact: true })
await nameBox.waitFor({ timeout: 10000 })
if (await nameBox.evaluate((el) => el.readOnly || el.disabled)) fail('Name field must be editable in Edit settings')
await nameBox.click() // caret in Name: the frame shows renaming as this form's own field
if (!(await nameBox.evaluate((el) => document.activeElement === el))) fail('Name field did not take focus on click')
if ((await editGpu.locator('details').count()) !== 0) fail('the edit form must not carry a read-only disclosure')
await page.screenshot({ path: `${OUT}/02-rename-form-name-only.png`, clip: await listClip(20) })
console.log('wrote 02')
await editGpu.getByRole('button', { name: 'Cancel' }).click()
await editGpu.waitFor({ state: 'hidden', timeout: 5000 })

// ---- Frame 3: draft refusal at the clicked row ----------------------------
await openMenu('dev-box-1')
await page.getByRole('menuitem', { name: 'Edit settings' }).click()
const editForm = page.getByRole('group', { name: 'Edit dev-box-1' })
await editForm.waitFor({ timeout: 10000 })
const editHost = editForm.getByRole('textbox', { name: /SSH host/ })
await editHost.fill('dev-box-1-corrected')
await openMenu('dev-box-2')
await page.getByRole('menuitem', { name: 'Edit settings', exact: true }).click()
const draftRefusal = rowOf('dev-box-2').getByRole('alert')
await draftRefusal.waitFor({ timeout: 10000 })
const draftText = (await draftRefusal.innerText()).trim()
console.log('DRAFT REFUSAL', JSON.stringify(draftText))
if (!/Save or cancel the open edit before editing another crew/.test(draftText)) fail(`draft refusal wording: ${JSON.stringify(draftText)}`)
if (!(await editForm.isVisible())) fail('the dev-box-1 edit form must stay open across the refused edit')
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/03-draft-refusal-at-row.png`, clip: await listClip(20) })
console.log('wrote 03')

// ---- Frame 4: the live form freezes during its own save --------------------
// A Name draft typed before Save: frame 5 proves the abort keeps it.
const editName = editForm.getByRole('textbox', { name: 'Name', exact: true })
await editName.fill('dev-box-1-renamed')
holdPatch = true
await editForm.getByRole('button', { name: 'Save changes' }).click()
const savingButton = editForm.getByRole('button', { name: /Saving/ })
await savingButton.waitFor({ timeout: 10000 })
if (!(await savingButton.isDisabled())) fail('Save must be disabled while its request is pending')
if (!(await editHost.isDisabled())) fail('the live edit form fields must be disabled while saving')
if (!(await editName.isDisabled())) fail('the live edit form name must be disabled while saving')
// The exit button changes its label for the pending save and is the one control
// that must stay live: a hung PATCH has no other in-form way out.
const stopWaiting = editForm.getByRole('button', { name: 'Stop waiting' })
await stopWaiting.waitFor({ timeout: 10000 })
if (!(await stopWaiting.isEnabled())) fail('Stop waiting must stay enabled while its save is pending')
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/04-saving-form-frozen.png`, clip: await listClip(20) })
console.log('wrote 04')

// ---- Frame 5: Stop waiting returns the form to editable with its draft -----
// holdPatch stays true: the abort was client-side and the stub never answered,
// so releasing it here would fake a server reply the flow never received.
await stopWaiting.click()
await editForm.getByRole('button', { name: 'Save changes' }).waitFor({ timeout: 10000 })
if (await editHost.isDisabled()) fail('the SSH host field must be editable again after Stop waiting')
if (await editName.isDisabled()) fail('the Name field must be editable again after Stop waiting')
const keptName = await editName.inputValue()
if (keptName !== 'dev-box-1-renamed') fail(`the typed Name draft must survive Stop waiting: ${JSON.stringify(keptName)}`)
const keptHost = await editHost.inputValue()
if (keptHost !== 'dev-box-1-corrected') fail(`the typed host draft must survive Stop waiting: ${JSON.stringify(keptHost)}`)
const stoppedNote = editForm.getByRole('status').filter({ hasText: 'Stopped waiting' })
if (!(await stoppedNote.isVisible())) fail('a role="status" note containing "Stopped waiting" must be visible')
if (!(await editForm.getByRole('button', { name: 'Cancel', exact: true }).isVisible())) {
  fail('the exit button must read Cancel again after Stop waiting')
}
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/05-stopped-waiting-draft-kept.png`, clip: await listClip(20) })
console.log('wrote 05')

// ---- Frame 4b: the frozen form again, in the LIGHT theme -------------------
// holdPatch is still true, so this page's PATCH hangs the same way.
const lightPage = await context.newPage()
await stubDashboardApi(lightPage, { extra, theme: 'light' })
logPageProblems(lightPage)
await lightPage.goto(`${base}/settings?tab=instances`, { waitUntil: 'domcontentloaded' })
await lightPage.getByText('dev-box-1', { exact: true }).first().waitFor({ timeout: 20000 })
await lightPage.waitForTimeout(500)
await lightPage.getByRole('button', { name: 'More actions for dev-box-1' }).click()
await lightPage.getByRole('menuitem', { name: 'Edit settings', exact: true }).click()
const lightForm = lightPage.getByRole('group', { name: 'Edit dev-box-1' })
await lightForm.waitFor({ timeout: 10000 })
await lightForm.getByRole('textbox', { name: /SSH host/ }).fill('dev-box-1-corrected')
await lightForm.getByRole('textbox', { name: 'Name', exact: true }).fill('dev-box-1-renamed')
await lightForm.getByRole('button', { name: 'Save changes' }).click()
await lightForm.getByRole('button', { name: /Saving/ }).waitFor({ timeout: 10000 })
if (!(await lightForm.getByRole('button', { name: /Saving/ }).isDisabled())) fail('light pass: Save must be disabled while pending')
await lightPage.waitForTimeout(300)
await lightPage.screenshot({ path: `${OUT}/04b-saving-form-frozen-light.png`, clip: await listClip(20, lightPage) })
console.log('wrote 04b')

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
