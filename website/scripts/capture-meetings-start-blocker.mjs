/**
 * Screenshot harness for the refused-Start notice on MeetingView.
 *
 * A Start refused because another meeting holds the slot names that meeting. When
 * the 409 also carries `blocking_meeting_id`, the notice offers one action that
 * opens it: "Open its review" for a meeting in review, "Open that meeting"
 * otherwise. The "before" shots stub a 409 without the id, which is the body
 * current main sends, so the notice has no action. The last shot edits the
 * minutes first: the action then asks before discarding that unsaved draft.
 *
 * Runs the real production SPA with deterministic API fixtures, following the
 * repository's other capture harnesses (capture-meetings-translation.mjs).
 *
 * Usage: node scripts/capture-meetings-start-blocker.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/meetings-start-blocker'
mkdirSync(OUT, { recursive: true })

const NOTES = { id: 'notes', name: 'Note Taker', widget_type: 'markdown' }

const IDLE = {
  event_id: 'planning-session',
  title: 'Planning session',
  status: 'idle',
  attachments: [],
  outputs: { notes: '# Agenda\n\n- Q4 roadmap' },
  muted_agents: [],
  agents_enabled: ['notes'],
  started_at: '',
  ended_at: '',
}

const BLOCKER = {
  event_id: 'launch-retro',
  title: 'Launch retrospective',
  status: 'reviewing',
  attachments: [],
  outputs: {},
  muted_agents: [],
  agents_enabled: [],
  started_at: '2026-08-28T09:00:00Z',
  ended_at: '',
}

const LIVE = {
  active_meeting: BLOCKER.event_id,
  muted_agents: [],
  agents: {},
  agents_paused: false,
  expired: false,
  accepting_dispatches: true,
}

const META = { [IDLE.event_id]: IDLE, [BLOCKER.event_id]: BLOCKER }

/** Whether the 409 carries `blocking_meeting_id` (this branch) or not (main). */
let sendId = true

function meetingsApi(path, route) {
  const method = route.request().method()
  if (path === '/api/apps/meetings/config') {
    return json(route, {
      config: {
        meeting_agents: [NOTES],
        stt_provider: 'kiro',
        task_provider: 'ledger',
        calendar: { provider: 'none', source: '' },
        presets: {},
        default_preset: '',
        poll_interval_active: 3600,
        poll_interval_idle: 3600,
      },
      task_providers: [{ id: 'ledger', label: 'Local ledger' }],
      calendar_providers: [{ id: 'none', label: 'None' }],
      stt_providers: [{ id: 'kiro', label: 'Kiro Crew' }],
    }), true
  }
  if (path === '/api/apps/meetings/calendar') {
    return json(route, { events: [], provider: 'none', configured: false }), true
  }
  if (path === '/api/apps/meetings/meetings' && method === 'GET') {
    return json(route, {
      meetings: [IDLE, BLOCKER].map(m => ({
        event_id: m.event_id,
        title: m.title,
        status: m.status,
        started_at: m.started_at,
        ended_at: '',
      })),
    }), true
  }
  if (path === '/api/apps/meetings/agents') {
    return json(route, { agents: [NOTES], task_extractor_id: '' }), true
  }
  if (path === '/api/apps/meetings/status') {
    return json(route, LIVE), true
  }
  const match = path.match(/^\/api\/apps\/meetings\/meetings\/([^/]+)(\/.*)?$/)
  if (!match) {
    if (path.startsWith('/api/apps/meetings/')) console.log('UNMATCHED:', method, path)
    return false
  }
  const meta = META[decodeURIComponent(match[1])]
  const tail = match[2] ?? ''
  if (!meta) return false
  if (tail === '/init' && method === 'POST') {
    return json(route, { meeting_id: meta.event_id, meta }), true
  }
  if (tail === '/start' && method === 'POST') {
    const body = {
      error: 'another meeting is already active',
      code: 'meeting_already_active',
      blocking_meeting_title: BLOCKER.title,
      blocking_meeting_status: BLOCKER.status,
    }
    if (sendId) body.blocking_meeting_id = BLOCKER.event_id
    return json(route, body, 409), true
  }
  if (tail.startsWith('/transcript')) {
    return json(route, { segments: [], next_cursor: 0 }), true
  }
  if (tail === '/outputs') return json(route, { outputs: meta.outputs, tasks: [] }), true
  if (tail === '/tasks' && method === 'GET') return json(route, { tasks: [] }), true
  if (tail === '' && method === 'GET') {
    return json(route, { meta, live: meta === BLOCKER ? LIVE : null }), true
  }
  console.log('UNMATCHED:', method, path)
  return false
}

async function refusedStart(browser, base, theme) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 820 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme, extra: meetingsApi })
  logPageProblems(page)
  await page.goto(base + '/meetings', { waitUntil: 'domcontentloaded' })
  await page.getByText('Planning session').click()
  await page.getByRole('button', { name: 'Start', exact: true }).click()
  await page.getByText('“Launch retrospective” is still in review').waitFor()
  return { context, page }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shots = []
  const shot = async (page, name) => {
    const path = `${OUT}/${name}.png`
    await page.screenshot({ path })
    shots.push(path)
  }
  try {
    sendId = false
    const before = await refusedStart(browser, base, 'dark')
    if (await before.page.getByRole('button', { name: 'Open its review' }).count()) {
      throw new Error('a 409 without an id must not offer the action')
    }
    await shot(before.page, '01-before-no-action-dark')
    await before.context.close()

    sendId = true
    for (const theme of ['dark', 'light']) {
      const after = await refusedStart(browser, base, theme)
      const action = after.page.getByRole('button', { name: 'Open its review' })
      await action.waitFor()
      await shot(after.page, `02-after-action-${theme}`)
      if (theme === 'dark') {
        await action.click()
        // The blocker is in review, so MeetingView opens on its review screen.
        await after.page.getByRole('button', { name: 'Close the meeting' }).waitFor()
        await after.page.waitForTimeout(400)
        await shot(after.page, '03-after-opened-review-dark')
      }
      await after.context.close()
    }

    // An unsaved minutes draft: the action asks first, and Cancel stays here.
    const dirty = await refusedStart(browser, base, 'dark')
    await dirty.page.getByLabel('Edit this output').click()
    await dirty.page.getByLabel('Note Taker output, editable').fill('# Agenda\n\n- Q4 roadmap\n- Hiring')
    await dirty.page.getByRole('button', { name: 'Open its review' }).click()
    const dialog = dirty.page.getByRole('dialog')
    await dialog.getByText('Your unsaved changes will be permanently deleted.').waitFor()
    await dirty.page.waitForTimeout(400)
    await shot(dirty.page, '04-after-dirty-minutes-confirm-dark')
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await dirty.page.getByLabel('Note Taker output, editable').waitFor()
    if (await dirty.page.getByRole('button', { name: 'Close the meeting' }).count()) {
      throw new Error('Cancel must keep the user on the meeting with the draft')
    }
    await dirty.context.close()
    for (const path of shots) console.log('wrote', path)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
