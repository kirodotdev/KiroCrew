/**
 * Screenshot harness for the effort picker in the Switch All Sessions panel.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through the shared
 * `stubDashboardApi` helper. No gateway, no dashboard auth, no kiro-cli.
 *
 * Scene-specific stubs: `/api/models` (a roster with one model that takes
 * effort and Auto, which does not), `/api/config/kirocrew` (a configured
 * default effort, none, or a failed read), and `POST /api/chat/slots/model`
 * (records the body so the harness can assert what the panel sent, and answers
 * with the scene's own switch outcome).
 *
 * Scenes, each in dark and light:
 *   00 the panel just opened: no model picked, the picker disabled with its
 *      reason beneath it;
 *   01 an effort-capable model picked, the effort list open;
 *   02 High picked, the scale cue beneath the picker, the Switch count
 *      including the effort-only difference and, above the buttons, its split
 *      into the session that resets and the one that keeps its conversation;
 *   03 Auto picked: the picker disabled with its reason beneath it;
 *   04 after a Switch that skipped both changed sessions because they were
 *      still replying: the panel stays open with the status line;
 *   05 after a Switch where one session failed and a remote session manages
 *      its own effort: the failure and the status line together;
 *   06 no default effort configured in Settings: the effort list open on its
 *      Model default row;
 *   07 the Settings read failed: the notice under the effort picker;
 *   08 the Settings read failed: the notice above the composer;
 *   09 after a Switch with the Default effort row picked (nothing configured)
 *      where one session keeps its current level until it next starts: the
 *      panel stays open with the named status line and the Default row still
 *      picked (only a Default pick can defer);
 *   10 a split-view grid pane whose Settings read failed: the pane's own
 *      config-error notice above its composer.
 *
 * Usage: node scripts/capture-bulk-model-effort.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/bulk-model-effort-shots'
mkdirSync(OUT, { recursive: true })

const SIDEBAR_WIDTH = 300

// Two sessions already on the effort-capable model at different levels, and
// one on another model: picking claude-opus-4.8 at High switches two of them
// (the sonnet one for its model, the Low one for its effort).
const SLOTS = [
  { key: 'chat-1', title: 'Refactor the auth middleware', messages: 12, running: false, agent: 'kirocrew', mode: '', model: 'claude-opus-4.8', reasoning_effort: 'low' },
  { key: 'chat-2', title: 'Weekly report draft', messages: 4, running: false, agent: 'kirocrew', mode: '', model: 'claude-opus-4.8', reasoning_effort: 'high' },
  { key: 'chat-3', title: 'Debug the flaky e2e suite', messages: 27, running: false, agent: 'kirocrew', mode: '', model: 'claude-sonnet-4.7' },
]

// Scene 05 adds a session whose turns run on a remote peer: the peer owns its
// effort, so the panel never counts it and the switch reports it apart.
const REMOTE_SLOT = { key: 'chat-4', title: 'Nightly triage on the build box', messages: 9, running: false, agent: 'kirocrew', mode: '', model: 'claude-opus-4.8', reasoning_effort: 'low', executor: 'remote', instance_id: 'peer-1' }

// Scenes 07-08 show the failed-Settings notices. The composer notice appears
// only while the active slot inherits the Settings default, so the active
// session (chat-1) carries no level of its own there.
const INHERITING_SLOTS = SLOTS.map(({ reasoning_effort, ...s }) => (s.key === 'chat-1' ? s : { ...s, reasoning_effort }))

const MODELS = [
  { model_name: 'auto', description: 'Models chosen by task' },
  { model_name: 'claude-opus-4.8', description: 'Claude Opus 4.8' },
  { model_name: 'claude-sonnet-4.7', description: 'Claude Sonnet 4.7' },
]

const CONFIGURED = {
  ...KIROCREW_CONFIG_FIXTURE,
  agent: { ...KIROCREW_CONFIG_FIXTURE.agent, reasoning_effort: 'medium' },
}
const UNCONFIGURED = {
  ...KIROCREW_CONFIG_FIXTURE,
  agent: { ...KIROCREW_CONFIG_FIXTURE.agent, reasoning_effort: '' },
}

const CLEAN_SWITCH = { ok: true, model: 'claude-opus-4.8', switched: ['chat-1', 'chat-3'], skipped_running: [], unchanged: ['chat-2'], failed: [], effort_not_applied: [] }
// The partial outcomes change nothing for the sessions they name, so the
// reset/keep lists the panel still shows are exactly what a retry would do.
const BUSY = { ok: true, model: 'claude-opus-4.8', switched: [], skipped_running: ['chat-1', 'chat-3'], unchanged: ['chat-2'], failed: [], effort_not_applied: [] }
const FAILED_AND_REMOTE = { ok: true, model: 'claude-opus-4.8', switched: [], skipped_running: [], unchanged: ['chat-2'], failed: ['chat-1', 'chat-3'], effort_not_applied: ['chat-4'] }
// A switch to claude-opus-4.8 with the Default effort row picked. Only a
// Default pick can defer: the backend pushes an explicit level live, while
// clearing to Default on a live session that needs a cold start records Default
// and reports the slot in `effort_deferred` (also in `switched`, never
// `failed`). Every slot changes under a Default pick -- chat-1 and chat-2 clear
// their levels, chat-3 changes model -- and chat-1 is the one whose live
// session keeps its level until it next starts. Nothing failed and nothing is
// busy, so the deferred notice is the only thing keeping the panel open.
const DEFERRED = { ok: true, model: 'claude-opus-4.8', switched: ['chat-1', 'chat-2', 'chat-3'], skipped_running: [], unchanged: [], failed: [], effort_not_applied: [], effort_deferred: ['chat-1'] }

const sent = []

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** One page per scene group, so each carries its own config and switch outcome. */
async function openScene(theme, { config = CONFIGURED, configFails = false, response = CLEAN_SWITCH, slots = SLOTS } = {}) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  const extra = async (path, route) => {
    if (path === '/api/models') { await json(route, MODELS); return true }
    if (path === '/api/config/kirocrew') {
      if (configFails) await json(route, { error: 'config store unavailable' }, 500)
      else await json(route, config)
      return true
    }
    if (path === '/api/chat/slots/model' && route.request().method() === 'POST') {
      sent.push(route.request().postDataJSON())
      await json(route, response)
      return true
    }
    return false
  }
  await stubDashboardApi(page, {
    slots,
    theme,
    extra,
    localStorageEntries: { 'mc-active-slot': 'chat-1', 'mc-lang': 'en', 'mc-sidebar-width': String(SIDEBAR_WIDTH) },
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  const panel = () => page.locator('div.animate-rise').filter({ hasText: 'Switch All Sessions' }).first()
  const effort = () => panel().getByRole('combobox', { name: 'Effort' })
  const switchBtn = () => panel().getByRole('button', { name: /^Switch \d+ session/ })
  const shot = async (name, { list = false } = {}) => {
    await page.waitForTimeout(300)
    const out = join(OUT, `${theme}-${name}.png`)
    if (list) {
      // The open list is portalled outside the panel, so frame the region that
      // holds both instead of the panel element alone.
      const p = await panel().boundingBox()
      const l = await page.getByRole('listbox').last().boundingBox()
      if (!p || !l) throw new Error('panel or effort list has no bounding box')
      const x = Math.min(p.x, l.x) - 8
      const y = Math.min(p.y, l.y) - 8
      const w = Math.max(p.x + p.width, l.x + l.width) - x + 8
      const h = Math.max(p.y + p.height, l.y + l.height) - y + 8
      await page.screenshot({ path: out, clip: { x, y, width: w, height: h } })
    } else {
      await panel().screenshot({ path: out })
    }
    console.log('wrote', out)
  }
  const openPanel = async () => {
    await page.getByLabel('More options').first().click()
    await page.getByRole('menuitem', { name: /Switch all to model/ }).click()
    await panel().waitFor({ state: 'visible', timeout: 5000 })
  }
  const pickOpusAtHigh = async () => {
    await panel().getByRole('option', { name: /claude-opus-4\.8/ }).first().click()
    await effort().click()
    await page.getByRole('option', { name: 'High', exact: true }).click()
  }
  return { context, page, panel, effort, switchBtn, shot, openPanel, pickOpusAtHigh }
}

for (const theme of ['dark', 'light']) {
  // 00-03, then a clean Switch that closes the panel and carries the level.
  {
    const s = await openScene(theme)
    await s.openPanel()
    if (!(await s.effort().isDisabled())) throw new Error('effort picker should be disabled before a model is picked')
    await s.panel().getByTestId('bulk-effort-unsupported').waitFor({ state: 'visible', timeout: 5000 })
    await s.shot('00-no-model')

    await s.panel().getByRole('option', { name: /claude-opus-4\.8/ }).first().click()
    if (await s.effort().isDisabled()) throw new Error('effort picker still disabled after picking claude-opus-4.8')
    await s.effort().click()
    await s.page.getByRole('option', { name: 'Default from Settings · Medium' }).waitFor({ state: 'visible', timeout: 5000 })
    await s.shot('01-effort-list-open', { list: true })

    await s.page.getByRole('option', { name: 'High', exact: true }).click()
    const label = (await s.switchBtn().textContent())?.trim()
    if (label !== 'Switch 2 sessions') throw new Error(`expected "Switch 2 sessions", saw ${JSON.stringify(label)}`)
    await s.panel().getByTestId('bulk-effort-scale').waitFor({ state: 'visible', timeout: 5000 })
    const reset = (await s.panel().getByTestId('bulk-switch-reset').textContent())?.trim()
    const keep = (await s.panel().getByTestId('bulk-switch-keep').textContent())?.trim()
    if (reset !== '1 resets its conversationDebug the flaky e2e suite' || keep !== '1 keeps its conversationRefactor the auth middleware') {
      throw new Error(`expected the reset/keep split to name its sessions, saw ${JSON.stringify({ reset, keep })}`)
    }
    await s.shot('02-high-picked')

    await s.panel().getByRole('option', { name: /auto/i }).first().click()
    if (!(await s.effort().isDisabled())) throw new Error('effort picker should be disabled for auto')
    await s.panel().getByTestId('bulk-effort-unsupported').waitFor({ state: 'visible', timeout: 5000 })
    await s.shot('03-auto-disabled')

    await s.pickOpusAtHigh()
    await s.switchBtn().click()
    await s.panel().waitFor({ state: 'hidden', timeout: 5000 })
    const body = sent.at(-1)
    if (body?.model !== 'claude-opus-4.8' || body?.reasoning_effort !== 'high') {
      throw new Error(`unexpected request body ${JSON.stringify(body)}`)
    }
    await s.context.close()
  }

  // 04: both changed sessions were still replying: the panel stays open, the
  // status line names them and says what pressing Switch again does, while
  // the lists beneath still show the plan a retry would carry out.
  {
    const s = await openScene(theme, { response: BUSY })
    await s.openPanel()
    await s.pickOpusAtHigh()
    await s.switchBtn().click()
    const notice = s.panel().getByTestId('bulk-model-notice')
    await notice.waitFor({ state: 'visible', timeout: 5000 })
    const text = (await notice.textContent()) || ''
    if (!text.includes('Refactor the auth middleware') || !text.includes('Debug the flaky e2e suite') || !text.includes('Press Switch again')) {
      throw new Error(`status line should name both busy sessions and the retry, saw ${JSON.stringify(text)}`)
    }
    await s.shot('04-busy')
    await s.context.close()
  }

  // 05: one failure and one remote session in the same response.
  {
    const s = await openScene(theme, { response: FAILED_AND_REMOTE, slots: [...SLOTS, REMOTE_SLOT] })
    await s.openPanel()
    await s.pickOpusAtHigh()
    await s.switchBtn().click()
    await s.panel().getByTestId('bulk-model-error').waitFor({ state: 'visible', timeout: 5000 })
    await s.panel().getByTestId('bulk-model-notice').waitFor({ state: 'visible', timeout: 5000 })
    const failedText = (await s.panel().getByTestId('bulk-model-error').textContent()) || ''
    if (!failedText.includes('failed to switch') || !failedText.includes('Refactor the auth middleware') || !failedText.includes('Debug the flaky e2e suite')) {
      throw new Error(`the failure notice should name both failed sessions, saw ${JSON.stringify(failedText)}`)
    }
    await s.shot('05-failed-and-remote')
    await s.context.close()
  }

  // 09: the user picks the Default effort row (nothing configured, so it reads
  // "Default from Settings · not set") and one live session keeps its current
  // level until it next starts. Only a Default pick can produce this outcome.
  // Nothing failed or is busy, so the deferred notice alone holds the panel
  // open; it names the session, and the picker still shows the Default row.
  {
    const DEFAULT_ROW = 'Default from Settings · not set'
    const s = await openScene(theme, { config: UNCONFIGURED, response: DEFERRED })
    await s.openPanel()
    await s.panel().getByRole('option', { name: /claude-opus-4\.8/ }).first().click()
    await s.effort().click()
    await s.page.getByRole('option', { name: DEFAULT_ROW, exact: true }).click()
    // A Default pick clears chat-1 (Low) and chat-2 (High) and moves chat-3's model.
    const label = (await s.switchBtn().textContent())?.trim()
    if (label !== 'Switch 3 sessions') throw new Error(`expected "Switch 3 sessions" under a Default pick, saw ${JSON.stringify(label)}`)
    const before = sent.length
    await s.switchBtn().click()
    const notice = s.panel().getByTestId('bulk-model-notice')
    await notice.waitFor({ state: 'visible', timeout: 5000 })
    if (sent.length !== before + 1) throw new Error(`expected one switch request, saw ${sent.length - before}`)
    const body = sent.at(-1)
    if (body?.model !== 'claude-opus-4.8' || !Object.hasOwn(body, 'reasoning_effort') || body.reasoning_effort !== '') {
      throw new Error(`a Default pick must send reasoning_effort "" with the model, sent ${JSON.stringify(body)}`)
    }
    const text = (await notice.textContent()) || ''
    if (!text.includes('Refactor the auth middleware') || !text.includes('next starts')) {
      throw new Error(`the deferred notice should name the session and say it waits for its next start, saw ${JSON.stringify(text)}`)
    }
    if (await s.panel().getByTestId('bulk-model-error').count() > 0) {
      throw new Error('a deferred-only outcome must not render the error notice')
    }
    if (!(await s.page.getByText('Switch All Sessions').count())) throw new Error('the panel should stay open on a deferred outcome')
    const picked = ((await s.effort().textContent()) || '').trim()
    if (picked !== DEFAULT_ROW) throw new Error(`the effort picker should still show ${JSON.stringify(DEFAULT_ROW)} after the switch, saw ${JSON.stringify(picked)}`)
    await s.shot('09-effort-deferred')
    await s.context.close()
  }

  // 06: nothing configured in Settings: the Default row still anchors to
  // Settings, marked as not set, rather than reading a bare "Model default".
  {
    const s = await openScene(theme, { config: UNCONFIGURED })
    await s.openPanel()
    await s.panel().getByRole('option', { name: /claude-opus-4\.8/ }).first().click()
    await s.effort().click()
    await s.page.getByRole('option', { name: 'Default from Settings · not set' }).waitFor({ state: 'visible', timeout: 5000 })
    await s.shot('06-model-default-row', { list: true })
    await s.context.close()
  }

  // 07-08: the Settings read failed: the panel's notice, then the composer's.
  // The composer notice only shows while the active slot's effort inherits the
  // Settings default, so chat-1 runs here with no level of its own; its
  // control must read Default beside the notice.
  {
    const s = await openScene(theme, { configFails: true, slots: INHERITING_SLOTS })
    const composerNotice = s.page.getByTestId('default-effort-config-error')
    await composerNotice.waitFor({ state: 'visible', timeout: 5000 })
    const composerText = (await composerNotice.textContent()) || ''
    if (composerText.includes('config store unavailable')) {
      throw new Error('the composer notice must not show the raw server text')
    }
    const effortCtl = s.page.locator('[title^="Reasoning effort: "]')
    if ((await effortCtl.count()) !== 1) throw new Error(`expected one composer effort control, got ${await effortCtl.count()}`)
    const effortText = ((await effortCtl.textContent()) || '').trim()
    if (!effortText.includes('Default')) {
      throw new Error(`the notice says the effort control shows Default, but it reads ${JSON.stringify(effortText)}`)
    }
    // Frame the notice, the composer and the effort control under it, so the
    // frame shows the control the notice names.
    const n = await composerNotice.boundingBox()
    const c = await s.page.getByRole('textbox').last().boundingBox()
    const e = await effortCtl.boundingBox()
    if (!n || !c || !e) throw new Error('composer notice, composer or effort control has no bounding box')
    const out = join(OUT, `${theme}-08-config-error-composer.png`)
    const x = Math.min(n.x, c.x, e.x) - 16
    const y = n.y - 16
    const w = Math.max(n.x + n.width, c.x + c.width, e.x + e.width) - x + 16
    const h = Math.max(c.y + c.height, e.y + e.height) - y + 16
    await s.page.screenshot({ path: out, clip: { x, y, width: w, height: h } })
    console.log('wrote', out, `(effort control reads ${JSON.stringify(effortText)})`)

    await s.openPanel()
    await s.panel().getByRole('option', { name: /claude-opus-4\.8/ }).first().click()
    await s.panel().getByTestId('bulk-effort-config-error').waitFor({ state: 'visible', timeout: 5000 })
    await s.shot('07-config-error-panel')
    await s.context.close()
  }
}

// 10: the grid-pane config-error notice. A split-view pane is a different mount
// than the single chat page above (ChatPane, not ChatPage), so it carries its
// own `chat-pane-default-effort-config-error` when the Settings read fails.
// This scene photographs that notice, which no other frame covered. Split view
// needs the session-grid flag, a persisted two-pane layout, and a transcript
// per pane; the layout and active slot are seeded through stubDashboardApi's own
// localStorage init so they land after its clear.
// The left pane inherits its effort (no level of its own), so the failed read
// leaves its control on Default and it carries the notice; the right pane has
// its own level, so it shows that level and no notice.
const GRID_SLOTS = [
  { key: 'grid-a', title: 'Refactor the auth middleware', messages: 3, running: false, agent: 'kirocrew', mode: '', model: 'claude-opus-4.8' },
  { key: 'grid-b', title: 'Weekly report draft', messages: 2, running: false, agent: 'kirocrew', mode: '', model: 'claude-opus-4.8', reasoning_effort: 'high' },
]
const GRID_LAYOUT = {
  'grid-a': {
    type: 'split', id: 'sp-grid', dir: 'row', sizes: [0.5, 0.5],
    children: [
      { type: 'leaf', id: 'lf-a', kind: 'session', slot: 'grid-a' },
      { type: 'leaf', id: 'lf-b', kind: 'session', slot: 'grid-b' },
    ],
  },
}
const GRID_TRANSCRIPTS = {
  'grid-a': [
    { role: 'user', content: 'Can you take the auth middleware refactor?', ts: '2026-09-25T09:58:00Z', meta: { mid: 'ga-1' } },
    { role: 'assistant', content: 'On it — reading the current middleware first.', ts: '2026-09-25T09:59:00Z', meta: { mid: 'ga-2' } },
  ],
  'grid-b': [
    { role: 'user', content: 'Draft the weekly report.', ts: '2026-09-25T09:28:00Z', meta: { mid: 'gb-1' } },
    { role: 'assistant', content: 'Here is a first pass at the summary.', ts: '2026-09-25T09:29:00Z', meta: { mid: 'gb-2' } },
  ],
}
for (const theme of ['dark', 'light']) {
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    slots: GRID_SLOTS,
    theme,
    localStorageEntries: { 'mc-active-slot': 'grid-a', 'mc-lang': 'en', 'mc-split-layouts': JSON.stringify(GRID_LAYOUT) },
    extra: async (path, route) => {
      if (path === '/api/models') { await json(route, MODELS); return true }
      // The whole point of the scene: the Settings read fails.
      if (path === '/api/config/kirocrew') { await json(route, { error: 'config store unavailable' }, 500); return true }
      // session_grid gates split view; keep the rest of the shared default.
      if (path === '/api/dashboard/config') {
        await json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more', session_grid: true })
        return true
      }
      for (const [slot, messages] of Object.entries(GRID_TRANSCRIPTS)) {
        if (path === `/api/chat/slots/${slot}`) { await json(route, { messages, has_more: false, total: messages.length }); return true }
      }
      if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
      if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
      return false
    },
  })
  await page.goto(base + '/chat/grid-a', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const panes = page.locator('[data-chat-pane]')
  await panes.first().waitFor({ state: 'visible', timeout: 20000 })
  const paneCount = await panes.count()
  if (paneCount !== 2) throw new Error(`expected 2 grid panes (split view), got ${paneCount} — the frame would show single chat`)
  const notice = page.getByTestId('chat-pane-default-effort-config-error').first()
  await notice.waitFor({ state: 'visible', timeout: 10000 })
  const noticeText = (await notice.textContent()) || ''
  if (noticeText.includes('config store unavailable')) throw new Error('the pane notice must not show the raw server text')
  const noticeCount = await page.getByTestId('chat-pane-default-effort-config-error').count()
  if (noticeCount !== 1) throw new Error(`expected the notice in exactly 1 pane (the inheriting one), got ${noticeCount}`)
  // The pane that carries the notice must be the inheriting one, and its
  // effort control must read Default -- the notice names exactly that control.
  const holder = panes.filter({ has: page.getByTestId('chat-pane-default-effort-config-error') }).first()
  const holderTitle = ((await holder.textContent()) || '')
  if (!holderTitle.includes('Refactor the auth middleware')) throw new Error('the notice should sit in the inheriting pane (grid-a)')
  const holderEffort = ((await holder.locator('[title^="Reasoning effort: "]').first().textContent()) || '').trim()
  if (!holderEffort.includes('Default')) {
    throw new Error(`the notice says the effort control shows Default, but it reads ${JSON.stringify(holderEffort)}`)
  }
  // The other pane keeps its own level and so shows no notice.
  const other = panes.filter({ hasNot: page.getByTestId('chat-pane-default-effort-config-error') }).first()
  const otherEffort = ((await other.locator('[title^="Reasoning effort: "]').first().textContent()) || '').trim()
  if (!otherEffort.includes('High')) throw new Error(`the pane with its own level should read High, saw ${JSON.stringify(otherEffort)}`)
  // Frame the pane that carries the notice (half-width in a row split), so the
  // config-error above its composer is the subject.
  const out = join(OUT, `${theme}-10-config-error-grid-pane.png`)
  await holder.screenshot({ path: out })
  console.log('wrote', out, `(notice pane effort ${JSON.stringify(holderEffort)}, other pane ${JSON.stringify(otherEffort)})`)
  await context.close()
}

await browser.close()
srv.close()
console.log('request bodies:', JSON.stringify(sent))
