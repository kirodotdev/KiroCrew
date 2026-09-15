/**
 * Screenshot harness for the chat chrome typeface.
 *
 * Four surfaces render UI chrome in a HARDCODED `font-mono` (Tailwind
 * `font-mono` → `var(--mono)` → JetBrains Mono), so Settings → Display → Font
 * Family — which only writes `--font-body` — cannot reach them:
 *
 *   1. the tool-call pills in the transcript   (ToolCallLine)
 *   2. the sub-agent wave chip above the input (SubagentProgressBar)
 *   3. the composer shelf + approval pill      (ChatInput / ApprovalModePicker)
 *   4. the per-message footer                  (AssistantMessage / UserMessage)
 *
 * Surface 4 is captured under BOTH font settings, because "does not hardcode
 * mono" and "follows the setting" are different claims and only the pair proves
 * the second: the sans pass must render sans and the mono pass must render mono,
 * from the same markup.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call and the /api/ws websocket answered from
 * fixtures. No gateway, no dashboard token, no sub-agents actually spawned —
 * the client code is unmodified, only the network is stubbed, so these three
 * surfaces lay out exactly as they do in production.
 *
 * Captured per language (en + zh-CN, light theme — the CJK pass is the point:
 * JetBrains Mono carries no Han glyphs, so mono chrome falls to a system
 * fallback and mixes two sets of metrics in one row):
 *
 *   <lang>-01-tool-lines.png       transcript tool pills
 *   <lang>-02-wave-chip.png        sub-agent chip + composer + shelf
 *   <lang>-03-shelf.png            shelf alone (agent · project — model · effort)
 *   <lang>-04-full.png             whole window, all three surfaces at once
 *   <lang>-05-footer-sans.png      message footer, Font Family = Sans
 *   <lang>-05-footer-mono.png      message footer, Font Family = Mono
 *
 * Usage: node scripts/capture-chrome-font.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/chrome-font'
const SLOT = 'chat-chrome-font'
// Basename is what the shelf shows, so the fixture ends in the folder a
// KiroCrew user actually sees there.
const PROJECT = '/home/user/.kiro/crew/workspace'
const VIEW = { width: 1500, height: 1000 }

mkdirSync(OUT, { recursive: true })

/** The wave behind the chip: two running agents and one queued. */
const AGENTS = [
  {
    id: '1713e7d0',
    task: 'You are auditing the GitHub PR history of the repo kirodotdev/KiroCrew for a translation gap.',
    tool: 'gh pr list --repo kirodotdev/KiroCrew --state all --limit 700 --json number,title,state',
    tool_count: 5,
  },
  {
    id: '5c15adde',
    task: 'You are extracting the phase structure of an i18n remediation plan for a GitHub tracking issue.',
    tool: 'grep -n "Phase" knowledge/i18n-architecture-audit-and-plan.md',
    tool_count: 10,
  },
]

// `running: false` with a live wave is the real post-spawn state (the agent is
// told to END ITS TURN after spawn_run, so the slot idles while its sub-agents
// work) and it keeps the shelf's hardcoded English `title=` attributes stable —
// they flip to "Stop the current response to switch …" while a turn is running,
// which is what the crop locators key off.
const slots = [{
  key: SLOT,
  title: 'Should chat chrome follow the font setting?',
  running: false,
  last_message: 'Spawned 2 agents, waiting for results…',
  messages: 5,
  agent: 'default',
  memory_mode: 'persistent',
  project: PROJECT,
  model: '',
  reasoning_effort: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const now = Date.now() / 1000

/** A transcript whose tool pills mix prose with embedded arguments and a file
 *  chip — the exact shape that makes the pill's typeface a judgement call. */
const toolPill = (ts, content, meta) => ({ role: 'tool', ts, content, meta })

const detail = {
  running: false,
  has_more: false,
  total: 5,
  queue: [],
  project: PROJECT,
  model: '',
  reasoning_effort: '',
  messages: [
    { role: 'user', ts: now - 300, content: 'Should the chat chrome follow the app font setting?' },
    toolPill(now - 280, '🔧 Finding tailwind.config.* in website', {
      tool_call_id: 'tc_glob', purpose: 'Locate the Tailwind config', input: '{"pattern":"tailwind.config.*"}', output: 'website/tailwind.config.js',
    }),
    toolPill(now - 260, "🔧 Searching for 'font-mono' in src", {
      tool_call_id: 'tc_grep', purpose: 'Count the hardcoded mono sites', input: '{"pattern":"font-mono"}', output: '338 matches in 129 files',
    }),
    toolPill(now - 240, '🔧 Reading tailwind.config.js:1', {
      tool_call_id: 'tc_read', purpose: 'Read the fontFamily block', input: '{"path":"website/tailwind.config.js"}', output: "mono: ['var(--mono)']",
      file_path: 'website/tailwind.config.js',
    }),
    {
      role: 'assistant',
      ts: now - 200,
      content: 'Spawned 2 agents to audit the mono sites. Waiting for results before changing anything.',
      // `turn_stats` is what the backend attaches to the LAST assistant message
      // of a finished turn (chat_runner._attach_turn_stats), and it is the only
      // thing that makes the footer's billed line exist at all.
      meta: { turn_stats: { elapsed_ms: 59_000, credits: 1.98 } },
    },
  ],
}

const MODELS = [
  { model_name: 'auto', description: 'Let Kiro choose' },
  { model_name: 'claude-opus-5', description: 'Most capable' },
  { model_name: 'claude-sonnet-4.6', description: 'Balanced' },
]

const scene = { language: 'en' }
const FIXED_API = makeFixedApi(PROJECT)

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEW,
    // 10–13px chrome; a 1x shot renders the typeface difference too soft to judge.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // The shelf advertises what the turn WILL run on, resolved from config.
    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: { model: 'claude-opus-5', reasoning_effort: '', provider: 'acp' },
        session: { autocompact_pct: 90 },
        dashboard: { language: scene.language, user_role: '', user_technical_level: '' },
      })
    }
    if (path === '/api/models') return json(route, MODELS)
    if (path.startsWith('/api/effort-levels')) return json(route, ['low', 'medium', 'high', 'xhigh', 'max'])
    if (path === '/api/agents') {
      return json(route, {
        agents: [{ name: 'default', kiro_agent: 'kirocrew', description: 'Default crew agent' }],
        default_agent: 'default',
      })
    }
    if (path.startsWith('/api/agents/detail/')) return json(route, { name: 'kirocrew', model: 'claude-opus-5', skills: [] })
    if (path === '/api/agents/installed') return json(route, [])
    // The boot payload carries the UI language, and LanguageProvider treats it as
    // authoritative over the localStorage fast-path — a payload without
    // `language` reverts the whole UI to English mid-boot (see
    // LanguageProvider.test.tsx), which silently turned the zh-CN pass English.
    if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '', language: scene.language })
    // SubagentProgressBar's 30s reconcile loop: report the wave as still live so
    // a tick cannot sweep the agents out mid-capture.
    if (path === '/api/spawn') {
      return json(route, {
        agents: AGENTS.map(a => ({ id: a.id, task: a.task, done: false, parent: `dashboard:${SLOT}`, agent: 'kirocrew' })),
      })
    }
    return handleBootRoute(route, path, { project: PROJECT, theme: 'light', fixedApi: FIXED_API })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  async function load(language, family = 'sans') {
    scene.language = language
    await page.addInitScript(([slot, lang, fam]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'light')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-active-slot-chat', slot)
      // Boot fast-path so the FIRST paint is already in-language.
      localStorage.setItem('mc-lang', lang)
      // The family under test. 'sans' is the DEFAULT — nothing here is asking
      // for mono, which is the whole point: the surfaces above render mono
      // anyway. The footer pass re-runs with 'mono' to prove it follows.
      localStorage.setItem('mc-font-family', fam)
      // The footer is opt-in twice over: the timestamp needs `showTimestamps`
      // and the credits/elapsed line needs `showTurnStats`. Both default true,
      // but pinning them makes the crop independent of the shipped defaults.
      localStorage.setItem('mc-chat-config', JSON.stringify({ showTimestamps: true, showTurnStats: true, collapseAllSteps: true }))
      // Pre-ack YOLO so the pill can show the mode the report screenshotted
      // without the confirm gate opening over it.
      localStorage.setItem('mc-yolo-ack', '1')
    }, [SLOT, language, family])
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3200)
  }

  const push = async (type, data, settle = 400) => {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({ type, data }))
    await page.waitForTimeout(settle)
  }

  /** Drive the wave the way the gateway drives it (see _subagent_event).
   *  The approval pill stays on its default Normal — it is the same component
   *  and the same class in every mode, so the mode does not affect the typeface
   *  question this harness documents. */
  async function pushWave() {
    await push('subagent_queued', { slot: SLOT, queued: 1 }, 150)
    for (const a of AGENTS) {
      await push('subagent_spawn', { slot: SLOT, id: a.id, task: a.task, agent: 'kirocrew' }, 150)
      await push('subagent_tool', { slot: SLOT, id: a.id, tool: a.tool, turns: 1, tool_count: a.tool_count }, 200)
    }
    await page.waitForTimeout(600)
  }

  /** Tight crop around a locator, padded and clamped to the viewport. */
  async function crop(name, locator, pad = { x: 24, y: 16, w: 48, h: 40 }) {
    const el = locator.first()
    if (await el.count()) {
      const box = await el.boundingBox()
      if (box) {
        const x = Math.max(0, box.x - pad.x)
        const y = Math.max(0, box.y - pad.y)
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x, y,
            width: Math.min(VIEW.width - x, box.width + pad.w),
            height: Math.min(VIEW.height - y, box.height + pad.h),
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    console.log('WARN: locator missing for', name, '— full page instead')
    await page.screenshot({ path: `${OUT}/${name}.png` })
  }

  for (const lang of ['en', 'zh-CN']) {
    await load(lang)
    await pushWave()

    // Tool calls arrive folded into TurnBlock's "Worked through N steps" pane.
    // Expand it so the pills are on screen. That label is a hardcoded English
    // string (TurnBlock.tsx), so the selector survives the zh-CN pass.
    const turnToggle = page.getByRole('button', { name: /Worked through \d+ step/ }).first()
    if (await turnToggle.count()) {
      await turnToggle.click()
      await page.waitForTimeout(900)
    } else {
      console.log('WARN: no "Worked through N steps" toggle —', lang, 'tool pills may be folded')
    }

    // 1. Transcript tool pills. Crop from the first pill down over all three.
    await crop(`${lang}-01-tool-lines`, page.locator('[aria-label*="details for tool"]'), { x: 20, y: 20, w: 620, h: 130 })

    // 2. The wave chip, the composer and the shelf as one unit — the chip sits
    //    directly above the input, so one crop carries surfaces 2 and 3.
    await crop(`${lang}-02-wave-chip`, page.getByTestId('subagent-histogram'), { x: 40, y: 26, w: 900, h: 260 })

    // 3. The shelf alone: agent · project on the left, model · effort right.
    //    Anchored on the MODEL chip, found by its fixture model name rather than
    //    by a `title`/`aria-label`: those flip to the translated "Stop the
    //    current response…" wording whenever a turn — or, here, a sub-agent wave
    //    — is in flight, so a title selector silently misses on the zh-CN pass.
    //    The rect taken is the shelf ROW (chip → right group → row), so the
    //    whole row is in frame rather than one chip.
    const shelfBox = await page.evaluate(() => {
      const chip = [...document.querySelectorAll('button')]
        .find(b => b.textContent?.includes('claude-opus-5'))
      const row = chip?.parentElement?.parentElement
      if (!row) return null
      const r = row.getBoundingClientRect()
      return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }
    })
    if (shelfBox) {
      const x = Math.max(0, shelfBox.x - 8)
      const y = Math.max(0, shelfBox.y - 6)
      await page.screenshot({
        path: `${OUT}/${lang}-03-shelf.png`,
        clip: {
          x, y,
          width: Math.min(VIEW.width - x, shelfBox.width + 16),
          height: Math.min(VIEW.height - y, shelfBox.height + 12),
        },
      })
      console.log('wrote', `${OUT}/${lang}-03-shelf.png`)
    } else {
      console.log('WARN: shelf row not found for', lang)
    }

    await page.screenshot({ path: `${OUT}/${lang}-04-full.png` })
    console.log('wrote', `${OUT}/${lang}-04-full.png`)


    // Report what the surfaces actually resolved to, so a silently-empty crop
    // is obvious in the log instead of only in the PNG.
    const fonts = await page.evaluate(() => {
      const first = el => (el ? getComputedStyle(el).fontFamily.split(',')[0].replace(/['"]/g, '') : '<absent>')
      const ff = sel => first(document.querySelector(sel))
      const shelfChip = [...document.querySelectorAll('button')]
        .find(b => b.textContent?.includes('claude-opus-5'))
      return {
        toolPill: ff('[aria-label*="details for tool"]'),
        waveChip: ff('[data-testid="subagent-histogram"]'),
        shelfModel: first(shelfChip),
        approvalPill: first([...document.querySelectorAll('button')].find(b => /^(Normal|正常|Reads|Trust|YOLO)/.test((b.textContent || '').trim()))),
        body: first(document.body),
      }
    })
    console.log(`${lang} computed first-family:`, JSON.stringify(fonts))

    // 4. The per-message footer, once per font setting. The billed line is
    //    always visible; the timestamp row is hover-revealed (opacity-0 until
    //    group-hover/msg), so the message is hovered before the crop or the
    //    date is invisible in the PNG even though it is in the DOM.
    for (const family of ['sans', 'mono']) {
      await load(lang, family)
      const stats = page.getByTestId('turn-stats').first()
      if (!(await stats.count())) {
        console.log('WARN: no turn-stats footer for', lang, family)
        continue
      }
      await stats.hover()
      await page.waitForTimeout(700)
      await crop(`${lang}-05-footer-${family}`, stats, { x: 24, y: 30, w: 560, h: 96 })

      // The claim under test, read off the live DOM rather than the PNG: the
      // footer's family must TRACK the setting, and the two passes must differ.
      const seen = await page.evaluate(() => {
        const first = el => (el ? getComputedStyle(el).fontFamily.split(',')[0].replace(/['"]/g, '') : '<absent>')
        const statsEl = document.querySelector('[data-testid="turn-stats"]')
        // The timestamp is the tabular-nums span in the hover row beneath.
        const stampEl = [...document.querySelectorAll('span.tabular-nums')]
          .find(e => /\d{1,2}:\d{2}/.test(e.textContent || ''))
        return {
          billed: first(statsEl),
          timestamp: first(stampEl),
          timestampText: stampEl?.textContent || '<absent>',
          timestampTitle: stampEl?.getAttribute('title') || '<none>',
          body: first(document.body),
        }
      })
      console.log(`${lang} footer @ family=${family}:`, JSON.stringify(seen))
      if (seen.billed !== seen.body) {
        console.log(`  FAIL: footer family ${seen.billed} does not match body ${seen.body}`)
      }
      if (/\b(19|20)\d\d\b/.test(seen.timestampText)) {
        console.log(`  FAIL: footer timestamp still prints a year: ${seen.timestampText}`)
      }
    }
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
