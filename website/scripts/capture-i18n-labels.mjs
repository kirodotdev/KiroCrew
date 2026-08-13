/**
 * Screenshot harness for the label tables this branch converted.
 *
 * Three surfaces rendered raw English under translated headings, because their
 * copy sat in module-level ALL-CAPS tables (invisible to every i18n gate) or in
 * an inline tile array (visible, deferred):
 *
 *   1. session filter + sort menu   `pages/ChatSidebar.tsx`
 *   2. reasoning-effort popover     `lib/effort.ts` via ReasoningEffortDropdown
 *   3. Overview status tiles        `pages/OverviewPage.tsx`
 *
 * Captured in zh-CN (the locale the bug was reported in) plus de/it — the longest
 * renderings, which is what a screenshot catches and a string assertion cannot:
 * `Betriebszeit` and `Tempo di attività` have to fit a 150px tile, and the effort
 * popover is a fixed 240px.
 *
 * Every locator is derived from the CATALOG rather than hardcoded, so the script
 * fails loudly if a key is renamed instead of silently screenshotting the wrong
 * element. Runs the REAL built SPA (website/dist) with every /api/** call
 * answered from fixtures — no gateway, no token. Same technique as
 * capture-chrome-font.mjs.
 *
 * Usage: node scripts/capture-i18n-labels.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/i18n-labels'
const SLOT = 'i18n-labels'
const PROJECT = '/home/user/.kiro/crew/workspace'
const VIEW = { width: 1500, height: 1000 }
// `fileURLToPath`, not `.pathname`: the latter yields `/C:/…` on Windows and
// leaves percent-encoding in place, so the catalog read fails there.
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))

mkdirSync(OUT, { recursive: true })

/** Catalog lookup, so a renamed key breaks the capture instead of the crop. */
const CATALOGS = {}
function t(lang, key) {
  if (!CATALOGS[lang]) {
    const read = (f) => JSON.parse(readFileSync(LOCALES + f, 'utf-8'))
    CATALOGS[lang] = lang === 'en'
      ? { ...read('en.json'), ...read('en.manual.json') }
      : read(`${lang}.json`)
    if (lang === 'en') {
      // Shallow spread would drop the manual catalog's nested siblings.
      const [gen, manual] = [read('en.json'), read('en.manual.json')]
      const merge = (a, b) => {
        const out = { ...a }
        for (const [k, v] of Object.entries(b)) {
          out[k] = v && typeof v === 'object' && a[k] && typeof a[k] === 'object' ? merge(a[k], v) : v
        }
        return out
      }
      CATALOGS.en = merge(gen, manual)
    }
  }
  const value = key.split('.').reduce((node, part) => (node ?? {})[part], CATALOGS[lang])
  if (typeof value !== 'string') throw new Error(`${lang}: no catalog value for ${key}`)
  return value
}

const now = Date.now() / 1000

/** Two slots so the filter counts render non-zero beside their labels. */
const slots = [
  {
    key: SLOT,
    title: 'Reasoning effort shows English in zh-CN',
    running: false,
    last_message: 'Checked the label tables.',
    messages: 4,
    agent: 'default',
    memory_mode: 'persistent',
    project: PROJECT,
    model: 'claude-opus-5',
    reasoning_effort: 'high',
    modified: Math.floor(now),
    source_links: [],
    source_links_total: 0,
    unread: 1,
    pinned: true,
  },
  {
    key: 'i18n-labels-2',
    title: 'Session filter labels',
    running: true,
    last_message: 'Working…',
    messages: 2,
    agent: 'default',
    memory_mode: 'persistent',
    project: PROJECT,
    model: 'claude-opus-5',
    reasoning_effort: '',
    modified: Math.floor(now - 600),
    source_links: [],
    source_links_total: 0,
  },
]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  model: 'claude-opus-5',
  reasoning_effort: 'high',
  messages: [
    { role: 'user', ts: now - 200, content: 'Is the effort value translated?' },
    { role: 'assistant', ts: now - 190, content: 'It is now — the level labels come from the catalog.' },
  ],
}

const MODELS = [
  { model_name: 'claude-opus-5', description: 'Most capable' },
  { model_name: 'claude-sonnet-4.6', description: 'Balanced' },
]

const status = {
  sessions: 12, messages: 4821, cron_jobs: 7, subagents: 3, lessons: 52,
  // `useUptime` ticks off `start_time`, not a precomputed duration.
  start_time: Math.floor(now) - 31749,
  version: '0.1.2-nightly.20260801t084716',
}

const scene = { language: 'en' }
const FIXED_API = makeFixedApi(PROJECT)

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path === '/api/status') return json(route, status)
    if (path === '/api/models') return json(route, MODELS)
    if (path.startsWith('/api/effort-levels')) return json(route, ['low', 'medium', 'high', 'xhigh', 'max'])
    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: { model: 'claude-opus-5', reasoning_effort: 'high', provider: 'acp' },
        session: { autocompact_pct: 90 },
        dashboard: { language: scene.language, user_role: '', user_technical_level: '' },
      })
    }
    if (path === '/api/agents') {
      return json(route, {
        agents: [{ name: 'default', kiro_agent: 'kirocrew', description: 'Default crew agent' }],
        default_agent: 'default',
      })
    }
    if (path.startsWith('/api/agents/detail/')) return json(route, { name: 'kirocrew', model: 'claude-opus-5', skills: [] })
    if (path === '/api/agents/installed') return json(route, [])
    // LanguageProvider treats the boot payload as authoritative over the
    // localStorage fast-path, so a payload without `language` reverts the UI to
    // English mid-boot and silently turns a localised pass into an English one.
    if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '', language: scene.language })
    return handleBootRoute(route, path, { project: PROJECT, theme: 'light', fixedApi: FIXED_API })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))

  async function load(language, route = '/') {
    scene.language = language
    await page.addInitScript(([slot, lang]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'light')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-active-slot-chat', slot)
      localStorage.setItem('mc-lang', lang)
      localStorage.setItem('mc-yolo-ack', '1')
      // Pre-arm two filters so the chips under the search box render too.
      localStorage.setItem('mc-session-unread-only', '1')
      localStorage.setItem('mc-session-recent-only', '1')
    }, [SLOT, language])
    await page.goto(base + route, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  /**
   * Crop to the union of several locators' boxes, padded.
   *
   * `pad` may be a number or per-side: the Overview tiles are anchored on their
   * LABELS, so the value underneath needs extra room below.
   */
  async function shotUnion(locators, name, pad = 14) {
    const p = typeof pad === 'number'
      ? { top: pad, right: pad, bottom: pad, left: pad }
      : { top: 14, right: 14, bottom: 14, left: 14, ...pad }
    const boxes = []
    for (const l of locators) {
      const box = await l.boundingBox()
      if (box) boxes.push(box)
    }
    if (boxes.length === 0) throw new Error(`no visible box for ${name}`)
    const x = Math.min(...boxes.map(b => b.x)) - p.left
    const y = Math.min(...boxes.map(b => b.y)) - p.top
    const right = Math.max(...boxes.map(b => b.x + b.width)) + p.right
    const bottom = Math.max(...boxes.map(b => b.y + b.height)) + p.bottom
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x: Math.max(0, x),
        y: Math.max(0, y),
        width: Math.min(VIEW.width - Math.max(0, x), right - Math.max(0, x)),
        height: Math.min(VIEW.height - Math.max(0, y), bottom - Math.max(0, y)),
      },
    })
    console.log(`wrote ${name}.png`)
  }

  // ---- 1. session filter + sort menu -------------------------------------
  for (const lang of ['zh-CN', 'en']) {
    await load(lang)
    await page.locator(`button[title="${t(lang, 'pages.chatSidebar.sort_filter_sessions')}"]`).click()
    const menu = page.locator('[role="menu"]').first()
    await menu.waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(500)
    await shotUnion([menu], `${lang}-01-session-filter-sort`)
    await page.keyboard.press('Escape')
  }

  // ---- 2. reasoning-effort popover ---------------------------------------
  for (const lang of ['zh-CN', 'de']) {
    await load(lang)
    // The model pill's title is `Model: <name>`, still English on main — do not
    // key the locator off it beyond the prefix.
    await page.locator('button[title^="Model: "]').first().click()
    // The footer row is the only button carrying BOTH the "Reasoning" label and
    // the current level. Matching on the label alone is ambiguous in locales that
    // keep it in English (de renders `Reasoning` verbatim).
    const reasoningRow = page.getByRole('button')
      .filter({ hasText: t(lang, 'components.modelEffortDropdown.reasoning') })
      .filter({ hasText: t(lang, 'lib.effort.high') })
      .first()
    await reasoningRow.waitFor({ state: 'visible', timeout: 5000 })
    await reasoningRow.click()
    // `Slider` in components/ui.tsx is fully custom and the drill-in panel stays
    // mounted off-screen, so its box is not a reliable readiness signal. Anchor on
    // the heading, which only paints once the panel is in view.
    const heading = page.getByText(t(lang, 'components.reasoningEffortDropdown.effort'), { exact: true }).first()
    await heading.waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(900)
    // Union of the heading, the scale ends and the default toggle: the whole
    // popover body, without depending on its DOM nesting.
    await shotUnion([
      heading,
      page.getByText(t(lang, 'components.reasoningEffortDropdown.faster'), { exact: true }).first(),
      page.getByText(t(lang, 'components.reasoningEffortDropdown.smarter'), { exact: true }).first(),
      page.getByText(t(lang, 'components.reasoningEffortDropdown.use_configured_default'), { exact: true }).first(),
    ], `${lang}-02-effort-popover`, 18)
    await page.keyboard.press('Escape')
  }

  // ---- 3. Overview status tiles ------------------------------------------
  for (const lang of ['zh-CN', 'de', 'it']) {
    await load(lang, '/overview')
    const firstTile = page.getByText(t(lang, 'pages.overviewPage.stat_uptime'), { exact: true }).first()
    const lastTile = page.getByText(t(lang, 'pages.overviewPage.stat_lessons'), { exact: true }).first()
    await firstTile.waitFor({ state: 'visible', timeout: 8000 })
    await shotUnion([firstTile, lastTile], `${lang}-03-overview-tiles`, { top: 24, bottom: 58, left: 24, right: 24 })
  }

  await context.close()
  await browser.close()
  srv.close()
}

await main()
