/**
 * Screenshot harness for the Dynamic Dashboard DOCK above the composer.
 *
 * Photographs the three-tile dock in the REAL built SPA (website/dist) against a
 * stubbed inventory: a conductor with one worker, a two-item work board with one
 * blocked item, and one pending approval. Frames: tiles at rest, the Progress and
 * Needs you disclosures open, the dock hidden to its dot, the side panel the dock
 * opens, and a quiet scenario (no work board, nothing waiting) for the Progress
 * tile's running count, the empty Blocked list and the unlit dot. Four more
 * frames cover the rarer states: a board long enough for the "more in the Dashboard"
 * row, the stale notice when a source fails, the panel's info tip open, and the
 * panel's body (its Blocked rows, work items and running runs) photographed on a
 * viewport tall enough to hold it whole. Two more cover the Progress list's own
 * states: opened under a TODO plan, where its lead sentence separates the plan's
 * number from the runs it lists, and opened with nothing running at all.
 *
 * Also records the whole sequence as `demo.webm`, because the hide-to-dot morph is
 * motion a still cannot prove, and asserts the one geometry fact the layout rule
 * states: the dock spans the composer's column (its left and right edges match
 * the input box within a pixel). Nothing in CI runs this file; treat it as a
 * manual guard.
 *
 * Usage: node scripts/capture-command-center-dock.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/command-center-dock'
const ROOT = 'conductor'
const now = Math.floor(Date.now() / 1000)

mkdirSync(OUT, { recursive: true })

const slots = [
  { key: ROOT, title: 'Ship the sandbox fix', running: true, messages: 4, agent: 'kirocrew', memory_mode: 'persistent', modified: now, source_links: [], source_links_total: 0,
    todo: { tasks: [], total: 0, completed: 0 } },
  { key: 'worker-a', title: 'Windows chmod branch', running: true, created_by: ROOT, messages: 2, agent: 'kirocrew', memory_mode: 'persistent', modified: now, source_links: [], source_links_total: 0,
    pending_approval: true, pending_approval_info: { origin: 'native', request_id: 'r1', request_mid: 'm1', tool: 'shell', tool_input: 'git push -u origin feat/safe-chmod', tool_purpose: 'Publish the branch for the PR', tool_kind: 'execute' } },
  { key: 'worker-b', title: 'Review worker', running: false, created_by: ROOT, messages: 1, agent: 'kirocrew', memory_mode: 'persistent', modified: now, source_links: [], source_links_total: 0 },
]

const detail = { running: true, has_more: false, total: 1, queue: [], messages: [
  { role: 'user', ts: now - 600, content: 'Fix the Windows chmod path and open a PR.' },
  { role: 'assistant', ts: now - 30, content: 'Two workers are on it. I will merge the branches once CI is green.' },
] }

const work = { value: { items: [
  { item_id: 'w1', title: 'Add the Windows branch to _safe_chmod', state: 'accepted' },
  { item_id: 'w2', title: 'Pin the regression in test_sandbox', state: 'dispatched', status: 'blocked', summary: 'CI red: test_sandbox timeout' },
  { item_id: 'w3', title: 'Open the pull request', state: 'dispatched' },
] } }

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2, recordVideo: { dir: OUT, size: { width: 1400, height: 900 } } })
  const extra = async (path, route) => {
    if (path === '/api/approvals') { await json(route, []); return true }
    if (path === '/api/ask-question/pending') { await json(route, []); return true }
    if (path === '/api/workflows/runs') { await json(route, { runs: [] }); return true }
    if (path.endsWith('/crew-log/projection/work')) { await json(route, work); return true }
    if (path.startsWith('/api/artifacts')) { await json(route, { artifacts: [] }); return true }
    if (path.endsWith('/dashboard-card')) { await json(route, { card: null, status: 'disabled', published_at: null, content_event_at: null, stale: false }); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot); localStorage.removeItem('mc-task-dashboard-hidden') }, ROOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  const dock = page.getByTestId('command-center-dock')
  await dock.waitFor({ timeout: 15000 })
  await page.waitForTimeout(1200)

  const shot = async (name, top, height) => {
    await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: 0, y: top, width: 1400, height } })
    console.log('wrote', `${OUT}/${name}.png`)
  }
  // The layout rule is about the dock's visible pane -- its glass host, the same
  // surface the composer sits in -- which must share the composer column's
  // edges; the tiles sit inside that pane behind its own padding.
  const pane = await dock.locator('.liquid-glass').first().boundingBox()
  const tiles = await page.getByTestId('status-tiles').boundingBox()
  const box = await page.getByTestId('input-wrapper').first().boundingBox()
  console.log(JSON.stringify({ paneLeft: pane.x, paneRight: pane.x + pane.width, boxLeft: box.x, boxRight: box.x + box.width }))
  const aligned = Math.abs(pane.x - box.x) <= 1 && Math.abs(pane.x + pane.width - (box.x + box.width)) <= 1
  await shot('01-tiles-at-rest', tiles.y - 24, box.y + box.height - tiles.y + 60)

  // The dock is bottom-anchored, so an open disclosure moves the tiles UP: re-measure.
  const band = async name => {
    const t = await page.getByTestId('status-tiles').boundingBox()
    const b = await page.getByTestId('input-wrapper').first().boundingBox()
    await shot(name, t.y - 24, b.y + b.height - t.y + 60)
  }
  await page.getByRole('button', { name: /^Progress/ }).click()
  await page.waitForTimeout(600)
  await band('01b-progress-open')

  await page.getByRole('button', { name: /^Needs you/ }).click()
  await page.waitForTimeout(600)
  await band('02-needs-you-open')

  await page.getByRole('button', { name: /^Blocked/ }).click()
  await page.waitForTimeout(600)
  await band('03-blocked-open')

  await page.getByRole('button', { name: 'Hide status tiles' }).click()
  await page.waitForTimeout(700)
  const dot = await page.getByRole('button', { name: /^Dashboard: Needs you: / }).boundingBox()
  await shot('04-hidden-dot', dot.y - 40, (await page.getByTestId('input-wrapper').first().boundingBox()).y + 80 - (dot.y - 40))

  await page.getByRole('button', { name: /^Dashboard: Needs you: / }).click()
  await page.waitForTimeout(700)
  await page.getByTestId('status-tiles').getByRole('button', { name: 'Open Dashboard' }).click()
  await page.getByTestId('command-center-panel').waitFor({ timeout: 8000 })
  await page.waitForTimeout(900)
  await page.screenshot({ path: `${OUT}/05-panel.png` })
  console.log('wrote', `${OUT}/05-panel.png`)

  // The panel body (Blocked rows, work items, running runs) sits below the fold
  // at 900px: stretch the viewport so the whole panel fits, photograph just the
  // panel, then restore the size the remaining frames are clipped against.
  const viewport = page.viewportSize()
  await page.setViewportSize({ width: viewport.width, height: 1600 })
  await page.waitForTimeout(400)
  await page.getByTestId('command-center-panel').screenshot({ path: `${OUT}/12-panel-body.png` })
  console.log('wrote', `${OUT}/12-panel-body.png`)
  await page.setViewportSize(viewport)
  await page.waitForTimeout(400)

  // The panel's explanatory copy lives behind its one info control.
  await page.getByTestId('command-center-panel').getByRole('button', { name: 'More information' }).click()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/11-panel-info-open.png` })
  console.log('wrote', `${OUT}/11-panel-info-open.png`)

  // Second scenario: nothing waits on the user and there is no work board, so
  // the Progress tile reads the running count, the Blocked list is empty and the
  // hidden dot stays quiet.
  const quiet = await context.newPage()
  logPageProblems(quiet)
  const quietSlots = slots.map(s => ({ ...s, pending_approval: undefined, pending_approval_info: undefined }))
  await stubDashboardApi(quiet, { slots: quietSlots, theme: 'dark', extra: async (path, route) => {
    if (path.endsWith('/crew-log/projection/work')) { await json(route, { value: { items: [] } }); return true }
    return extra(path, route)
  } })
  await quiet.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot); localStorage.removeItem('mc-task-dashboard-hidden') }, ROOT)
  await quiet.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await quiet.getByTestId('status-tiles').waitFor({ timeout: 15000 })
  await quiet.waitForTimeout(1200)
  const qt = await quiet.getByTestId('status-tiles').boundingBox()
  const qb = await quiet.getByTestId('input-wrapper').first().boundingBox()
  await quiet.screenshot({ path: `${OUT}/06-running-tile-no-plan.png`, clip: { x: 0, y: qt.y - 24, width: 1400, height: qb.y + qb.height - qt.y + 60 } })
  console.log('wrote', `${OUT}/06-running-tile-no-plan.png`)
  // The disclosed list opens by saying it counts active runs, since no plan exists.
  await quiet.getByRole('button', { name: /^Progress/ }).click()
  await quiet.waitForTimeout(600)
  const qo = await quiet.getByTestId('status-tiles').boundingBox()
  await quiet.screenshot({ path: `${OUT}/06b-running-list-no-plan.png`, clip: { x: 0, y: qo.y - 24, width: 1400, height: qb.y + qb.height - qo.y + 60 } })
  console.log('wrote', `${OUT}/06b-running-list-no-plan.png`)
  await quiet.getByRole('button', { name: /^Progress/ }).click()
  await quiet.waitForTimeout(400)
  await quiet.getByRole('button', { name: /^Blocked/ }).click()
  await quiet.waitForTimeout(600)
  await quiet.getByRole('region', { name: 'Blocked' }).getByText('Nothing is blocked.').waitFor({ timeout: 5000 })
  const qbt = await quiet.getByTestId('status-tiles').boundingBox()
  const qbb = await quiet.getByTestId('input-wrapper').first().boundingBox()
  await quiet.screenshot({ path: `${OUT}/08-blocked-empty-open.png`, clip: { x: 0, y: qbt.y - 24, width: 1400, height: qbb.y + qbb.height - qbt.y + 60 } })
  console.log('wrote', `${OUT}/08-blocked-empty-open.png`)
  // A second click on the open tile closes its list.
  await quiet.getByRole('button', { name: /^Blocked/ }).click()
  await quiet.waitForTimeout(600)
  await quiet.getByRole('button', { name: 'Hide status tiles' }).click()
  await quiet.waitForTimeout(700)
  const qd = await quiet.getByRole('button', { name: 'Show status tiles' }).boundingBox()
  const qb2 = await quiet.getByTestId('input-wrapper').first().boundingBox()
  await quiet.screenshot({ path: `${OUT}/07-quiet-dot.png`, clip: { x: 0, y: qd.y - 40, width: 1400, height: qb2.y + 80 - (qd.y - 40) } })
  console.log('wrote', `${OUT}/07-quiet-dot.png`)
  const qv = quiet.video()
  await quiet.close()
  await qv.delete()

  // One frame per rarer state, each on its own page so the stub differs in one
  // fact only. `scenario` opens the page and returns the dock band's clip.
  const scenario = async (name, { slots: pageSlots = slots, work: pageWork = work, fail = null } = {}) => {
    const other = await context.newPage()
    logPageProblems(other)
    await stubDashboardApi(other, { slots: pageSlots, theme: 'dark', extra: async (path, route) => {
      if (fail && path === fail) { await json(route, { error: 'unavailable' }, 500); return true }
      if (path.endsWith('/crew-log/projection/work')) { await json(route, pageWork); return true }
      return extra(path, route)
    } })
    await other.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot); localStorage.removeItem('mc-task-dashboard-hidden') }, ROOT)
    await other.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await other.getByTestId('status-tiles').waitFor({ timeout: 15000 })
    await other.waitForTimeout(1200)
    const bandShot = async () => {
      const t = await other.getByTestId('status-tiles').boundingBox()
      const b = await other.getByTestId('input-wrapper').first().boundingBox()
      await other.screenshot({ path: `${OUT}/${name}.png`, clip: { x: 0, y: t.y - 24, width: 1400, height: b.y + b.height - t.y + 60 } })
      console.log('wrote', `${OUT}/${name}.png`)
    }
    return { other, bandShot }
  }

  // A board longer than the list shows: the Progress disclosure ends in the
  // "more in the Dashboard" row.
  const longBoard = { value: { items: Array.from({ length: 8 }, (_, i) => ({ item_id: `long-${i + 1}`, title: `Work item ${i + 1}`, state: 'dispatched' })) } }
  const overflow = await scenario('09-overflow-row', { work: longBoard })
  await overflow.other.getByRole('button', { name: /^Progress/ }).click()
  await overflow.other.getByRole('button', { name: /more in the Dashboard$/ }).waitFor({ timeout: 5000 })
  await overflow.other.waitForTimeout(600)
  await overflow.bandShot()
  const ov = overflow.other.video()
  await overflow.other.close()
  await ov.delete()

  // A failing source: the approvals read answers 500, so after its one retry the
  // dock shows the shared stale notice under the tiles.
  const stale = await scenario('10-stale-notice', { fail: '/api/approvals' })
  await stale.other.getByTestId('command-center-dock').getByRole('alert').waitFor({ timeout: 15000 })
  await stale.other.waitForTimeout(400)
  await stale.bandShot()
  const sv = stale.other.video()
  await stale.other.close()
  await sv.delete()

  // A TODO plan and no work board: the tile's number is the plan's (1/3) while
  // the rows are still the runs, so the disclosed list opens with the sentence
  // that keeps the two readouts apart. One worker runs so the list has a row.
  const plan = { description: 'Ship the sandbox fix',
    tasks: [{ id: '1', text: 'Reproduce on Windows', completed: true }, { id: '2', text: 'Add the Windows branch to _safe_chmod', completed: false }, { id: '3', text: 'Open the pull request', completed: false }],
    total: 3, completed: 1, current: 'Add the Windows branch to _safe_chmod' }
  const plannedSlots = slots.map(s => ({ ...s, pending_approval: undefined, pending_approval_info: undefined, ...(s.key === ROOT ? { todo: plan } : {}) }))
  const planned = await scenario('13-plan-progress-open', { slots: plannedSlots, work: { value: { items: [] } } })
  await planned.other.getByRole('button', { name: /^Progress 1\/3/ }).click()
  await planned.other.getByRole('region', { name: 'Progress' }).getByText('Progress counts the plan; this list shows what’s running now.').waitFor({ timeout: 5000 })
  await planned.other.waitForTimeout(600)
  await planned.bandShot()
  const pv = planned.other.video()
  await planned.other.close()
  await pv.delete()

  // The Progress list with nothing to list: the board holds one accepted item
  // and one blocked item, so the number is work done (1/2) and the blocked item
  // belongs to the Blocked tile, while every session is idle -- the conductor
  // waits on the item its worker reported stuck. Nothing is settled (the blocked
  // item keeps the dock up), yet nothing runs, so the list says so.
  const idleSlots = slots.map(s => ({ ...s, running: false, pending_approval: undefined, pending_approval_info: undefined }))
  const stuckBoard = { value: { items: [
    { item_id: 'w1', title: 'Add the Windows branch to _safe_chmod', state: 'accepted' },
    { item_id: 'w2', title: 'Pin the regression in test_sandbox', state: 'dispatched', status: 'blocked', summary: 'CI red: test_sandbox timeout' },
  ] } }
  const empty = await scenario('14-progress-empty', { slots: idleSlots, work: stuckBoard })
  await empty.other.getByRole('button', { name: /^Progress 1\/2/ }).click()
  await empty.other.getByRole('region', { name: 'Progress' }).getByText('Nothing is running.').waitFor({ timeout: 5000 })
  await empty.other.waitForTimeout(600)
  await empty.bandShot()
  const ev = empty.other.video()
  await empty.other.close()
  await ev.delete()

  const video = page.video()
  await page.close()
  await video.saveAs(`${OUT}/demo.webm`)
  await video.delete()
  console.log('wrote', `${OUT}/demo.webm`)
  await browser.close()
  srv.close()
  console.log(JSON.stringify({ aligned }))
  if (!aligned) { console.error('FAIL: the dock does not span the composer column'); process.exit(1) }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
