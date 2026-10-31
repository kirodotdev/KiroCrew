/**
 * Agent-switch refusal (`409 turn_in_flight`) photographed in the REAL built SPA
 * behind `serveDist`, gateway-free: every /api/** call is answered from the shared
 * `stubDashboardApi` fixtures.
 *
 * Three frames, one per surface the notice now has:
 *   dashboard-dark / dashboard-light — the pane's own Agent picker is driven, so the
 *     frame carries BOTH surfaces at once: the shared toast from the local pane's
 *     `LocalOverlayRoot` and the in-pane `chat-pane-switch-error` the same failure
 *     writes through `reportSlotWriteFailure`.
 *   popout — the narrow window the frames-gated mount serves. The Alt+Shift agent
 *     cycle is driven rather than a picker, because that cycle has no disabled state
 *     and the toast is its ONLY surface there.
 *
 * The stubbed route is `POST /api/chat/slots/*​/agent` answering
 * `409 {"error":...,"code":"turn_in_flight"}` — the gateway's real refusal when a
 * turn starts between the picker rendering and the click landing. Nothing here
 * fabricates a status the backend does not produce.
 *
 * Every frame is written only after the assertions describing it pass.
 *
 * Usage: npm run build && node scripts/capture-agent-switch-turn-in-flight.mjs [outDir]
 */
import { chromium, expect } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { stubSplitPanes } from './lib/split-pane-fixture.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const out = process.argv[2]
  || join(process.env.KIROCREW_SCRATCH || 'temp-screenshots', 'agent-switch-notice')
mkdirSync(out, { recursive: true })

const SLOT = 'chat-1'
const REFUSAL = 'a turn is already running for this session'
const TARGET = 'oncall-triage'

// `/api/agents/catalog` is the route the picker's hook reads, and it filters on
// `selection_kind`: only `template` rows are offered. The stub's own `/api/agents`
// default carries no such field, which is why that route is not enough here.
const row = (name, selection_kind, source, description) => ({
  name, selection_kind, source, description, scope: 'global',
  kiro_agent: name, workspace: 'default',
  memory_store: selection_kind === 'member' ? `member-${name}` : 'default',
  model: '', reasoning_effort: '', triggers: '', session_color: '', avatar: {},
})
const CATALOG = [
  row('kirocrew', 'template', 'builtin', 'The stock crew agent template'),
  row(TARGET, 'template', 'package', 'Triage pager alerts against the runbook'),
]

// Pinned, not `new Date()`: a wall-clock value renders into the session list and the
// pane header, so an unpinned fixture makes every run produce different pixels and
// the committed frames churn on each capture.
const TS = '2026-10-20T15:00:00Z'

const slots = () => [{
  key: SLOT, title: 'agent switch', running: false, messages: 2, agent: 'kirocrew',
  project: '/workplace/rnoack', model: '', mode: '', last_ts: TS, created: TS,
}]

async function prepare(page, theme) {
  const errors = []
  const switchPosts = []
  page.on('pageerror', e => errors.push(e.message))
  await stubDashboardApi(page, {
    theme,
    slots: slots(),
    folders: [],
    localStorageEntries: { 'mc-lang': 'en', 'mc-active-slot': SLOT },
    extra: async (path, route) => {
      if (path === '/api/agents/catalog') {
        await json(route, { agents: CATALOG, default_agent: 'kirocrew' })
        return true
      }
      if (/^\/api\/chat\/slots\/[^/]+\/agent$/.test(path) && route.request().method() === 'POST') {
        switchPosts.push(route.request().postDataJSON?.()?.agent ?? '?')
        await json(route, { error: REFUSAL, code: 'turn_in_flight' }, 409)
        return true
      }
      return false
    },
  })
  return { errors, switchPosts }
}

const { srv, base } = await serveDist()
// A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, which the
// browser child inherits — its older libstdc++ then loses to the system
// libgallium/libLLVM and chromium fails to launch. Same override the sibling
// harnesses carry.
const browser = await chromium.launch({
  headless: true,
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' },
})
const written = []

try {
  // ---- dashboard, both themes: the toast in the local pane's overlay root, plus
  // the in-pane notice the same failure writes.
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    const { errors, switchPosts } = await prepare(page, theme)
    await page.goto(`${base}/chat/${SLOT}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    // Anchored on the composer control's own label: a loose /agent/i matches the
    // sidebar's "Agent Capabilities" row first and opens the wrong surface.
    await page.getByRole('button', { name: /^Agent: / }).first().click()
    const picker = page.getByRole('dialog', { name: 'Agent selector' })
    await picker.waitFor({ state: 'visible', timeout: 10000 })
    await picker.getByRole('option', { name: new RegExp(TARGET) }).first().click()

    // Wait on the region rather than sleeping, so the frame cannot race the
    // render or photograph an expired notice.
    const toast = page.getByTestId('agent-switch-notice')
    await toast.waitFor({ state: 'visible', timeout: 10000 })

    // The toast carries the turn_in_flight copy, not the raw server prose.
    await expect(toast).toContainText(/turn is running/i)
    expect(switchPosts).toContain(TARGET)
    expect(errors).toEqual([])

    // The notice animates in (`animate-rise`), so shooting the instant it becomes
    // visible catches it mid-transition and the frame is not byte-reproducible.
    await page.waitForTimeout(700)
    const name = `01-turn-in-flight-${theme}.png`
    await page.screenshot({ path: join(out, name) })
    written.push(name)
    await ctx.close()
  }

  // ---- popout: the narrow window the frames-gated mount serves. No picker here;
  // the Alt+Shift cycle is the surface with no disabled state to gray out.
  {
    const ctx = await browser.newContext({ viewport: { width: 520, height: 820 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    const { errors, switchPosts } = await prepare(page, 'dark')
    await page.goto(`${base}/popout/chat/${SLOT}?sid=${SLOT}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    await page.keyboard.press('Alt+Shift+A')

    const toast = page.getByTestId('agent-switch-notice')
    await toast.waitFor({ state: 'visible', timeout: 10000 })
    await expect(toast).toContainText(/turn is running/i)
    expect(switchPosts.length).toBeGreaterThan(0)
    expect(errors).toEqual([])

    await page.waitForTimeout(700)
    const name = '02-turn-in-flight-popout.png'
    await page.screenshot({ path: join(out, name) })
    written.push(name)
    await ctx.close()
  }

  // ---- split pane: the only layout that renders ChatPane, and therefore the only
  // one that carries BOTH surfaces. The pane's own Agent chip routes through
  // useChatPanePickers.switchAgent, which writes the in-pane notice as well as the
  // toast; the composer dock's picker (sessionPicks.ts) writes the toast only.
  {
    const ctx = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    const errors = []
    const switchPosts = []
    page.on('pageerror', e => errors.push(e.message))
    const LEFT = 'pane-refused'
    const RIGHT = 'pane-idle'
    const ts = '2026-10-20T15:00:00Z'
    const paneSlot = (key, title) => ({
      key, title, running: false, messages: 2, agent: 'kirocrew',
      created: ts, last_ts: ts, folder_id: '', model: '', mode: '',
    })
    await stubSplitPanes(page, {
      theme: 'dark',
      slots: [paneSlot(LEFT, 'Switch refused'), paneSlot(RIGHT, 'Idle pane')],
      transcripts: {
        [LEFT]: [{ role: 'user', content: 'switch agent', ts, meta: { mid: 'l-1' } }],
        [RIGHT]: [{ role: 'user', content: 'nothing here', ts, meta: { mid: 'r-1' } }],
      },
      layout: {
        [LEFT]: {
          type: 'split', id: 'sp-1', dir: 'row', sizes: [0.5, 0.5],
          children: [
            { type: 'leaf', id: 'lf-1', kind: 'session', slot: LEFT },
            { type: 'leaf', id: 'lf-2', kind: 'session', slot: RIGHT },
          ],
        },
      },
      extra: async (path, route) => {
        if (path === '/api/agents/catalog') {
          await json(route, { agents: CATALOG, default_agent: 'kirocrew' })
          return true
        }
        if (/^\/api\/chat\/slots\/[^/]+\/agent$/.test(path) && route.request().method() === 'POST') {
          switchPosts.push(route.request().postDataJSON?.()?.agent ?? '?')
          await json(route, { error: REFUSAL, code: 'turn_in_flight' }, 409)
          return true
        }
        return false
      },
    })
    await page.goto(`${base}/chat/${LEFT}`, { waitUntil: 'domcontentloaded' })
    const panes = page.locator('[data-chat-pane]')
    await panes.first().waitFor({ timeout: 20000 })
    await expect(panes).toHaveCount(2)

    // The refused pane is the left one; drive its own chip, not the dock's.
    const left = panes.nth(0)
    await left.getByRole('button', { name: /^Agent: / }).first().click()
    const picker = page.getByRole('dialog', { name: 'Agent list' })
    await picker.waitFor({ state: 'visible', timeout: 10000 })
    await picker.getByRole('option', { name: new RegExp(TARGET) }).first().click()

    const toast = page.getByTestId('agent-switch-notice')
    await toast.waitFor({ state: 'visible', timeout: 10000 })
    const inPane = page.getByTestId('chat-pane-switch-error')
    await inPane.waitFor({ state: 'visible', timeout: 10000 })

    // Both surfaces, same copy, in one frame -- which is the adjacency UX asked for.
    await expect(toast).toContainText(/turn is running/i)
    await expect(inPane).toContainText(/turn is running/i)
    expect(switchPosts).toContain(TARGET)
    expect(errors).toEqual([])

    await page.waitForTimeout(700)
    const name = '03-turn-in-flight-split-pane-both-surfaces.png'
    await page.screenshot({ path: join(out, name) })
    written.push(name)
    await ctx.close()
  }
} finally {
  await browser.close()
  srv.close()
}

console.log('wrote:', written.join(', '), 'into', out)
