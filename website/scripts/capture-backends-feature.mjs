/**
 * Screenshot harness for the config-defined backends feature.
 *
 * Three frames, one per user-visible surface the change adds:
 *   1. Settings -> Backends: the registry listing, with one config-defined
 *      backend selectable beside the builtins, one INVALID descriptor and one
 *      UNROUTABLE descriptor each shown with its reason.
 *   2. New chat: the welcome-screen backend picker open, offering the
 *      config-defined backend beside the builtins with the default flagged.
 *   3. An open chat pinned to that backend: the read-only backend chip in the
 *      composer shelf beside the agent chip.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures: no gateway, no
 * dashboard token, no provider CLI.
 *
 * Usage: node scripts/capture-backends-feature.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/backends-feature'
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

// The listing the registry endpoint serves: builtins plus one config-defined
// backend ("acme", from harnesses.json) that registered as selectable, one
// descriptor that failed validation, and one that declared no verified routing.
const BACKENDS = {
  backends: [
    { id: '', label: 'Kiro CLI', is_global_default: true },
    { id: 'claude', label: 'Claude Code', is_global_default: false },
    { id: 'kas', label: 'Kiro Agent Server', is_global_default: false },
    { id: 'codex', label: 'Codex', is_global_default: false },
    { id: 'acme', label: 'Acme Agent', is_global_default: false },
  ],
  invalid: [
    { id: 'broken', label: 'broken', reasons: ['argv[0] must be "{executable}"', 'model_source is \'static\' but no models are declared'] },
  ],
  unroutable: [
    { id: 'shadow', label: 'Shadow Agent', reason: 'descriptor declares no routing, so nothing establishes its tool calls reach the permission gate' },
  ],
}

const MODELS_BY_BACKEND = {
  '': [{ model_name: 'auto', description: 'Models chosen by task' }, { model_name: 'claude-opus-5', description: 'Claude Opus 5' }],
  acme: [{ model_name: 'acme-large', description: 'Acme Large' }, { model_name: 'acme-mini', description: 'Acme Mini' }],
}

const EMPTY_SLOT = 'chat-new'
const PINNED_SLOT = 'chat-pinned'

const SLOTS = [
  {
    key: EMPTY_SLOT, title: '', running: false, messages: 0, agent: 'kirocrew', model: 'auto',
    acp_backend: '', modified: now, last_ts: '2026-09-18T16:00:00Z', folder_id: '',
  },
  {
    key: PINNED_SLOT, title: 'Draft the release notes', running: false, messages: 2, agent: 'kirocrew',
    model: 'acme-large', acp_backend: 'acme', modified: now - 300, last_ts: '2026-09-18T15:55:00Z', folder_id: '',
    last_message: 'Here is a first draft of the notes.',
  },
]

const pinnedDetail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Draft the release notes for 0.8.', ts: '2026-09-18T15:54:00Z' },
    { role: 'assistant', content: 'Here is a first draft of the notes.', ts: '2026-09-18T15:55:00Z' },
  ],
}
const emptyDetail = { running: false, has_more: false, total: 0, queue: [], messages: [] }

const extra = async (path, route) => {
  const url = new URL(route.request().url())
  if (path === '/api/backends') return await json(route, BACKENDS), true
  if (path === '/api/chat/slots') return await json(route, SLOTS), true
  if (path.startsWith('/api/chat/slots/' + PINNED_SLOT)) return await json(route, pinnedDetail), true
  if (path.startsWith('/api/chat/slots/')) return await json(route, emptyDetail), true
  if (path === '/api/models') {
    const b = url.searchParams.get('backend') || ''
    return await json(route, MODELS_BY_BACKEND[b] || MODELS_BY_BACKEND['']), true
  }
  if (path.startsWith('/api/agents/') && path.endsWith('/resolved-model')) return await json(route, { model: 'auto' }), true
  return false
}

async function newPage(browser, activeSlot) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    extra,
    localStorageEntries: {
      'mc-lang': 'en',
      'mc-active-slot': activeSlot,
      'mc-privacy-notice-v1': '1',
      'mc-sidebar-pinned': 'true',
    },
  })
  return { context, page }
}

async function shot(target, name) {
  const out = join(OUT, name)
  await target.screenshot({ path: out })
  console.log('wrote', out)
}

async function captureSettings(browser, base) {
  const { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/settings/backends', { waitUntil: 'domcontentloaded' })
  await page.getByText('Acme Agent', { exact: true }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.getByText('Shadow Agent', { exact: true }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)
  await shot(page, '01-settings-backends.png')
  await context.close()
}

async function capturePicker(browser, base) {
  const { context, page } = await newPage(browser, EMPTY_SLOT)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  const trigger = page.getByLabel(/^Backend: /).first()
  await trigger.waitFor({ state: 'visible', timeout: 20000 })
  await trigger.click()
  await page.getByRole('option', { name: /Acme Agent/ }).waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(400)
  await shot(page, '02-new-chat-backend-picker.png')
  await context.close()
}

async function captureChip(browser, base) {
  const { context, page } = await newPage(browser, PINNED_SLOT)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  // Open the pinned chat from the session list: the stored active-slot key is
  // not authoritative on a cold load, and the transcript preview in the list
  // would satisfy a bare text wait without the chat ever being opened.
  const row = page.getByText('Draft the release notes', { exact: true }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  const chip = page.getByTestId('chat-input-backend-chip')
  await chip.waitFor({ state: 'visible', timeout: 20000 })
  // The assertion the frame exists for: the chip names the PINNED backend.
  await chip.getByText('Acme Agent', { exact: true }).waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(500)
  await shot(page, '03-chat-backend-chip.png')
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await captureSettings(browser, base)
    await capturePicker(browser, base)
    await captureChip(browser, base)
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()
