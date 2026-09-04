/**
 * Screenshot harness for the dropdown FILTER BOX typeface.
 *
 * Seven dropdown filter inputs hardcoded Tailwind `font-mono` (→ `var(--mono)`),
 * overriding the shared `Input` primitive's own `font-body` (components/ui.tsx).
 * Settings → Display → Font Family writes ONLY `--font-body` (hooks/useZoom.ts),
 * so `--mono` is a token that setting never touches — the filter boxes were
 * pinned to JetBrains Mono no matter what the user picked.
 *
 * This harness documents the two filter boxes reachable from the chat composer:
 *
 *   1. the MODEL picker's filter box   (components/ModelEffortDropdown.tsx)
 *   2. the AGENT picker's filter box   (pages/ChatPage.tsx, agent dropdown portal)
 *
 * and, as the deliberate CONTRAST, a model-list row's model NAME
 * (components/ModelDropdownList.tsx, `[data-model-name]`), which must STAY mono:
 * a model id is an identifier the user transcribes, not chrome prose. Without
 * that third read the evidence cannot distinguish "the filter box now follows
 * the setting" from "mono was swept out of the popover wholesale".
 *
 * THE COMPUTED STYLE IS THE PRIMARY DELIVERABLE, NOT THE PIXELS. Neither Space
 * Grotesk nor JetBrains Mono is vendored (zero font files in the tree) and the
 * capture container has no network, so both chains fall through to whatever the
 * image ships — body to a system sans, mono to a system mono. So the harness
 * does not ask the PNG to prove the change:
 *
 *   - it classifies each target's computed `font-family` against two live probe
 *     elements (`.font-body` and `.font-mono` rendered into the same document),
 *     which names the CSS TOKEN each element resolves through rather than
 *     eyeballing a chain string, and
 *   - it asks CDP `CSS.getPlatformFontsForNode` which typeface Chromium
 *     ACTUALLY rasterised for that node, which is the only read that survives
 *     the missing-font-files caveat.
 *
 * Booted with `mc-font-family = 'system'` — the setting the user actually has,
 * and the whole point of the change is that these boxes must follow it. 'system'
 * is also an EXPLICIT choice, so the CLI-mode auto-mono override in useZoom
 * (which only fires for the default 'sans') cannot confound the reads.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call and the /api/ws websocket answered from
 * fixtures. No gateway, no dashboard token — the client code is unmodified,
 * only the network is stubbed.
 *
 * Writes, where <tag> is `before` (base worktree) or `after` (fix worktree):
 *
 *   model-filter-<tag>.png              model popover: typed filter value + mono model names
 *   model-filter-placeholder-<tag>.png  model popover: untyped, placeholder prose visible
 *   agent-filter-<tag>.png              agent popover: typed filter value
 *
 * Usage: node scripts/capture-filter-input-font.mjs [outDir] [tag]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/filter-input-font'
const TAG = process.argv[3] || 'after'
const SLOT = 'filter-input-font'
const PROJECT = '/home/user/.kiro/crew/workspace'
const VIEW = { width: 1500, height: 1000 }

// The family under test. NOT the default: 'sans' would leave --font-body at its
// stylesheet value and a passing read could just mean "the stylesheet default
// happens to be sans". 'system' is a value only the SETTING can produce.
const FAMILY = 'system'

// The model the shelf advertises, and therefore the chip's `title`. The chip is
// the only way into the model popover, so this string is load-bearing.
const SHELF_MODEL = 'claude-opus-5'

// Filter fragments typed into each box. Chosen to leave rows on screen: a box
// filtered down to nothing crops to an empty popover and the model-name contrast
// read disappears with it.
const MODEL_FRAGMENT = 'claude'
const AGENT_FRAGMENT = 'rev'

mkdirSync(OUT, { recursive: true })

const MODELS = [
  { model_name: 'auto', description: 'Let Kiro choose' },
  { model_name: 'claude-opus-5', description: 'Most capable' },
  { model_name: 'claude-sonnet-4.6', description: 'Balanced' },
  { model_name: 'claude-haiku-4.6', description: 'Fastest' },
]

const AGENTS = [
  { name: 'default', kiro_agent: 'kirocrew', description: 'Default crew agent' },
  { name: 'reviewer', kiro_agent: 'kirocrew', description: 'Reads a diff and reports findings' },
  { name: 'researcher', kiro_agent: 'kirocrew', description: 'Gathers evidence before a decision' },
  { name: 'translator', kiro_agent: 'kirocrew', description: 'Keeps the locale catalogs in parity' },
]

const now = Date.now() / 1000

// `running: false` matters twice: a running turn DISABLES both shelf chips (so
// neither popover can be opened at all) and flips their title/aria-label to the
// "Stop the current response to switch …" wording the selectors key off.
const slots = [{
  key: SLOT,
  title: 'Should the filter boxes follow the font setting?',
  running: false,
  last_message: 'Reading the Input primitive.',
  messages: 2,
  agent: 'default',
  memory_mode: 'persistent',
  project: PROJECT,
  model: '',
  reasoning_effort: '',
  modified: Math.floor(now),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  model: '',
  reasoning_effort: '',
  messages: [
    { role: 'user', ts: now - 120, content: 'Do the dropdown filter boxes follow Settings → Display → Font Family?' },
    { role: 'assistant', ts: now - 60, content: 'They did not: each one hardcoded font-mono, and that resolves to --mono — a token the setting never writes.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEW,
    // 13px text; a 1x shot renders the typeface difference too soft to judge.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    // `provider: 'acp'` is what turns the agent chip on at all — onAgentClick is
    // wired only when provider.capabilities.agentTemplates is true, and acp is
    // the adapter that declares it (providers/adapters/acp.ts).
    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: { model: SHELF_MODEL, reasoning_effort: '', provider: 'acp' },
        session: { autocompact_pct: 90 },
        dashboard: { language: 'en', user_role: '', user_technical_level: '' },
      })
    }
    // Array shape, NOT the { models: [] } object in boot-api's fixed map: an
    // empty/non-array body makes acp.fetchAvailableModels() mark the list
    // degraded and serve auto-only, which empties the popover.
    if (path === '/api/models') return json(route, MODELS)
    if (path.startsWith('/api/effort-levels')) return json(route, ['low', 'medium', 'high', 'xhigh', 'max'])
    if (path === '/api/agents') return json(route, { agents: AGENTS, default_agent: 'default' })
    if (path.startsWith('/api/agents/detail/')) return json(route, { name: 'kirocrew', model: SHELF_MODEL, skills: [] })
    if (path === '/api/agents/installed') return json(route, AGENTS)
    if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '', language: 'en' })
    return handleBootRoute(route, path, { project: PROJECT, theme: 'light', fixedApi: makeFixedApi(PROJECT) })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  await page.addInitScript(([slot, fam]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-active-slot-chat', slot)
    localStorage.setItem('mc-lang', 'en')
    localStorage.setItem('mc-font-family', fam)
    localStorage.setItem('mc-yolo-ack', '1')
  }, [SLOT, FAMILY])

  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3200)

  // CDP is the only read that reports the typeface Chromium actually
  // rasterised, as opposed to the chain it was asked to try.
  const cdp = await context.newCDPSession(page)
  await cdp.send('DOM.enable')
  await cdp.send('CSS.enable')

  /** Typefaces actually used to raster `selector`'s text, per CDP. Empty when the
   *  node lays out no text of its own — an input showing only its placeholder
   *  renders that in a pseudo-element, so this reads {} until a value is typed. */
  async function platformFonts(selector) {
    try {
      const { root } = await cdp.send('DOM.getDocument', { depth: -1 })
      const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector })
      if (!nodeId) return '<node not found>'
      const { fonts } = await cdp.send('CSS.getPlatformFontsForNode', { nodeId })
      if (!fonts.length) return '<no text rasterised>'
      return fonts.map(f => `${f.familyName}${f.isCustomFont ? ' (webfont)' : ''} ×${f.glyphCount}`).join(' + ')
    } catch (err) {
      return `<cdp failed: ${String(err).slice(0, 80)}>`
    }
  }

  /**
   * Classify each target's computed font-family by comparing it to two probe
   * elements rendered into the SAME document — so the answer is the name of the
   * CSS token the element resolves through, not a chain string a reader has to
   * pattern-match by eye.
   */
  async function readFonts(targets) {
    return page.evaluate(sels => {
      const probe = cls => {
        const d = document.createElement('div')
        d.className = cls
        document.body.appendChild(d)
        const f = getComputedStyle(d).fontFamily
        d.remove()
        return f
      }
      const bodyChain = probe('font-body')
      const monoChain = probe('font-mono')
      const out = { _bodyChain: bodyChain, _monoChain: monoChain, _targets: {} }
      for (const [label, sel] of Object.entries(sels)) {
        const el = document.querySelector(sel)
        if (!el) { out._targets[label] = { selector: sel, resolves: '<ABSENT>' } ; continue }
        const chain = getComputedStyle(el).fontFamily
        out._targets[label] = {
          selector: sel,
          resolves: chain === bodyChain ? 'font-body  (--font-body)'
            : chain === monoChain ? 'font-mono  (--mono)'
            : 'OTHER (matches neither token)',
          first: chain.split(',')[0].replace(/['"]/g, '').trim(),
          chain,
          classAttr: el.getAttribute('class') || '',
          hasFontMonoClass: /(^|\s)font-mono(\s|$)/.test(el.getAttribute('class') || ''),
        }
      }
      return out
    }, targets)
  }

  /** Tight crop around a locator, padded and clamped to the viewport. `maxH`
   *  caps a tall popover so 13px text stays readable instead of being one row
   *  in a 600px image. */
  async function crop(name, box, { pad = 10, maxH = Infinity } = {}) {
    if (!box) { console.log('WARN: no box for', name, '— full page instead'); await page.screenshot({ path: `${OUT}/${name}.png` }); return }
    const x = Math.max(0, box.x - pad)
    const y = Math.max(0, box.y - pad)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x, y,
        width: Math.min(VIEW.width - x, box.width + pad * 2),
        height: Math.min(VIEW.height - y, box.height + pad * 2, maxH),
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** The popover root that owns `inputSel`, as a viewport rect. The popovers are
   *  portaled to document.body with `position: fixed`, so `.closest('.fixed')`
   *  from the input is the popover itself. */
  const popoverBox = inputSel => page.evaluate(sel => {
    const input = document.querySelector(sel)
    const pop = input?.closest('.fixed')
    if (!pop) return null
    const r = pop.getBoundingClientRect()
    const ir = input.getBoundingClientRect()
    return {
      x: Math.round(r.x), y: Math.round(r.y),
      width: Math.round(r.width), height: Math.round(r.height),
      // Distance from the popover top to the bottom of the filter box, so the
      // caller can cap the crop a fixed number of rows below it.
      toInputBottom: Math.round(ir.bottom - r.y),
    }
  }, inputSel)

  const report = []

  // ---------------------------------------------------------------- MODEL box
  // The shelf chip carries no aria-label, only `title` — "Model: <name>"
  // (components/chatInput.model_2). Keyed on the title PREFIX rather than the
  // model name so the selector does not depend on the fixture's model string.
  const modelChip = page.locator('button[title^="Model: "]').first()
  if (!(await modelChip.count())) throw new Error('model shelf chip not found (button[title^="Model: "])')
  await modelChip.click()
  await page.waitForTimeout(900) // framer-motion height spring + slide-up

  const MODEL_INPUT = 'input[aria-label="Filter models"]'
  const MODEL_NAME = '[data-model-name]'
  if (!(await page.locator(MODEL_INPUT).count())) throw new Error('model filter input not found (' + MODEL_INPUT + ')')

  // Untyped first: the placeholder is the prose whose typeface the change is
  // about, even though it renders at placeholder:text-muted/50 and is therefore
  // the harder of the two states to judge from a PNG.
  let box = await popoverBox(MODEL_INPUT)
  await crop(`model-filter-placeholder-${TAG}`, box, { maxH: (box?.toInputBottom ?? 60) + 230 })

  await page.locator(MODEL_INPUT).fill(MODEL_FRAGMENT)
  await page.waitForTimeout(700) // list re-filters, popover height re-springs
  box = await popoverBox(MODEL_INPUT)
  await crop(`model-filter-${TAG}`, box, { maxH: (box?.toInputBottom ?? 60) + 230 })

  const modelFonts = await readFonts({ modelFilterInput: MODEL_INPUT, modelListRowName: MODEL_NAME })
  report.push({
    surface: 'model popover',
    probes: { bodyChain: modelFonts._bodyChain, monoChain: modelFonts._monoChain },
    targets: modelFonts._targets,
    platform: {
      modelFilterInput: await platformFonts(MODEL_INPUT),
      modelListRowName: await platformFonts(MODEL_NAME),
    },
  })

  // Close before opening the agent popover: two `fixed` portals on screen at
  // once would make `.closest('.fixed')` ambiguous about which one to crop.
  await page.keyboard.press('Escape')
  await page.waitForTimeout(500)
  if (await page.locator(MODEL_INPUT).count()) {
    // Escape is not wired on every dropdown; fall back to an outside click,
    // which useFilteredDropdown's document listener treats as a close.
    await page.mouse.click(20, 20)
    await page.waitForTimeout(500)
  }

  // ---------------------------------------------------------------- AGENT box
  const agentChip = page.locator('button[aria-label^="Agent: "]').first()
  if (!(await agentChip.count())) throw new Error('agent shelf chip not found (button[aria-label^="Agent: "])')
  await agentChip.click()
  await page.waitForTimeout(800)

  const AGENT_INPUT = 'input[aria-label="Filter agents"]'
  if (!(await page.locator(AGENT_INPUT).count())) throw new Error('agent filter input not found (' + AGENT_INPUT + ')')

  await page.locator(AGENT_INPUT).fill(AGENT_FRAGMENT)
  await page.waitForTimeout(600)
  box = await popoverBox(AGENT_INPUT)
  await crop(`agent-filter-${TAG}`, box, { maxH: (box?.toInputBottom ?? 60) + 230 })

  const agentFonts = await readFonts({ agentFilterInput: AGENT_INPUT })
  report.push({
    surface: 'agent popover',
    probes: { bodyChain: agentFonts._bodyChain, monoChain: agentFonts._monoChain },
    targets: agentFonts._targets,
    platform: { agentFilterInput: await platformFonts(AGENT_INPUT) },
  })

  // -------------------------------------------------------------------- table
  console.log(`\n===== FILTER INPUT FONT — tag=${TAG}, mc-font-family=${FAMILY} =====`)
  for (const r of report) {
    console.log(`\n[${r.surface}]`)
    console.log('  probe .font-body →', r.probes.bodyChain)
    console.log('  probe .font-mono →', r.probes.monoChain)
    for (const [label, t] of Object.entries(r.targets)) {
      console.log(`  ${label}`)
      console.log(`     selector      ${t.selector}`)
      console.log(`     resolves      ${t.resolves}`)
      console.log(`     first entry   ${t.first}`)
      console.log(`     font-mono cls ${t.hasFontMonoClass}`)
      console.log(`     rastered      ${r.platform[label] ?? '<not read>'}`)
    }
  }
  console.log(`\n===== END tag=${TAG} =====\n`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
