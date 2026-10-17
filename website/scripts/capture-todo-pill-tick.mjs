/**
 * Screenshot harness for the TASK CHECKLIST PILL's clickable rows.
 *
 * Renders a slot whose `todo` snapshot shows 3 of 7 done (the shape of the
 * bug report: an agent that lost its native todo_list state after a restart
 * and could not tick rows 4–7), expands the pill, clicks row 4, and lets the
 * stubbed `PATCH /api/chat/slots/{slot}/todo` echo the same `todo_update`
 * frame the gateway broadcasts. Seven shots per theme: collapsed, expanded,
 * after the tick (person mark on the ticked row), the failure state (the stub
 * refuses the next PATCH), a tick held in flight, a remote-bound read-only
 * list, and a row the person unticked.
 *
 * Usage: node scripts/capture-todo-pill-tick.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/todo-pill-tick'
const SLOT = 'chat-checklist'
const PROJECT = '/home/user/workspace/atx-db'

mkdirSync(OUT, { recursive: true })

const TASKS = [
  ['Locate and run the SC setup skill to completion', true],
  ['Load working preferences context', true],
  ['Fetch all open SEV2/2.5 tickets and split SC vs CT', true],
  ['Read SC runbook and CT runbook + CT investigate-job-runs skill', false],
  ['RCA each SC ticket with evidence saved to durable evidence folder', false],
  ['RCA each CT ticket with evidence saved to durable evidence folder', false],
  ['Categorize SC and CT tickets into consolidated category SEV2s and present', false],
]

// A third tuple slot marks a row the PERSON set (the gateway's `person` flag,
// carried until the agent's own list confirms it).
const todoPayload = tasks => {
  const list = tasks.map(([text, completed, person], i) => ({ id: String(i + 1), text, completed, ...(person ? { person: true } : {}) }))
  return {
    description: 'Run SC setup skill, load working preferences, then RCA every open SEV2/2.5',
    tasks: list,
    completed: list.filter(t => t.completed).length,
    total: list.length,
    current: list.find(t => !t.completed)?.text ?? '',
  }
}

let todo = todoPayload(TASKS)
let remote = false

const slots = () => [{
  key: SLOT,
  title: 'RCA the open SEV2s',
  running: false,
  last_message: 'The work behind all four is done.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
  executor: remote ? 'remote' : 'local',
  instance_id: remote ? 'peer-1' : '',
  todo,
}]

const detail = () => ({
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Please tick the remaining checklist items, the work is done.' },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 30,
      content: 'I can\'t tick those four items off in the dashboard checklist from this session, but the work behind all four is done.',
    },
  ],
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 1200 }, deviceScaleFactor: 2 })

  let wsServer = null
  let refuseNext = false
  let holdNext = false
  let releaseHeld = null
  const extra = async (path, route) => {
    // The remote scenario: the peer's capability read must answer a real
    // shape, or the chat page's model memo dereferences a missing `models`.
    if (/^\/api\/instances\/[^/]+\/capabilities$/.test(path)) {
      await json(route, { models: [], effort_levels: [], default_model: '' })
      return true
    }
    if (path.startsWith('/api/instances')) {
      await json(route, { instances: remote ? [{ id: 'peer-1', name: 'peer-1', status: 'online' }] : [], active: '' })
      return true
    }
    const m = /^\/api\/chat\/slots\/([^/]+)\/todo$/.exec(path)
    if (m && route.request().method() === 'PATCH') {
      if (refuseNext) {
        refuseNext = false
        await json(route, { error: 'the calling session is gone, so this write cannot be attributed', code: 'caller_unattributable' }, 403)
        return true
      }
      if (holdNext) {
        holdNext = false
        await new Promise(r => { releaseHeld = r })
        await json(route, { ok: true, todo })
        return true
      }
      const body = route.request().postDataJSON()
      // Apply the click to the CURRENT list, the way the gateway's override
      // layer does, so an earlier tick survives a later untick.
      const tasks = todo.tasks.map(t => [t.text, t.completed, !!t.person])
      const idx = Number(body.id) - 1
      if (tasks[idx]) { tasks[idx][1] = body.completed; tasks[idx][2] = true }
      todo = todoPayload(tasks)
      await json(route, { ok: true, todo })
      // The gateway broadcasts the same delta the agent's tool result does.
      if (wsServer) wsServer.send(JSON.stringify({ type: 'todo_update', data: { slot: SLOT, todo } }))
      return true
    }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail()); return true }
    return false
  }

  let page = null
  async function load(theme) {
    if (page) await page.close()
    wsServer = null
    todo = todoPayload(TASKS)
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { folders: [], slots: slots(), theme, extra })
    await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
    await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  async function shoot(name) {
    const pill = page.getByTestId('todo-pill')
    await pill.waitFor({ state: 'visible', timeout: 10000 })
    // Clip to the pill's OUTER panel, which grows with its content (the
    // refusal notice sits under the list, outside its max-height), rather
    // than the inner list's box. The panel lives in the composer status
    // stack, capped at 50svh with its own scrollbar: the tall viewport keeps
    // the whole panel inside that cap, and the scroll keeps a notice in view
    // when it is not.
    const notice = page.getByTestId('todo-tick-error')
    if (await notice.count()) await notice.scrollIntoViewIfNeeded()
    await page.waitForTimeout(150)
    const panel = pill.locator('xpath=ancestor::div[contains(@class,"animate-slide-up")][1]')
    const box = await panel.boundingBox()
    const top = Math.max(0, box.y - 24)
    const bottom = box.y + box.height + 24
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x: 0, y: top, width: 1400, height: Math.min(1200 - top, bottom - top + 60) },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  for (const theme of ['dark', 'light']) {
    await load(theme)
    await shoot(`${theme}-1-collapsed-3-of-7`)
    await page.getByTestId('todo-pill').click()
    await page.waitForTimeout(300)
    await shoot(`${theme}-2-expanded`)
    const rows = page.getByTestId('todo-row-toggle')
    const row4 = rows.nth(3)
    await row4.hover()
    await row4.click()
    await page.waitForTimeout(600)
    const count = await page.getByTestId('todo-count').textContent()
    const checked = await row4.getAttribute('aria-checked')
    console.log(theme, 'after tick:', count, 'row4 aria-checked=', checked)
    if (checked !== 'true' || !/^4 /.test(count || '')) throw new Error(`tick did not repaint: ${count} / ${checked}`)
    await shoot(`${theme}-3-after-tick-4-of-7`)
    // The failure state: the gateway refuses the next tick; the row keeps its
    // old state and the notice names it with the server's words as detail.
    refuseNext = true
    await rows.nth(4).click()
    await page.getByTestId('todo-tick-error').waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(200)
    await shoot(`${theme}-4-tick-refused`)
    // The pending state: hold the next PATCH open and photograph the greyed row.
    holdNext = true
    await page.getByTestId('todo-tick-error').getByRole('button', { name: /dismiss/i }).click().catch(() => {})
    await rows.nth(5).click()
    await page.waitForTimeout(250)
    await shoot(`${theme}-5-tick-pending`)
    if (releaseHeld) releaseHeld()
    await page.waitForTimeout(300)
    // The undo half: untick a row the agent had completed (row 3). The stub
    // echoes it open with the person mark, the way the gateway's override does.
    await page.getByTestId('todo-row-toggle').nth(2).click()
    await page.waitForTimeout(600)
    await shoot(`${theme}-7-person-unticked`)
    // A remote-bound session: the same list, drawn read-only.
    remote = true
    await load(theme)
    await page.getByTestId('todo-pill').click()
    await page.waitForTimeout(300)
    await shoot(`${theme}-6-remote-read-only`)
    remote = false
  }

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
