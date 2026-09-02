/**
 * Screenshot harness for the composer model picker's PER-AGENT DEFAULT row.
 *
 * The row is the last footer in the model popover and its whole job is to name
 * two things: WHICH model the click persists, and WHICH agent it persists it
 * for. It used to read "Set claude-opus-5 as default for default" — the trailing
 * word is the AGENT'S NAME, and because an agent can literally be called
 * `default`, the sentence never said what kind of default it sets. The fix says
 * "default MODEL", and renders both identifiers through <Trans> so each gets its
 * own `font-mono` span: prose follows the Font Family setting, verbatim
 * identifiers stay monospace, and the pair is what disambiguates the sentence
 * when the agent is named `default`.
 *
 * Two locales, because they are two different claims:
 *
 *   en  the wording fix and the two mono identifiers, one line.
 *   es  the stress case. "Establecer <model/> como modelo predeterminado para
 *       <agent/>" is ~63 characters against a popover hardcoded to WIDTH = 340.
 *       The label WRAPS rather than truncating: both identifiers sit at the
 *       sentence ends, so an ellipsis would eat exactly the agent name the row
 *       exists to name. The es shot is there to prove it degrades by wrapping
 *       and still shows the agent name.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call and the /api/ws websocket answered from
 * fixtures. No gateway, no dashboard token — the client code is unmodified, only
 * the network is stubbed, so the row lays out exactly as it does in production.
 *
 * The fixture is tuned to reach the row's ACTIONABLE branch, which needs all
 * four of these to hold at once:
 *
 *   /api/agents             one agent named `default` (so the sentence's two
 *                           identifiers collide, which is the bug)
 *   /api/agents/resolved-model
 *                           → claude-opus-5, with slot.model = '' — this is what
 *                           `_modelPinActive` resolves from
 *   /api/models             must LIST claude-opus-5 (wire key `model_name`), or
 *                           `displayModel` returns 'auto', `pinIsWithheld` goes
 *                           true and the row renders the "isn't offered right
 *                           now" branch instead
 *   /api/agents/installed   [] so the agent stores no pin, or `pinnedToAgent`
 *                           goes true and the row renders the already-pinned
 *                           branch instead
 *
 * Objective numbers are logged next to every shot, because the PNG cannot carry
 * them. This repo vendors NO font files: `index.html` pulls Space Grotesk and
 * JetBrains Mono from Google Fonts, so what the two chains resolve to depends on
 * the capture host. In the `mcr.microsoft.com/playwright:v1.58.2-noble` image
 * WITH network, the run reports:
 *
 *   prose (Font Family = system → --font-body)  Liberation Sans
 *     'system' asks for the PLATFORM sans (-apple-system / Segoe UI / sans-serif)
 *     and deliberately does not list Space Grotesk at all, so Linux resolves it
 *     to the installed Liberation Sans. That is correct behaviour, not a
 *     fallback failure.
 *   identifiers (font-mono → --mono)            JetBrains Mono, the real webfont
 *
 * Offline the mono chain degrades to the generic `monospace` face instead (in this
 * image, WenQuanYi Zen Hei Mono — the image installs Liberation only, so neither
 * JetBrains Mono nor DejaVu is present locally). The row still contrasts with the
 * prose either way, but the reported family name differs, which is why the log
 * prints the family Chromium actually rasterised (CDP
 * CSS.getPlatformFontsForNode) alongside the requested chain rather than
 * trusting either one alone.
 *
 * Usage: node scripts/capture-pin-row.mjs [outDir] [phase]
 *   phase is 'before' | 'after' and only names the files, so the same script can
 *   run unchanged in a throwaway worktree at the base commit.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pin-row-default-model'
const PHASE = process.argv[3] || 'after'
const SLOT = 'pin-row-default-model'
const PROJECT = '/home/user/.kiro/crew/workspace'
// The agent is named `default` on purpose: that collision with the English
// adjective is the entire defect this row's wording fixes.
const AGENT = 'default'
const MODEL = 'claude-opus-5'
const VIEW = { width: 1400, height: 900 }

mkdirSync(OUT, { recursive: true })

// `model: ''` is load-bearing: ChatPage only runs the resolved-model query for a
// slot carrying NO model of its own (`_slotAgentName`), and that query is what
// feeds the model id the row names.
const slots = [{
  key: SLOT,
  title: 'Which agent does this default belong to?',
  running: false,
  last_message: 'Opening the model picker.',
  messages: 2,
  agent: AGENT,
  memory_mode: 'persistent',
  project: PROJECT,
  model: '',
  reasoning_effort: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const now = Date.now() / 1000
const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  model: '',
  reasoning_effort: '',
  messages: [
    { role: 'user', ts: now - 120, content: 'Which agent does this default belong to?' },
    { role: 'assistant', ts: now - 100, content: 'Open the model picker and read its last row.' },
  ],
}

// Wire shape is `model_name` (AcpAdapter.fetchAvailableModels maps it to
// `name`); a fixture using `name` here silently yields an empty list.
const MODELS = [
  { model_name: 'auto', description: 'Let Kiro choose' },
  { model_name: MODEL, description: 'Most capable' },
  { model_name: 'claude-sonnet-4.6', description: 'Balanced' },
]

const scene = { language: 'en' }
const FIXED_API = makeFixedApi(PROJECT)

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEW,
    // The row is 12px. A 1x shot renders it too soft to read the two typefaces
    // apart, which is most of what these screenshots are for.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  // CDP is how the ACTUAL rasterised font is read. getComputedStyle only reports
  // the requested chain; with neither webfont vendored, the chain and the used
  // font are different facts and only the second one matches the PNG.
  const cdp = await context.newCDPSession(page)
  await cdp.send('DOM.enable')
  await cdp.send('CSS.enable')

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: { model: MODEL, reasoning_effort: '', provider: 'acp' },
        session: { autocompact_pct: 90 },
        dashboard: { language: scene.language, user_role: '', user_technical_level: '' },
      })
    }
    if (path === '/api/models') return json(route, MODELS)
    if (path.startsWith('/api/effort-levels')) return json(route, ['low', 'medium', 'high', 'xhigh', 'max'])
    // The model the row NAMES. ChatPage asks the backend rather than deriving the
    // four-tier precedence client-side, so this endpoint is the only source.
    if (path === '/api/agents/resolved-model') return json(route, { model: MODEL, agent: AGENT })
    if (path === '/api/agents') {
      return json(route, {
        agents: [{ name: AGENT, kiro_agent: 'kirocrew', description: 'Default crew agent' }],
        default_agent: AGENT,
      })
    }
    if (path.startsWith('/api/agents/detail/')) return json(route, { name: 'kirocrew', model: MODEL, skills: [] })
    // Empty: the agent stores NO pin of its own, so the row offers the write
    // instead of reporting an existing default.
    if (path === '/api/agents/installed') return json(route, [])
    // LanguageProvider treats this payload as authoritative over the
    // localStorage fast-path, so a payload without `language` reverts the whole
    // UI to English mid-boot and silently turns the es pass into a second en one.
    if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '', language: scene.language })
    return handleBootRoute(route, path, { project: PROJECT, theme: 'light', fixedApi: FIXED_API })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  async function load(language) {
    scene.language = language
    await page.addInitScript(([slot, lang]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'light')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', slot)
      localStorage.setItem('mc-active-slot-chat', slot)
      // Boot fast-path so the FIRST paint is already in-language.
      localStorage.setItem('mc-lang', lang)
      // The user's real setting. 'system' resolves --font-body to the platform
      // sans chain; `font-mono` still points at --mono, which is exactly the
      // split this row relies on.
      localStorage.setItem('mc-font-family', 'system')
      localStorage.setItem('mc-yolo-ack', '1')
    }, [SLOT, language])
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(3200)
    // index.html pulls Space Grotesk + JetBrains Mono from Google Fonts, and this
    // container HAS network — so the mono identifiers rasterise in the real
    // JetBrains Mono rather than a fallback. Wait for the swap to settle or the
    // measurements straddle two faces. (Offline, `--mono` degrades to the generic
    // `monospace` face instead; the row still contrasts with the prose, but the
    // reported family name changes, so it is worth knowing which run you got.)
    await page.evaluate(() => document.fonts.ready)
    await page.waitForTimeout(400)
  }

  /** Click the composer's model chip to open the picker.
   *
   *  Found by its first child span's text, not by `title`/`aria-label`: those are
   *  translated, and the title also flips to "Stop the current response…" while a
   *  turn is in flight, so a title selector silently misses on the es pass. */
  async function openPicker() {
    const opened = await page.evaluate(model => {
      const chip = [...document.querySelectorAll('button')].find(b => {
        const first = b.querySelector(':scope > span')
        return first && first.textContent.trim() === model
      })
      if (!chip) return false
      chip.click()
      return true
    }, MODEL)
    if (!opened) return false
    await page.waitForTimeout(900)
    // `aria-pressed` is on the pin row and on nothing else in the popover (the
    // model rows use role=option/aria-selected, the two other footers carry no
    // pressed state), and it is there on both the base and the fixed component.
    return (await page.locator('button[aria-pressed]').count()) > 0
  }

  /** Read the row's geometry and typography off the live DOM. */
  async function measure() {
    return page.evaluate(() => {
      const row = document.querySelector('button[aria-pressed]')
      if (!row) return null
      const popover = row.closest('.fixed')
      // The sentence wrapper: the row's first span, which is the flex item the
      // label lives in. Before the fix it is a bare <span>; after, it carries
      // min-w-0 / break-words and contains the two identifier spans.
      const sentence = row.querySelector(':scope > span')
      const idents = [...row.querySelectorAll('.font-mono')]

      // Tag the nodes so the CDP pass can find the same ones by selector.
      if (sentence) sentence.setAttribute('data-shot', 'sentence')
      idents.forEach((el, i) => el.setAttribute('data-shot', 'ident-' + i))

      const chain = el => (el ? getComputedStyle(el).fontFamily : '<absent>')
      const first = el => (el
        ? getComputedStyle(el).fontFamily.split(',')[0].trim().replace(/['"]/g, '')
        : '<absent>')

      // Visual line count. A Range over the span's contents yields one client
      // rect per inline box, and the span itself is a block-level flex item that
      // would report a single rect — so the Range is the right instrument.
      //
      // Counting DISTINCT TOPS is wrong here and silently doubles the answer:
      // the two identifier spans render in a different face from the prose, and
      // a taller/shorter face gives its inline box a different `top` ON THE SAME
      // LINE. Cluster by vertical OVERLAP instead — boxes sharing a line always
      // intersect vertically, boxes on different lines never do.
      let lines = 0
      let lineTops = []
      if (sentence) {
        const r = document.createRange()
        r.selectNodeContents(sentence)
        const rects = [...r.getClientRects()]
          .filter(x => x.width > 0 && x.height > 0)
          .sort((a, b) => a.top - b.top)
        const bands = []
        for (const rc of rects) {
          const band = bands[bands.length - 1]
          // 1px slack absorbs sub-pixel rounding between adjacent faces without
          // merging genuinely separate lines (line boxes here are ~19px apart).
          if (band && rc.top < band.bottom - 1) {
            band.bottom = Math.max(band.bottom, rc.bottom)
          } else {
            bands.push({ top: rc.top, bottom: rc.bottom })
          }
        }
        lines = bands.length
        lineTops = bands.map(b => Math.round(b.top))
      }
      // Independent second opinion on the same number, from box metrics rather
      // than from rect geometry. The two must agree.
      const lineHeightPx = sentence ? parseFloat(getComputedStyle(sentence).lineHeight) : 0
      const linesByHeight = (sentence && lineHeightPx)
        ? Math.round(sentence.offsetHeight / lineHeightPx)
        : 0

      return {
        text: (row.textContent || '').trim(),
        lines,
        linesByHeight,
        lineTops,
        lineHeightPx,
        row: {
          offsetHeight: row.offsetHeight,
          scrollWidth: row.scrollWidth,
          clientWidth: row.clientWidth,
          overflows: row.scrollWidth > row.clientWidth,
        },
        popover: popover ? {
          offsetWidth: popover.offsetWidth,
          scrollWidth: popover.scrollWidth,
          clientWidth: popover.clientWidth,
          overflows: popover.scrollWidth > popover.clientWidth,
        } : null,
        sentence: {
          present: !!sentence,
          className: sentence ? sentence.className : '<absent>',
          fontFirst: first(sentence),
          fontChain: chain(sentence),
          offsetHeight: sentence ? sentence.offsetHeight : 0,
          scrollWidth: sentence ? sentence.scrollWidth : 0,
          clientWidth: sentence ? sentence.clientWidth : 0,
        },
        identifiers: idents.map((el, i) => ({
          selector: 'ident-' + i,
          text: (el.textContent || '').trim(),
          fontFirst: first(el),
          fontChain: chain(el),
        })),
        bodyFontFirst: first(document.body),
        bodyFontChain: chain(document.body),
        // The Font Family setting's resolved value, published by useZoom.
        htmlFontFamilyAttr: document.documentElement.dataset.fontFamily || '<unset>',
      }
    })
  }

  /** The font Chromium actually rasterised for each tagged node.
   *
   *  ONE document handle, then SEQUENTIAL lookups. Every `DOM.getDocument` resets
   *  the node-id map, so a `Promise.all` over the tags has each call invalidating
   *  the ids the others are still holding — which surfaces as an arbitrary subset
   *  failing with "Could not find node" while its siblings succeed. */
  async function platformFonts(tags) {
    const out = {}
    let root
    try {
      root = (await cdp.send('DOM.getDocument', { depth: -1, pierce: true })).root
    } catch (e) {
      for (const t of tags) out[t] = `<cdp error: ${String(e).slice(0, 80)}>`
      return out
    }
    for (const tag of tags) {
      try {
        const { nodeId } = await cdp.send('DOM.querySelector', {
          nodeId: root.nodeId,
          selector: `[data-shot="${tag}"]`,
        })
        if (!nodeId) { out[tag] = '<node not found>'; continue }
        const { fonts } = await cdp.send('CSS.getPlatformFontsForNode', { nodeId })
        out[tag] = fonts.map(f => `${f.familyName}(${f.glyphCount})`).join(' + ') || '<none>'
      } catch (e) {
        out[tag] = `<cdp error: ${String(e).slice(0, 80)}>`
      }
    }
    return out
  }

  /** Tight crop over the popover's footer stack, clamped to the viewport. */
  async function cropFooter(name) {
    const box = await page.evaluate(() => {
      const row = document.querySelector('button[aria-pressed]')
      const popover = row?.closest('.fixed')
      if (!row || !popover) return null
      const r = row.getBoundingClientRect()
      const p = popover.getBoundingClientRect()
      // From just above the pin row down to the popover's bottom edge, so the
      // 340px container's own borders are in frame — that is what makes "no
      // horizontal overflow" visible rather than merely asserted.
      return {
        x: Math.round(p.left - 6),
        y: Math.round(r.top - 10),
        width: Math.round(p.width + 12),
        height: Math.round(p.bottom - r.top + 16),
      }
    })
    if (!box) {
      console.log('WARN: no pin row to crop for', name)
      return false
    }
    const x = Math.max(0, box.x)
    const y = Math.max(0, box.y)
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: {
        x, y,
        width: Math.min(VIEW.width - x, box.width),
        height: Math.min(VIEW.height - y, box.height),
      },
    })
    console.log('wrote', `${OUT}/${name}.png`)
    return true
  }

  let missing = 0
  for (const lang of ['en', 'es']) {
    await load(lang)
    if (!(await openPicker())) {
      // Explicitly NOT screenshotting a picker without the row: a shot of the
      // wrong branch would look like evidence and prove nothing.
      console.log(`FAIL: ${lang} — the per-agent default row did not render (model chip missing, or the fixture hit the withheld/already-pinned branch). No screenshot taken.`)
      missing++
      continue
    }
    const m = await measure()
    const fonts = await platformFonts(['sentence', ...(m?.identifiers || []).map(i => i.selector)])
    console.log(`\n===== ${lang} / ${PHASE} =====`)
    console.log('label text        :', JSON.stringify(m.text))
    console.log('rendered lines    :', m.lines, '(by rect bands)  /', m.linesByHeight,
      `(by offsetHeight ${m.sentence.offsetHeight} ÷ line-height ${m.lineHeightPx})`,
      m.lines === m.linesByHeight ? '— agree' : '— DISAGREE, treat both as suspect')
    console.log('line tops         :', JSON.stringify(m.lineTops))
    console.log('html[data-font-family]:', m.htmlFontFamilyAttr)
    console.log('row               :', JSON.stringify(m.row))
    console.log('popover           :', JSON.stringify(m.popover))
    console.log('sentence span     :', JSON.stringify(m.sentence))
    console.log('sentence rasterised:', fonts.sentence)
    if (!m.identifiers.length) {
      console.log('identifier spans  : NONE (.font-mono absent — this is the BEFORE shape)')
    }
    for (const id of m.identifiers) {
      console.log(`identifier ${id.selector} :`, JSON.stringify(id), '→ rasterised', fonts[id.selector])
    }
    console.log('body              :', m.bodyFontFirst, '|', m.bodyFontChain)
    await cropFooter(`pin-row-${lang}-${PHASE}`)
  }

  await browser.close()
  srv.close()
  if (missing) process.exitCode = 1
}

main().catch(err => { console.error(err); process.exit(1) })
