/**
 * Screenshot harness for the live-translation UI on MeetingView, capturing the
 * two review-driven fixes on this branch:
 *
 *   1. The toolbar's overflow menu: the row holds one primary status action plus
 *      a single trigger (max-two-buttons-per-row); End and review, Refresh,
 *      Translation and Action items live inside the menu with full text labels.
 *   2. The translation sidebar's responsive shape: side-by-side at 340px from
 *      `lg` up, stacked with a bounded height when narrow, so a 320px viewport
 *      no longer clips it.
 *
 * Runs the real production SPA with deterministic API fixtures, following the
 * repository's other capture harnesses (capture-meetings-delete.mjs).
 *
 * Usage: node scripts/capture-meetings-translation.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/meetings-translation'
mkdirSync(OUT, { recursive: true })

const MEETING = {
  event_id: 'weekly-product-sync',
  title: 'Weekly product sync',
  status: 'active',
  attachments: [],
  outputs: {},
  muted_agents: [],
  agents_enabled: [],
  started_at: '2026-08-28T15:00:00Z',
  ended_at: '',
}

const LIVE = {
  active_meeting: 'weekly-product-sync',
  muted_agents: [],
  agents: {},
  agents_paused: false,
  expired: false,
  accepting_dispatches: true,
}

const SEGMENTS = [
  ['we ship the meetings translation panel on friday', '15:00:04'],
  ['the sidebar shows the source line and the translation together', '15:00:11'],
  ['a failed line is marked instead of silently dropped', '15:00:19'],
  ['the panel follows the tail as new lines arrive', '15:00:26'],
].map(([text, at], index) => ({
  id: `seg-${index}`,
  timestamp: `2026-08-28T${at}Z`,
  source: 'speech',
  text,
}))

const TRANSLATIONS = [
  ['we ship the meetings translation panel on friday', '金曜日に会議翻訳パネルをリリースします'],
  ['the sidebar shows the source line and the translation together', 'サイドバーには原文と翻訳が並んで表示されます'],
  ['a failed line is marked instead of silently dropped', '失敗した行は黙って消えるのではなく、印が付きます'],
  ['the panel follows the tail as new lines arrive', '新しい行が届くとパネルは末尾を追いかけます'],
].map(([source, text], n) => ({ n, source, text, at: '2026-08-28T15:00:30Z' }))

async function meetingsApi(path, route) {
  const method = route.request().method()
  if (path === '/api/apps/meetings/config') {
    return json(route, {
      config: {
        meeting_agents: [],
        stt_provider: 'kiro',
        task_provider: 'ledger',
        calendar: { provider: 'none', source: '' },
        presets: {},
        default_preset: '',
        poll_interval_active: 3600,
        poll_interval_idle: 3600,
        translation_language: 'ja',
      },
      task_providers: [{ id: 'ledger', label: 'Local ledger' }],
      calendar_providers: [{ id: 'none', label: 'None' }],
      stt_providers: [{ id: 'kiro', label: 'Kiro Crew' }],
      translation_languages: [{ id: 'ja', label: '日本語' }],
    }), true
  }
  if (path === '/api/apps/meetings/calendar') {
    return json(route, { events: [], provider: 'none', configured: false }), true
  }
  if (path === '/api/apps/meetings/meetings' && method === 'GET') {
    return json(route, {
      meetings: [{
        event_id: MEETING.event_id,
        title: MEETING.title,
        status: MEETING.status,
        started_at: MEETING.started_at,
        ended_at: '',
      }],
    }), true
  }
  if (path === '/api/apps/meetings/agents') {
    return json(route, { agents: [], task_extractor_id: '' }), true
  }
  if (path === '/api/apps/meetings/status') {
    return json(route, LIVE), true
  }
  if (path.endsWith('/init') && method === 'POST') {
    return json(route, { meeting_id: MEETING.event_id, meta: MEETING }), true
  }
  if (path.endsWith('/transcript') || path.includes('/transcript?')) {
    return json(route, { segments: SEGMENTS, next_cursor: SEGMENTS.length }), true
  }
  if (path.endsWith('/translations')) {
    const since = Number(new URL(route.request().url()).searchParams.get('since') ?? '0')
    return json(route, {
      language: 'ja',
      language_label: '日本語',
      lines: TRANSLATIONS.filter(line => line.n >= since),
      next_n: TRANSLATIONS.length,
      pending: 0,
      dropped: 0,
    }), true
  }
  if (path.endsWith('/outputs')) {
    return json(route, { outputs: {}, tasks: [] }), true
  }
  if (path.endsWith('/tasks') && method === 'GET') {
    return json(route, { tasks: [] }), true
  }
  if (path === `/api/apps/meetings/meetings/${MEETING.event_id}` && method === 'GET') {
    return json(route, { meta: MEETING, live: LIVE }), true
  }
  if (path.startsWith('/api/apps/meetings/')) {
    console.log('UNMATCHED meetings request:', method, path)
  }
  return false
}

async function openMeeting(browser, base, viewport) {
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme: 'dark', extra: meetingsApi })
  logPageProblems(page)
  await page.goto(base + '/meetings', { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: 'Return to the meeting' }).click()
  // The meeting view is up once the transcript fixture renders.
  try {
    await page.getByText('we ship the meetings translation panel on friday').first().waitFor()
  } catch (error) {
    await page.screenshot({ path: `${OUT}/debug-open-failure.png`, fullPage: true })
    console.log('DEBUG page text:', (await page.locator('body').innerText()).slice(0, 1200))
    throw error
  }
  return { context, page }
}

async function verifyToolbarCap(page) {
  // The row holds exactly the primary status action (Pause, on an active
  // meeting) and the overflow trigger — the old sibling buttons must be gone.
  for (const name of ['Refresh', 'Translation', 'Action items', 'End and review']) {
    if (await page.getByRole('button', { name, exact: true }).count()) {
      throw new Error(`"${name}" still renders as a row button`)
    }
  }
  const pause = page.getByRole('button', { name: 'Pause', exact: true })
  if (!(await pause.count())) throw new Error('primary status action (Pause) missing from the row')
  const trigger = page.getByRole('button', { name: 'More actions', exact: true })
  if (!(await trigger.count())) throw new Error('overflow trigger missing from the row')
  return trigger
}

async function openTranslationPanel(page) {
  await page.getByRole('button', { name: 'More actions', exact: true }).click()
  await page.getByRole('menuitem', { name: 'Translation' }).click()
  await page.getByText('金曜日に会議翻訳パネルをリリースします').waitFor()
}

async function main() {
  const { srv, base } = await serveDist()
  // mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
  // older than what the system Chromium's Mesa/LLVM need — strip it from the
  // browser env, the same shape the sibling capture harnesses use.
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  try {
    // 1) Wide: overflow menu open — End and review, Refresh, Translation,
    //    Action items as labelled menu items behind one trigger.
    const wide = await openMeeting(browser, base, { width: 1440, height: 1000 })
    const trigger = await verifyToolbarCap(wide.page)
    await trigger.click()
    for (const item of ['End and review', 'Refresh', 'Translation', 'Action items']) {
      if (!(await wide.page.getByRole('menuitem', { name: item }).count())) {
        throw new Error(`menu item "${item}" missing from the overflow menu`)
      }
    }
    await wide.page.screenshot({ path: `${OUT}/c-1-toolbar-overflow-menu.png` })

    // 2) Wide: translation sidebar beside the meeting at its 340px lg width.
    await wide.page.getByRole('menuitem', { name: 'Translation' }).click()
    await wide.page.getByText('金曜日に会議翻訳パネルをリリースします').waitFor()
    await wide.page.screenshot({ path: `${OUT}/c-2-translation-sidebar-wide.png` })
    await wide.context.close()

    // 3) Narrow (320px): the sidebar stacks with a bounded height instead of
    //    clipping, and the toolbar stays within the two-control cap.
    const narrow = await openMeeting(browser, base, { width: 320, height: 900 })
    await verifyToolbarCap(narrow.page)
    await openTranslationPanel(narrow.page)
    const box = await narrow.page.locator('aside[aria-label="Translation"]').boundingBox()
    if (!box) throw new Error('translation sidebar not visible at 320px')
    if (box.width > 320.5) throw new Error(`sidebar still wider than the viewport: ${box.width}px`)
    if (box.x < -0.5) throw new Error(`sidebar clipped off-canvas at x=${box.x}`)
    await narrow.page.screenshot({ path: `${OUT}/c-3-translation-sidebar-narrow.png` })
    await narrow.context.close()

    console.log(`captured 3 screenshots into ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(error => {
  console.error(error)
  process.exit(1)
})
