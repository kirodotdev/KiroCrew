/**
 * Screenshot harness for #19859: a folder's "Send a copy to".
 *
 * Frames (each in dark and light):
 *  1. folder-send-outcomes  After three sends: crew-docs partial (4 of 5, a
 *                           refusal through ErrorNotice, "resend the 1 that
 *                           failed"), crew-build done with 2 transcript-only
 *                           copies and what that means on screen, crew-ops done
 *                           with the visible "send all again" line, crew-old
 *                           not connected, and the skipped private session.
 *  2. folder-send-sending   A send in progress: "1 of 3" at full contrast on a
 *                           row that is not dimmed (the press is ignored).
 *  3. folder-send-empty     Nothing sendable: every crew row disabled, "No
 *                           sessions to send", and the private sessions counted.
 *  4. folder-send-listerror The crew list failing to load, through ErrorNotice.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call stubbed, so no
 * gateway or crew is needed. It does NOT show a copy arriving on a crew.
 *
 * Usage: npm run build && node scripts/capture-19859-send-folder.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/19859-send-folder'
mkdirSync(OUT, { recursive: true })

const crew = (id, state, port) => ({
  id, name: id, ssh_host: `${id}-alias`, remote_port: 7777, local_port: port, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: state === 'connected',
  status: { instance_id: id, state, local_port: port, remote_port: 7777 },
})
const CREWS = [
  crew('crew-docs', 'connected', 7801),
  crew('crew-build', 'connected', 7802),
  crew('crew-ops', 'connected', 7803),
  crew('crew-old', 'disconnected', 0),
]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const FOLDER = 'f-rel'
const FOLDERS = [{ id: FOLDER, name: 'Release notes', order: 0, collapsed: false }]
const slot = (key, title, memory_mode = 'persistent') => ({
  key, title, messages: 4, running: false, agent: 'kirocrew', mode: '', memory_mode,
  folder_id: FOLDER, last_message: '', source_links: [], source_links_total: 0,
  created: '2026-10-19T08:00:00Z', last_ts: '2026-10-19T09:00:00Z', modified: Math.floor(Date.now() / 1000),
})

let stub = {}
const extra = async (path, route) => {
  if (path === '/api/instances') {
    if (stub.listFails) {
      await route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ error: 'gateway unreachable' }) })
      return true
    }
    await json(route, { active: true, instances: stub.crews ?? CREWS, warm_set_cap: 5, sso: SSO })
    return true
  }
  if (path === '/api/cloud/launch') { await json(route, { jobs: [] }); return true }
  const m = path.match(/^\/api\/instances\/([^/]+)\/send-session$/)
  if (m) {
    const id = decodeURIComponent(m[1])
    const n = (stub.calls[id] = (stub.calls[id] ?? 0) + 1)
    if (stub.hold?.(id, n)) return true // left pending on purpose: the "sending" frame
    if (id === 'crew-docs' && n === 5) {
      await route.fulfill({ status: 429, contentType: 'application/json', body: JSON.stringify({ error: 'the crew refused the export (HTTP 429)' }) })
      return true
    }
    const prefix = id === 'crew-build' && n <= 2
    await json(route, { ok: true, instance: id, key: `remote-${id}-${n}`, ...(prefix ? { resume_mode: 'prefix' } : {}) })
    return true
  }
  return false
}

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {})

const openPage = async (theme, slots, state = {}) => {
  stub = { calls: {}, ...state }
  const context = await browser.newContext({ viewport: { width: 1300, height: 1100 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { folders: FOLDERS, slots, extra, theme })
  logPageProblems(page)
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(`[data-testid="folder-collapse-${FOLDER}"]`, { timeout: 20000 })
  await page.waitForTimeout(800)
  await page.hover(`[data-testid="folder-collapse-${FOLDER}"]`)
  await page.click(`[data-testid="folder-menu-${FOLDER}"]`)
  const entry = page.getByRole('menuitem', { name: 'Send a copy to' })
  await entry.click()
  // The submenu is open once a second menu is on screen.
  await page.waitForFunction(() => document.querySelectorAll('[role="menu"]').length >= 2, null, { timeout: 10000 })
  await page.waitForTimeout(400)
  return page
}

const row = (page, name) => page.getByRole('menuitem').filter({ hasText: name }).first()
// Clip to the folder row plus both open menus, so the frame shows the menu, not the app.
const shot = async (page, file) => {
  const boxes = []
  for (const m of await page.getByRole('menu').all()) { const b = await m.boundingBox(); if (b) boxes.push(b) }
  const head = await page.getByTestId(`folder-collapse-${FOLDER}`).boundingBox()
  if (head) boxes.push(head)
  const x = Math.max(0, Math.min(...boxes.map(b => b.x)) - 16), y = Math.max(0, Math.min(...boxes.map(b => b.y)) - 16)
  await page.screenshot({
    path: `${OUT}/${file}`,
    clip: { x, y, width: Math.max(...boxes.map(b => b.x + b.width)) + 16 - x, height: Math.max(...boxes.map(b => b.y + b.height)) + 16 - y },
  })
  console.log(`wrote ${file}`)
}

const FIVE = ['a', 'b', 'c', 'd', 'e'].map(k => slot(`s-${k}`, `Notes ${k}`))
const PRIVATE = slot('s-p', 'Scratch', 'incognito')

const outcomes = async (theme) => {
  const page = await openPage(theme, [...FIVE, PRIVATE])
  for (const name of ['crew-docs', 'crew-build', 'crew-ops']) {
    await row(page, name).click()
    await page.waitForTimeout(150)
  }
  await page.getByText('4 of 5 sent').waitFor({ timeout: 10000 })
  await page.getByText('Sent: 5', { exact: true }).waitFor({ timeout: 10000 })
  await page.waitForTimeout(300)
  const docs = await row(page, 'crew-docs').innerText()
  if (!/HTTP 429/.test(docs) || !/resend the 1 that failed/.test(docs)) fail(`${theme}: partial row must show the refusal and the retry line: ${JSON.stringify(docs)}`)
  if (/send another copy of all/.test(docs)) fail(`${theme}: a partial row must not offer to resend everything`)
  const build = await row(page, 'crew-build').innerText()
  if (!/2 transcript only/.test(build) || !/so 2 copies resume from the transcript alone/.test(build)) fail(`${theme}: transcript-only row must say what it means on screen, counted: ${JSON.stringify(build)}`)
  if (!(await page.getByText('Copies are filed on the crew in a folder of the same name.').count())) fail(`${theme}: the submenu must say where the copies land`)
  const ops = await row(page, 'crew-ops').innerText()
  if (!/Click to send another copy of all 5/.test(ops)) fail(`${theme}: a finished row must say on screen that a click sends all again: ${JSON.stringify(ops)}`)
  if (!(await page.getByText(/1 incognito or temporary session is not sent/).count())) fail(`${theme}: the skipped private session must be counted on screen`)
  const sent = Object.fromEntries(Object.entries(stub.calls).map(([k, v]) => [k, v]))
  if (sent['crew-docs'] !== 5 || sent['crew-build'] !== 5 || sent['crew-ops'] !== 5 || sent['crew-old']) fail(`${theme}: one call per persistent session per crew, none for the offline crew or the private one: ${JSON.stringify(sent)}`)
  const clipped = await page.evaluate(() => [...document.querySelectorAll('[role="menu"]')].some(m => m.scrollHeight > m.clientHeight + 1))
  if (clipped) fail(`${theme}: the submenu must show every row without scrolling at this size`)
  await shot(page, `folder-send-outcomes-${theme}.png`)
  await page.context().close()
}

const sending = async (theme) => {
  const page = await openPage(theme, FIVE.slice(0, 3), { crews: [CREWS[0], CREWS[2]], hold: (id, n) => id === 'crew-docs' && n === 2 })
  await row(page, 'crew-docs').click()
  await page.getByText('1 of 3').waitFor({ timeout: 10000 })
  await page.waitForTimeout(300)
  const r = row(page, 'crew-docs')
  if ((await r.getAttribute('data-disabled')) !== null) fail(`${theme}: a sending row must not be Radix-disabled (that dims "1 of 3")`)
  const opacity = await r.evaluate(el => getComputedStyle(el).opacity)
  if (opacity !== '1') fail(`${theme}: the sending row must stay at full opacity, got ${opacity}`)
  await r.click()
  await page.waitForTimeout(300)
  if (stub.calls['crew-docs'] !== 2) fail(`${theme}: a press while sending must not start another send (${stub.calls['crew-docs']} calls)`)
  await shot(page, `folder-send-sending-${theme}.png`)
  await page.context().close()
}

const empty = async (theme) => {
  const page = await openPage(theme, [PRIVATE, slot('s-t', 'Try-out', 'temporary')])
  await page.getByText('No sessions to send').waitFor({ timeout: 10000 })
  for (const c of CREWS) {
    if ((await row(page, c.id).getAttribute('data-disabled')) === null) fail(`${theme}: ${c.id} must be disabled with nothing to send`)
  }
  if (!(await page.getByText(/2 incognito or temporary sessions are not sent/).count())) fail(`${theme}: both private sessions must be counted`)
  if (await page.getByText(/in a folder of the same name/).count()) fail(`${theme}: with nothing to send, no line says where copies would land`)
  await shot(page, `folder-send-empty-${theme}.png`)
  await page.context().close()
}

const listError = async (theme) => {
  const page = await openPage(theme, FIVE, { listFails: true })
  const alert = page.getByRole('alert').filter({ hasText: 'gateway unreachable' })
  await alert.waitFor({ timeout: 10000 })
  if (!(await page.getByRole('menuitem', { name: /ask the agent/i }).count())) fail(`${theme}: the list failure must offer the agent hand-off`)
  await shot(page, `folder-send-listerror-${theme}.png`)
  await page.context().close()
}

for (const theme of ['dark', 'light']) {
  await outcomes(theme)
  await sending(theme)
  await empty(theme)
  await listError(theme)
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
