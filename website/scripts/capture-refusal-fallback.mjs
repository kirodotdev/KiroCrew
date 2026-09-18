/**
 * Screenshot harness for the refusal-fallback model setting.
 *
 * Runs the REAL built SPA (website/dist) behind the in-process static server
 * with the shared stub answering the boot-path /api/** fixtures; only the
 * feature-specific routes (config with the refusal field, models, the fixture
 * transcript) live here, via the stub's `extra` seam.
 *
 * Six scenes: the Settings card disabled (dark) and pinned (light), the
 * picker open with the Auto option visible, and the feature's two chat
 * surfaces — the in-flight retry notice and the retry-exhausted refusal card —
 * rendered from fixture messages that mirror chat_runner's own `slot.append`
 * payloads byte-for-byte (the notice carries the `transient_retry` meta kind
 * the renderer keys on).
 *
 * Usage: node scripts/capture-refusal-fallback.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/refusal-fallback'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const MODELS = [
  { model_name: 'auto', description: 'Let Kiro choose' },
  { model_name: 'claude-opus-4.8', description: 'Most capable' },
  { model_name: 'claude-sonnet-4.5', description: 'Balanced' },
  { model_name: 'claude-haiku-4.5', description: 'Fastest' },
]

const scene = { refusal: '', slots: [], slotDetail: null }

/** Feature routes only — the shared stub owns the boot path. */
const extra = async (path, route) => {
  const method = route.request().method()
  if (path === '/api/config/kirocrew' && method === 'PATCH') {
    const body = JSON.parse(route.request().postData() || '{}')
    if (body.path === 'agent.refusal_fallback_model') scene.refusal = body.value
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/config/kirocrew') {
    await json(route, {
      ...KIROCREW_CONFIG_FIXTURE,
      agent: {
        ...KIROCREW_CONFIG_FIXTURE.agent,
        reasoning_effort: '',
        fallback_model: 'auto',
        refusal_fallback_model: scene.refusal,
        completion_keep: 'head',
        completion_keep_chars: 3000,
        soft_stop_budget_secs: 10,
      },
      dashboard: { user_role: '', user_technical_level: '' },
    })
    return true
  }
  if (path === '/api/models') { await json(route, MODELS); return true }
  if (/^\/api\/chat\/slots\/[^/]+$/.test(path) && scene.slotDetail) {
    await json(route, scene.slotDetail)
    return true
  }
  return false
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
  })

  let page = null

  /** A FRESH page per scene — stubDashboardApi bakes the theme into /api/theme/boot. */
  async function load(url, theme = 'dark', activeSlot = '') {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    const localStorageEntries = { 'mc-onboarded': '1' }
    if (activeSlot) localStorageEntries['mc-active-slot'] = activeSlot
    await stubDashboardApi(page, { slots: scene.slots, theme, extra, localStorageEntries })
    await page.goto(base + url, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  /** Tight crop around the two fallback cards — the whole story. */
  async function fallbackCards(name, pad = 20) {
    const throttle = page.getByText('Rate-limit fallback', { exact: true }).first()
    const refusal = page.getByText('Content-filter fallback', { exact: true }).first()
    await refusal.scrollIntoViewIfNeeded()
    await page.waitForTimeout(300)
    const row = page.locator('[data-setting-label="Content-filter fallback model"]').first()
    const boxes = []
    for (const loc of [throttle, refusal, row]) {
      const b = await loc.boundingBox().catch(() => null)
      if (b) boxes.push(b)
    }
    if (boxes.length) {
      const x0 = Math.max(0, Math.min(...boxes.map(b => b.x)) - pad)
      const y0 = Math.max(0, Math.min(...boxes.map(b => b.y)) - pad)
      const x1 = Math.max(...boxes.map(b => b.x + b.width)) + pad
      const y1 = Math.max(...boxes.map(b => b.y + b.height)) + pad
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: { x: x0, y: y0, width: Math.min(1500 - x0, x1 - x0), height: y1 - y0 },
      })
    } else {
      await page.screenshot({ path: `${OUT}/${name}.png` })
    }
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // 1. Default: Disabled — the shipped state.
  scene.refusal = ''
  await load('/settings?tab=chat')
  await fallbackCards('01-refusal-fallback-disabled-dark')

  // 2. A concrete pinned fallback, light theme for contrast coverage.
  scene.refusal = 'claude-opus-4.8'
  await load('/settings?tab=chat', 'light')
  await fallbackCards('02-refusal-fallback-pinned-light')

  // 3. The dropdown OPEN — 'Auto (model named in the refusal)' visible among options.
  scene.refusal = ''
  await load('/settings?tab=chat')
  {
    const row = page.locator('[data-setting-label="Content-filter fallback model"]').first()
    await row.scrollIntoViewIfNeeded()
    const trigger = row.locator('[role="combobox"]').first()
    await trigger.click()
    const auto = page.getByRole('option', { name: 'Auto (model named in the refusal)' }).first()
    await auto.waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    const rb = await row.boundingBox()
    const ab = await auto.boundingBox()
    const pad = 24
    const x0 = Math.max(0, Math.min(rb.x, ab.x) - pad)
    const y0 = Math.max(0, Math.min(rb.y, ab.y) - pad)
    const x1 = Math.max(rb.x + rb.width, ab.x + ab.width) + pad
    const y1 = Math.max(rb.y + rb.height, ab.y + ab.height) + pad
    await page.screenshot({
      path: `${OUT}/03-refusal-fallback-dropdown-open-dark.png`,
      clip: { x: x0, y: y0, width: Math.min(1500 - x0, x1 - x0), height: y1 - y0 },
    })
    console.log('wrote', `${OUT}/03-refusal-fallback-dropdown-open-dark.png`)
  }

  // 4 + 5. The feature's chat surfaces, from a fixture transcript.
  const RETRY_NOTICE =
    "⟳ Response declined by the model's content filter on 'claude-fable-5' — retrying once on 'claude-opus-4.8'…"
  const EXHAUSTED_CARD =
    'Response declined by the model. Content filter: cyber. '
    + 'Try rephrasing your request, or start a new conversation without the '
    + 'content that tripped the filter. '
    + "The configured fallback model ('claude-opus-4.8') also declined this request."
  const now = Date.now() / 1000
  const USER_MSG = {
    role: 'user', ts: now - 90,
    content: 'Summarize the exploit-chain writeup in this incident report.',
  }
  const scenes = [
    {
      name: '04-refusal-retry-notice-in-chat-dark',
      anchor: 'retrying once on',
      messages: [USER_MSG, { role: 'error', ts: now - 60, content: RETRY_NOTICE, meta: { kind: 'transient_retry' } }],
    },
    {
      name: '05-refusal-fallback-also-declined-dark',
      anchor: 'also declined this request.',
      messages: [
        USER_MSG,
        { role: 'error', ts: now - 60, content: RETRY_NOTICE, meta: { kind: 'transient_retry' } },
        { role: 'error', ts: now - 30, content: EXHAUSTED_CARD },
      ],
    },
    {
      // The third chat outcome: a Stop or queued correction supersedes the
      // replay before it dispatches — the cancelled notice replaces the retry.
      name: '06-refusal-retry-cancelled-dark',
      anchor: 'retry cancelled',
      messages: [
        USER_MSG,
        { role: 'error', ts: now - 60, content: RETRY_NOTICE, meta: { kind: 'transient_retry' } },
        {
          role: 'notice', ts: now - 45,
          content: 'ℹ️ Content-filter retry cancelled — your newer message runs instead.',
        },
        {
          role: 'user', ts: now - 30,
          content: 'Actually, just list the affected hosts from the report instead.',
        },
      ],
    },
  ]
  for (const sc of scenes) {
    const SLOT = 'chat-refusal-demo'
    scene.slots = [{
      key: SLOT, title: 'Incident report summary', running: false,
      last_message: sc.messages[sc.messages.length - 1].content.slice(0, 60),
      messages: sc.messages.length, agent: 'kirocrew', memory_mode: 'persistent',
      project: PROJECT, folder_id: '', modified: Math.floor(Date.now() / 1000),
      source_links: [], source_links_total: 0,
    }]
    scene.slotDetail = {
      running: false, has_more: false, total: sc.messages.length,
      queue: [], project: PROJECT, messages: sc.messages,
    }
    await load('/', 'dark', SLOT)
    const anchor = page.getByText(sc.anchor, { exact: false }).first()
    await anchor.waitFor({ state: 'visible', timeout: 8000 })
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${sc.name}.png` })
    console.log('wrote', `${OUT}/${sc.name}.png`)
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
