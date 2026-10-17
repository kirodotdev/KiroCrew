/**
 * Screenshot harness for the composer agent chip's agent-cycle hint.
 *
 * Runs the REAL built SPA (website/dist) behind `serveDist`, every /api/** call
 * answered from fixtures by `stubDashboardApi`. No gateway, no kiro-cli.
 *
 * The chip's tooltip gains a second line naming the next/previous agent chords.
 * A native `title` tooltip is drawn by the OS, not the page, so a headless
 * capture never paints it. Each frame therefore reads the chip's REAL `title`
 * attribute from the DOM and renders that exact text (line break included) in
 * a box beside the chip -- the same evidence technique the chip's
 * inherited-default capture uses. The text is never invented here; every frame
 * ASSERTS it before writing the PNG, so a stale bundle fails loudly.
 *
 *   1. pinned agent, Windows/Linux     -> "Agent: kirocrew" + Alt+Shift line
 *   2. pinned agent, macOS             -> "Agent: kirocrew" + Option glyph line
 *   3. inherited default, Windows/Linux -> long explanation + Alt+Shift line
 *   4. pinned agent, light theme       -> the same tooltip on the light palette
 *
 * `--verify-only` runs the assertions and writes nothing.
 *
 * Usage: node scripts/capture-agent-chip-cycle-hint.mjs [outDir] [--verify-only]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const args = process.argv.slice(2)
const VERIFY_ONLY = args.includes('--verify-only')
const OUT = args.find(a => !a.startsWith('--')) || '../temp-screenshots/agent-chip-cycle-hint'
if (!VERIFY_ONLY) mkdirSync(OUT, { recursive: true })

const DEFAULT_AGENT = 'kirocrew'
const NOW_ISO = new Date(Date.now() - 60_000).toISOString()
const INHERITED = 'chat-inherited'
const PINNED = 'chat-pinned'
const slots = [
  { key: INHERITED, title: 'Inherited default', messages: 4, running: false, agent: '', mode: '', tags: [], last_ts: NOW_ISO, last_turn_ts: NOW_ISO },
  { key: PINNED, title: 'Pinned to kirocrew', messages: 4, running: false, agent: 'kirocrew', mode: '', tags: [], last_ts: NOW_ISO, last_turn_ts: NOW_ISO },
]
const detailFor = agent => ({
  running: false, has_more: false, total: 1, queue: [],
  messages: [{ role: 'user', ts: Date.now() / 1000 - 300, content: 'hello' }],
  ...(agent ? { agent } : {}),
})

const PC_LINE = 'Next agent: Alt+Shift+A \u00b7 Previous: Alt+Shift+Z'
const MAC_LINE = 'Next agent: \u2325\u21e7A \u00b7 Previous: \u2325\u21e7Z'
const MAC_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const results = []

  const extra = async (path, route) => {
    if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/agents/catalog') {
      await json(route, { agents: [{ name: 'kirocrew', source: 'builtin' }, { name: 'oncall', source: 'aim' }], default_agent: DEFAULT_AGENT })
      return true
    }
    if (path.startsWith('/api/chat/slots/' + INHERITED)) { await json(route, detailFor('')); return true }
    if (path.startsWith('/api/chat/slots/' + PINNED)) { await json(route, detailFor('kirocrew')); return true }
    if (path.startsWith('/api/chat/slots/')) { await json(route, detailFor('')); return true }
    return false
  }

  async function frame(name, { slot, mac = false, theme = 'dark', wantFirst, wantSecond }) {
    const context = await browser.newContext({
      viewport: { width: 1400, height: 900 },
      deviceScaleFactor: 2,
      ...(mac ? { userAgent: MAC_UA } : {}),
    })
    const page = await context.newPage()
    logPageProblems(page)
    if (mac) {
      await page.addInitScript(() => { Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' }) })
    }
    await stubDashboardApi(page, { slots: slots.filter(s => s.key === slot), theme, extra })
    await page.routeWebSocket(/\/api\/ws/, () => {})
    await page.addInitScript(s => { localStorage.setItem('mc-active-slot', s) }, slot)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })

    // Poll the title until the default-agent fetch has resolved and the
    // expected first line is present; the final read decides pass/fail.
    const readTitle = () => page.evaluate(() => {
      const btn = document.querySelector('button svg.lucide-bot')?.closest('button')
      return btn ? { title: btn.getAttribute('title'), aks: btn.getAttribute('aria-keyshortcuts') } : null
    })
    let got = null
    for (let i = 0; i < 40; i++) {
      got = await readTitle()
      if (got?.title && wantFirst.test(got.title.split('\n')[0])) break
      await page.waitForTimeout(150)
    }
    const [first, second] = (got?.title ?? '').split('\n')
    const ok = !!got && wantFirst.test(first ?? '') && second === wantSecond
    results.push({ name, ok, title: got?.title ?? null, ariaKeyshortcuts: got?.aks ?? null })

    if (!VERIFY_ONLY && got?.title) {
      const box = await page.evaluate(() => {
        const btn = document.querySelector('button svg.lucide-bot')?.closest('button')
        const r = btn.getBoundingClientRect()
        return { x: r.x, y: r.y, w: r.width, h: r.height }
      })
      await page.locator('button:has(svg.lucide-bot)').first().hover()
      // Render the chip's real title text above the chip, as the OS tooltip would.
      await page.evaluate(({ t, b }) => {
        const d = document.createElement('div')
        d.textContent = t
        d.style.cssText = `position:fixed;left:${Math.max(8, b.x)}px;bottom:${window.innerHeight - b.y + 8}px;max-width:440px;`
          + 'white-space:pre-line;padding:6px 10px;background:var(--bg-elevated,#1b1e2b);color:var(--text,#e6e6e6);'
          + 'border:1px solid var(--border,#333);border-radius:6px;font:12px/1.45 system-ui,sans-serif;'
          + 'box-shadow:0 6px 24px rgba(0,0,0,.35);z-index:99999'
        document.body.appendChild(d)
      }, { t: got.title, b: box })
      await page.waitForTimeout(200)
      const top = Math.max(0, box.y - 150)
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: { x: Math.max(0, box.x - 40), y: top, width: 620, height: box.y + box.h + 30 - top },
      })
      console.log('wrote', `${OUT}/${name}.png`)
    }
    await context.close()
  }

  await frame('01-pinned-windows-linux-dark', { slot: PINNED, wantFirst: /^Agent: kirocrew$/, wantSecond: PC_LINE })
  await frame('02-pinned-macos-dark', { slot: PINNED, mac: true, wantFirst: /^Agent: kirocrew$/, wantSecond: MAC_LINE })
  await frame('03-inherited-default-windows-linux-dark', { slot: INHERITED, wantFirst: /follows the default agent/, wantSecond: PC_LINE })
  await frame('04-pinned-windows-linux-light', { slot: PINNED, theme: 'light', wantFirst: /^Agent: kirocrew$/, wantSecond: PC_LINE })

  await browser.close()
  srv.close()
  console.log('--- assertions ---')
  for (const r of results) console.log(JSON.stringify(r))
  if (!results.every(r => r.ok)) {
    console.error('FAIL: the agent chip did not carry the expected cycle hint')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
