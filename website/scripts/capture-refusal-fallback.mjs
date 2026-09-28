/**
 * Screenshot harness for the refusal-fallback model setting.
 *
 * Runs the REAL built SPA (website/dist) behind the in-process static server
 * with the shared stub answering the boot-path /api/** fixtures; only the
 * feature-specific routes (config with the refusal field, models, the fixture
 * transcript) live here, via the stub's `extra` seam.
 *
 * Seven scenes: the Settings card disabled (dark) and pinned (light), the
 * picker open with the Auto option visible, and the feature's chat
 * surfaces — the in-flight retry notice, the retry-exhausted refusal card,
 * the cancelled retry, and the tool-call path (the continuation card on the
 * fallback model) — rendered from fixture messages that mirror chat_runner's
 * own `slot.append` payloads byte-for-byte (the notice carries the
 * `transient_retry` meta kind the renderer keys on).
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
  // The tool-call path: the same refusal after the turn already ran a tool.
  // Mirrors chat_runner's continue notice and chat_utils._REFUSAL_FALLBACK_RESUME_MSG
  // byte-for-byte — the marker line is what RecoveryCard keys the card on.
  const CONTINUE_NOTICE =
    "⟳ Response declined by the model's content filter on 'claude-fable-5' after 1 tool call — "
    + "continuing on 'claude-opus-4.8' in the same session…"
  const CONTINUE_BODY =
    '[Content filter — continuing on the fallback model]\n'
    + 'The previous turn ended before it finished. Look at the conversation above, '
    + "work out what was already completed, and finish the user's most recent "
    + 'request from there. Do NOT re-run steps or tools that already completed '
    + 'successfully. If the completed work is not visible in the conversation '
    + 'above, do NOT start the request over — say so and stop.'
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
    {
      // The tool-call path: the filter declined AFTER the turn ran a tool, so
      // the message is not replayed (the tool would run twice) — the session
      // moves to the fallback model and CONTINUES from the completed work. The
      // inject row is the continuation the fallback reads, folded by
      // RecoveryCard into its own card; the fallback's answer follows it.
      name: '07-refusal-fallback-continue-after-tool-call-dark',
      anchor: 'Content filter declined the turn',
      expandSteps: true,
      messages: [
        USER_MSG,
        {
          role: 'assistant', ts: now - 80,
          content: 'Locating the exploit-chain section before summarizing it:',
        },
        {
          role: 'tool', ts: now - 79,
          content: '🔧 grep -n "exploit chain" incident-report.md | head -5',
          meta: {
            tool_call_id: 'toolu_demo_grep', tool_name: 'shell', kind: 'execute', done: true,
            purpose: 'Locate the exploit-chain section of the report',
            input: '{"command": "grep -n \\"exploit chain\\" incident-report.md | head -5"}',
            output: '41:## Exploit chain\n43:Stage 1 — initial access via the exposed admin endpoint',
          },
        },
        { role: 'error', ts: now - 60, content: CONTINUE_NOTICE, meta: { kind: 'transient_retry' } },
        { role: 'inject', ts: now - 59, content: CONTINUE_BODY, meta: { injectKind: 'recovery' } },
        {
          role: 'assistant', ts: now - 30,
          content: 'Resuming from the grep result — the exploit chain in the report has three stages: '
            + 'initial access through the exposed admin endpoint, credential reuse against the '
            + 'build host, and lateral movement to the artifact store.',
        },
      ],
    },
  ]
  // 8. The same tool-call path in the light theme — contrast coverage for the
  //    new card kind, which is the only surface this change adds.
  scenes.push({
    ...scenes[scenes.length - 1],
    name: '08-refusal-fallback-continue-after-tool-call-light',
    theme: 'light',
  })
  // 9. The same scene with the continuation card EXPANDED: the folded body is
  //    the prompt the fallback model actually reads (_REFUSAL_FALLBACK_RESUME_MSG),
  //    so the review needs to see it, not just the collapsed summary row.
  scenes.push({
    ...scenes[scenes.length - 2],
    name: '09-refusal-fallback-continue-after-tool-call-expanded-dark',
    expandCard: true,
  })
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
    await load('/', sc.theme || 'dark', SLOT)
    if (sc.expandSteps) {
      // The pre-refusal text, the tool row and the continuation card fold into
      // the "Worked through N steps" group; the card is the point of the scene.
      const toggle = page.getByRole('button', { name: /Worked through \d+ steps?/ }).first()
      await toggle.waitFor({ state: 'visible', timeout: 8000 })
      await toggle.click()
      await page.waitForTimeout(400)
    }
    const anchor = page.getByText(sc.anchor, { exact: false }).first()
    await anchor.waitFor({ state: 'visible', timeout: 8000 })
    if (sc.expandCard) {
      // The card folds its body behind its own toggle (the row that names the
      // title and detail); open it so the continuation prompt is in the frame.
      const cardToggle = page.getByTestId('recovery-card-toggle').first()
      await cardToggle.waitFor({ state: 'visible', timeout: 8000 })
      await cardToggle.click()
      await page.getByTestId('recovery-card-body').first().waitFor({ state: 'visible', timeout: 8000 })
    }
    await page.waitForTimeout(300)
    await page.screenshot({ path: `${OUT}/${sc.name}.png` })
    console.log('wrote', `${OUT}/${sc.name}.png`)
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
