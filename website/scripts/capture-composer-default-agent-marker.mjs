/**
 * Screenshot harness for the COMPOSER agent chip's inherited-default marker (#8770).
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * The chip names the agent a session runs under. An agent-less slot resolves the
 * CURRENT default (`kirocrew`) at run time, so the accurate chip label is
 * `kirocrew . default`; a slot explicitly pinned to `kirocrew` reads the bare
 * alias. Before this change BOTH read the bare `kirocrew`, so nothing on the chip
 * distinguished them -- that is the whole bug, so a single frame that shows the
 * two states side by side IS the before/after.
 *
 *   1. inherited-default slot, comfortable width -> chip reads `kirocrew . default`
 *   2. pinned slot, comfortable width            -> chip reads `kirocrew`
 *   3. inherited-default slot at a constrained composer width -> the marker
 *      truncates INSIDE the chip's own `max-w-[160px]` span; unlike the sidebar
 *      row it competes with no sibling tag chip, so nothing else is starved.
 *
 * Each frame ASSERTS the chip text before writing the PNG, so a capture that
 * silently photographs a stale bundle fails loudly instead of looking like
 * evidence. `--verify-only` runs the assertions and writes nothing.
 *
 * Usage: node scripts/capture-composer-default-agent-marker.mjs [outDir] [--verify-only]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const VERIFY_ONLY = args.includes('--verify-only')
const OUT = args.find(a => !a.startsWith('--')) || '../temp-screenshots/8770-composer-default-agent-marker'

if (!VERIFY_ONLY) mkdirSync(OUT, { recursive: true })

const DEFAULT_AGENT = 'kirocrew'
/** The marker's visible spelling, English catalog. U+00B7 MIDDLE DOT. */
const MARKED = `${DEFAULT_AGENT} \u00b7 default`

const NOW_ISO = new Date(Date.now() - 60_000).toISOString()

/** Two slots with the same resolved alias, opposite stored state. */
const INHERITED = 'chat-inherited'
const PINNED = 'chat-pinned'
const slots = [
  { key: INHERITED, title: 'Inherited default', messages: 4, running: false, agent: '', mode: '', tags: [], last_ts: NOW_ISO, last_turn_ts: NOW_ISO },
  { key: PINNED, title: 'Pinned to kirocrew', messages: 4, running: false, agent: 'kirocrew', mode: '', tags: [], last_ts: NOW_ISO, last_turn_ts: NOW_ISO },
]

const detailFor = (agent) => ({
  running: false, has_more: false, total: 1, queue: [], messages: [
    { role: 'user', ts: Date.now() / 1000 - 300, content: 'hello' },
  ], ...(agent ? { agent } : {}),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2,
  })

  const extra = async (path, route) => {
    // useAgents expects { agents, default_agent }; the shared stub returns a
    // bare array, which leaves defaultAgent '' and every chip reading 'default'.
    if (path === '/api/agents' || path === '/api/chat/agents') {
      await json(route, { agents: [{ name: 'kirocrew', source: 'builtin' }, { name: 'oncall', source: 'aim' }], default_agent: DEFAULT_AGENT })
      return true
    }
    if (path.startsWith('/api/chat/slots/' + INHERITED)) { await json(route, detailFor('')); return true }
    if (path.startsWith('/api/chat/slots/' + PINNED)) { await json(route, detailFor('kirocrew')); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detailFor('')); return true }
    return false
  }

  let page = null
  async function load(activeSlot, theme = 'dark') {
    if (page) await page.close()
    page = await context.newPage()
    logPageProblems(page)
    // Only the target slot is present, so the active/current slot is
    // unambiguous regardless of how the shell picks a default active slot.
    const only = slots.filter(s => s.key === activeSlot)
    await stubDashboardApi(page, { slots: only, theme, extra })
    await page.routeWebSocket(/\/api\/ws/, () => {})
    await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, activeSlot)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  /** The composer agent chip: the button carrying the Bot icon in the shelf. */
  async function chipText() {
    return page.evaluate(() => {
      const bot = document.querySelector('button svg.lucide-bot')
      const btn = bot && bot.closest('button')
      const span = btn && btn.querySelector('span')
      return span ? span.textContent : null
    })
  }

  async function chipBox() {
    return page.evaluate(() => {
      const bot = document.querySelector('button svg.lucide-bot')
      const btn = bot && bot.closest('button')
      if (!btn) return null
      const r = btn.getBoundingClientRect()
      return { x: r.x, y: r.y, w: r.width, h: r.height }
    })
  }

  const results = []
  async function frame(name, activeSlot, want, { narrow = false } = {}) {
    await load(activeSlot)
    if (narrow) {
      // Shrink the composer's max width so the chip's own span truncation shows.
      await page.addStyleTag({ content: ':root{--mc-input-width:420px}' })
      await page.waitForTimeout(400)
    }
    // Poll: the default-agent fetch (/api/agents) resolves asynchronously, so a
    // fast read can photograph the pre-default 'default' placeholder. Wait for
    // the expected text rather than a fixed timeout, so the capture never
    // records the wrong state (a stale-bundle/stale-state capture is worse than
    // none). Bounded; the final read below still decides pass/fail.
    let text = await chipText()
    for (let i = 0; i < 20 && !(text != null && (want instanceof RegExp ? want.test(text) : text.includes(want))); i++) {
      await page.waitForTimeout(150)
      text = await chipText()
    }
    const ok = text != null && (want instanceof RegExp ? want.test(text) : text.includes(want))
    results.push({ name, ok, text, want: String(want) })
    if (!VERIFY_ONLY) {
      const box = await chipBox()
      if (box) {
        const pad = 60
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x: Math.max(0, box.x - pad),
            y: Math.max(0, box.y - pad),
            width: Math.min(900, box.w + pad * 6),
            height: box.h + pad * 2,
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
      } else {
        await page.screenshot({ path: `${OUT}/${name}-MISSING.png` })
      }
    }
  }

  await frame('01-inherited-default-marked', INHERITED, MARKED)
  await frame('02-pinned-bare-alias', PINNED, /^kirocrew$/)
  await frame('03-inherited-narrow-truncates', INHERITED, DEFAULT_AGENT, { narrow: true })

  // 4. The inherited chip's explanatory tooltip. A native `title` does not
  //    screenshot reliably, so render the chip's REAL title attribute (read
  //    from the DOM, not invented) as a visible box beside the chip -- evidence
  //    of the on-demand text a hover or keyboard focus surfaces.
  await load(INHERITED)
  // Wait for the default-agent fetch to resolve so the title reflects the
  // inherited state, not the pre-default placeholder.
  for (let i = 0; i < 20; i++) {
    const t = await chipText()
    if (t && t.includes('default')) break
    await page.waitForTimeout(150)
  }
  const tipText = await page.evaluate(() => {
    const bot = document.querySelector('button svg.lucide-bot')
    const btn = bot && bot.closest('button')
    return btn ? btn.getAttribute('title') : null
  })
  results.push({ name: '04-inherited-tooltip', ok: !!(tipText && tipText.includes('follows the default')), text: tipText, want: 'follows the default' })
  if (!VERIFY_ONLY && tipText) {
    const box = await chipBox()
    if (box) {
      await page.evaluate((t) => {
        const d = document.createElement('div')
        d.textContent = t
        d.style.cssText = 'position:fixed;left:24px;top:24px;max-width:520px;padding:10px 14px;'
          + 'background:var(--bg-elevated,#1b1e2b);color:var(--text,#e6e6e6);border:1px solid var(--border,#333);'
          + 'border-radius:10px;font:13px/1.5 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.4);z-index:99999'
        d.setAttribute('data-tooltip-evidence', '1')
        document.body.appendChild(d)
      }, tipText)
      await page.waitForTimeout(200)
      await page.screenshot({
        path: `${OUT}/04-inherited-tooltip.png`,
        clip: { x: 0, y: 0, width: 700, height: Math.max(220, box.y + box.h + 40) },
      })
      console.log('wrote', `${OUT}/04-inherited-tooltip.png`)
    }
  }

  await browser.close()
  srv.close()

  console.log('--- assertions (the chip must distinguish inherited from pinned) ---')
  for (const r of results) console.log(JSON.stringify(r))
  if (!results.every(r => r.ok)) {
    console.error('FAIL: the composer chip did not render the expected label')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
