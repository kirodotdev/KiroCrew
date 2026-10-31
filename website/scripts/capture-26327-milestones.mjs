/**
 * Screenshot harness for #26327 (Explore, Ship 1 frontend): the new-chat page
 * with its suggestions and the Try today entry card in the last slot, the
 * "Kiro Crew in 15 seconds" card at the top of Explore, Getting started, the
 * achievement bars and the badge wall, the ring
 * in its own top-bar pill next to the readout capsule, the Explore panel (Try today, Your
 * progress), the quiet notice, the Settings toggle, the phone ⋯ menu row and a
 * failed read, in dark and light, at desktop width and at 390px.
 *
 * Runs the REAL built SPA (website/dist) gateway-free behind the shared
 * stubDashboardApi fixtures. `GET /api/milestones` is the one route this adds:
 * `before` answers `{ enabled: false }` (what every surface reads as today's
 * page), `after` answers a mid-way user. Each frame checks its own DOM before
 * it is written, so a still frame is evidence of what it claims.
 *
 * Usage (in website/, after `npm run build`):
 *   node scripts/capture-26327-milestones.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/26327-explore'
mkdirSync(OUT, { recursive: true })

const DAY = 86400
const NOW = Math.floor(Date.UTC(2026, 9, 30) / 1000)
const row = (id, state, send = 'prefill', kind = 'prompt', daysAgo = 0) => ({
  id, state,
  done_at: state === 'done' ? NOW - daysAgo * DAY : null,
  source: state === 'done' ? 'event' : null,
  video: null,
  starter: { kind, send, route: kind === 'route' ? '/members?member=mate' : '', agent: '', crew_mode: id === 'crew-mode' },
  doc: 'artifacts.md',
})

const MILESTONES = {
  enabled: true,
  features: { done: 2, total: 7 },
  streak: { current: 5, best: 9 },
  groups: [
    { id: 'features', items: [
      row('widgets', 'done', 'send', 'prompt', 2),
      row('scheduled-task', 'open'),
      row('mate', 'open', 'prefill', 'route'),
      row('parallel-chats', 'open', 'send'),
      row('slack', 'open'),
      row('pr-watch', 'skipped'),
      row('crew-mode', 'done', 'prefill', 'prompt', 1),
    ] },
    { id: 'sessions', items: [{ id: 'chats-opened', value: 137, tiers: [10, 100, 1000], tiers_reached_at: { 10: NOW - 9 * DAY, 100: NOW - 2 * DAY } }] },
    { id: 'streak', items: [{ id: 'streak-best', value: 9, tiers: [3, 7, 30, 100], tiers_reached_at: { 3: NOW - 20 * DAY, 7: NOW - 16 * DAY } }] },
  ],
  recommended: [],
  notices: [],
}

// One open, empty chat: the new-chat page with its suggestions.
const SLOTS = [{ key: 'chat-1', title: 'New chat', messages: 0, running: false, agent: 'kirocrew', mode: '', created: '2026-10-30T01:00:00Z', last_ts: '2026-10-30T02:00:00Z', folder_id: '' }]
const SUGGESTIONS = { suggestions: [
  { text: 'Summarize the open PRs I review', kind: 'review' },
  { text: 'What changed in the deploy pipeline this week?', kind: 'ops' },
  { text: 'Draft the release notes for 0.12', kind: 'write' },
  { text: 'Find flaky tests in the last 20 CI runs', kind: 'code' },
  { text: 'Plan my Friday from the calendar', kind: 'tasks' },
  { text: 'Research vector stores for local search', kind: 'research' },
], generated_at: 0, stale: false }

const { srv, base } = await serveDist()
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })

async function open(theme, width, milestones, { route = '/chat?slot=chat-1', config } = {}) {
  const height = width < 500 ? 844 : 900
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots: SLOTS,
    theme,
    localStorageEntries: { 'mc-theme-mode': theme, 'mc-privacy-notice-v1': '1', 'mc-nav': '1' },
    extra: async (path, r) => {
      if (path === '/api/milestones') { await json(r, milestones); return true }
      if (path.startsWith('/api/milestones/')) { await json(r, { ok: true }); return true }
      if (path === '/api/suggestions') { await json(r, SUGGESTIONS); return true }
      if (config && path === '/api/config/kirocrew') { await json(r, config); return true }
      return false
    },
  })
  logPageProblems(page)
  await page.goto(base + route, { waitUntil: 'domcontentloaded' })
  return { context, page }
}

async function must(page, testId, present, label) {
  const n = await page.getByTestId(testId).count()
  if (present ? n === 0 : n > 0) throw new Error(`${label}: [data-testid=${testId}] ${present ? 'missing' : 'present'} — refusing to write the frame`)
}

async function welcome(page) {
  await page.getByText('Summarize the open PRs I review').waitFor({ timeout: 20000 })
  await page.waitForTimeout(400)
}

try {
  for (const theme of ['dark', 'light']) {
    for (const [vp, width] of [['desktop', 1400], ['390', 390]]) {
      const tag = `${theme}-${vp}`

      // Before: the read answers `enabled: false`, so the page is today's: six suggestions.
      {
        const { context, page } = await open(theme, width, { enabled: false })
        await welcome(page)
        await must(page, 'try-today-entry', false, `before-${tag}`)
        await must(page, 'explore-intro-video', false, `before-${tag}`)
        await must(page, 'milestones-indicator', false, `before-${tag}`)
        await page.screenshot({ path: `${OUT}/before-newchat-${tag}.png` })
        await context.close()
      }

      // After: five suggestions plus the Try today entry; the ring in the capsule.
      {
        const { context, page } = await open(theme, width, MILESTONES)
        await welcome(page)
        await must(page, 'try-today-entry', true, `after-${tag}`)
        await must(page, 'explore-intro-video', false, `after-${tag}`)
        if (vp === 'desktop') await must(page, 'milestones-pill', true, `after-${tag}`)
        await page.screenshot({ path: `${OUT}/after-newchat-${tag}.png` })
        await page.getByTestId('try-today-entry').click()
        await page.getByTestId('milestones-try-today').waitFor()
        await must(page, 'explore-intro-video', true, `explore-${tag}`)
        await must(page, 'milestones-getting-started', true, `explore-${tag}`)
        await must(page, 'milestones-badges', true, `explore-${tag}`)
        await page.waitForTimeout(400)
        await page.screenshot({ path: `${OUT}/after-explore-${tag}.png` })
        await context.close()
      }
      // Phone, Explore closed, outside a chat: the ring beside the bell is the way back in.
      if (vp === '390') {
        const { context, page } = await open(theme, width, MILESTONES, { route: '/schedule' })
        await page.getByTestId('milestones-indicator').waitFor({ timeout: 20000 })
        await must(page, 'milestones-sheet', false, `phone-ring-${tag}`)
        await page.waitForTimeout(400)
        await page.screenshot({ path: `${OUT}/after-phone-ring-${tag}.png` })
        await context.close()
      }
      console.log('wrote', tag)
    }
  }

  // The states the default frames do not reach, dark only.
  {
    // Watch opens the bundled clip in the card, poster first.
    const { context, page } = await open('dark', 1400, MILESTONES)
    await welcome(page)
    await page.getByTestId('try-today-entry').click()
    await page.getByTestId('explore-intro-video-watch').click({ timeout: 10000 })
    const player = page.getByTestId('explore-intro-video-player')
    await player.waitFor()
    await page.waitForFunction(() => {
      const v = document.querySelector('[data-testid="explore-intro-video-player"]')
      return v instanceof HTMLVideoElement && v.readyState >= 2
    }, null, { timeout: 15000 })
    await player.evaluate(v => { v.pause(); v.currentTime = 6 })
    await page.waitForTimeout(800)
    await page.screenshot({ path: `${OUT}/after-explore-intro-playing-dark-desktop.png` })
    await context.close()
  }
  {
    // A read that fails (500, not a missing route): the ring stays and Explore says so.
    const { context, page } = await open('dark', 1400, MILESTONES)
    await page.route('**/api/milestones', r => r.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' }))
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByTestId('milestones-indicator').click({ timeout: 20000 })
    await page.getByText('Could not load Explore.').waitFor({ timeout: 10000 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/after-explore-load-failed-dark-desktop.png` })
    await context.close()
  }
  {
    // Every feature tried: Try today says so, and Getting started is gone.
    const allDone = { ...MILESTONES, features: { done: 7, total: 7 }, groups: MILESTONES.groups.map(g => g.id !== 'features' ? g : { ...g, items: g.items.map(it => ({ ...it, state: 'done', done_at: NOW - DAY, source: 'event' })) }) }
    const { context, page } = await open('dark', 1400, allDone)
    await page.getByTestId('milestones-indicator').click({ timeout: 20000 })
    await page.getByTestId('milestones-all-tried').waitFor({ timeout: 10000 })
    await must(page, 'milestones-getting-started', false, 'all-tried')
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/after-explore-all-tried-dark-desktop.png` })
    await context.close()
  }
  {
    // One new milestone: one line naming it.
    const { context, page } = await open('dark', 1400, { ...MILESTONES, notices: ['chats-opened:100'] })
    await page.getByTestId('milestone-notice').waitFor({ timeout: 20000 })
    const text = await page.getByTestId('milestone-notice').innerText()
    if (!text.includes('Reached: 100 chats')) throw new Error(`single notice reads "${text}"`)
    await page.screenshot({ path: `${OUT}/after-notice-one-dark-desktop.png` })
    await context.close()
  }
  {
    // Several new ones in one read merge into one line.
    const { context, page } = await open('dark', 1400, { ...MILESTONES, notices: ['chats-opened:10', 'streak-best:7', 'widgets'] })
    await page.getByTestId('milestone-notice').waitFor({ timeout: 20000 })
    const text = await page.getByTestId('milestone-notice').innerText()
    if (!text.includes('3 new in Explore')) throw new Error(`merged notice reads "${text}"`)
    await page.screenshot({ path: `${OUT}/after-notice-merged-dark-desktop.png` })
    await context.close()
  }
  {
    // Settings › Display › View, on a gateway whose config carries the key.
    const config = { ...KIROCREW_CONFIG_FIXTURE, dashboard: { ...(KIROCREW_CONFIG_FIXTURE.dashboard || {}), milestones_enabled: true } }
    const { context, page } = await open('dark', 1400, MILESTONES, { route: '/settings?tab=display&sub=view', config })
    const toggle = page.getByRole('switch', { name: 'Explore' })
    await toggle.waitFor({ timeout: 20000 })
    await toggle.scrollIntoViewIfNeeded()
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/after-settings-display-dark-desktop.png` })
    await context.close()
  }
  {
    // Phone with a chat open: Explore is a row in the ⋯ menu.
    const { context, page } = await open('dark', 390, MILESTONES)
    await page.getByTestId('mobile-topbar-more').click({ timeout: 20000 })
    await page.getByTestId('mobile-milestones-item').waitFor({ timeout: 10000 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/after-phone-menu-dark-390.png` })
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
