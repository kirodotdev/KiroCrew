/**
 * Screenshots + assertions for the chat header status dot while a stop is
 * under way (#20397).
 *
 * Drives capture/chat-header-stop-dot.html: the REAL ChatPane (not frameless,
 * so the header row and its dot render) against fixed fake slot data. Two
 * scenes per theme — a turn working normally, and a stop under way — captured
 * so the PR shows the dot going from green to danger.
 *
 * Asserted, not assumed, per scene: the dot exists, and its computed
 * background colour equals --ok while working and --danger while stopping
 * (never the same colour as the other scene). An honest image: we assert the
 * DOM/colour BEFORE writing the PNG.
 *
 * Starts its own vite dev server on a loopback ephemeral port.
 *
 * Usage:
 *   node scripts/capture-20397-chat-header-stop-dot.mjs ../temp-screenshots/20397-chat-header-stop-dot
 */
import { chromium } from 'playwright'
import { spawn } from 'node:child_process'
import { mkdirSync, statSync } from 'node:fs'
import { once } from 'node:events'

const OUT = process.argv[2] || '../temp-screenshots/20397-chat-header-stop-dot'
const PORT = Number(process.argv[3]) || 6837
mkdirSync(OUT, { recursive: true })

/** Start vite on a FIXED loopback port and resolve once it answers HTTP.
 *  Readiness is a real fetch of the fixture, not stdout parsing (vite's banner
 *  format is not a contract and a missed match hangs forever). */
async function startVite() {
  const proc = spawn('npx', ['vite', '--host', '127.0.0.1', '--port', String(PORT), '--strictPort'], {
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const log = []
  proc.stdout.on('data', c => log.push(String(c)))
  proc.stderr.on('data', c => log.push(String(c)))
  const base = `http://127.0.0.1:${PORT}`
  const url = `${base}/capture/chat-header-stop-dot.html?theme=dark&state=running`
  const deadline = Date.now() + 90_000
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) throw new Error(`vite exited (${proc.exitCode}):\n${log.join('')}`)
    try {
      const res = await fetch(url)
      if (res.ok) return { proc, base }
    } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 300))
  }
  proc.kill('SIGKILL')
  throw new Error(`vite did not answer ${url} within 90s:\n${log.join('')}`)
}

/** Resolve a CSS var to its rgb() so a token name can be compared to a
 *  computed colour (which the browser always reports as rgb/rgba). */
async function tokenRgb(page, name) {
  return page.evaluate((v) => {
    const probe = document.createElement('span')
    probe.style.color = `var(${v})`
    document.body.appendChild(probe)
    const c = getComputedStyle(probe).color
    probe.remove()
    return c
  }, name)
}

const { proc, base } = await startVite()
const pageErrors = []
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const browser = await chromium.launch()
const SURFACES = [
  { surface: 'pane', dotTestId: 'chat-pane-status-dot', header: '[data-pane-title-row]' },
  { surface: 'embed', dotTestId: 'chat-embed-status-dot', header: '[data-capture-root] > div:first-child' },
]
try {
  for (const { surface, dotTestId, header } of SURFACES) {
    for (const theme of ['dark', 'light']) {
      const colors = {}
      for (const state of ['running', 'stopping', 'killing']) {
        const name = `${surface}-${state}-${theme}`
        const page = await browser.newPage({ viewport: { width: 820, height: 460 }, deviceScaleFactor: 2 })
        page.on('pageerror', e => pageErrors.push(String(e)))
        await page.goto(`${base}/capture/chat-header-stop-dot.html?surface=${surface}&theme=${theme}&state=${state}`, { waitUntil: 'networkidle' })
        const dot = page.getByTestId(dotTestId)
        await dot.waitFor()
        // Stop the pulse so the screenshot and the colour read are stable.
        await page.evaluate(() => Promise.all(document.getAnimations().map(a => { a.pause(); return Promise.resolve() })))
        const bg = await dot.evaluate(el => getComputedStyle(el).backgroundColor)
        colors[state] = bg
        const okRgb = await tokenRgb(page, '--ok')
        const dangerRgb = await tokenRgb(page, '--danger')
        if (state === 'running') {
          if (bg !== okRgb) fail(`${name}: dot bg ${bg} is not --ok ${okRgb}`)
          if (bg === dangerRgb) fail(`${name}: dot is danger while working`)
        } else {
          if (bg !== dangerRgb) fail(`${name}: dot bg ${bg} is not --danger ${dangerRgb}`)
          if (bg === okRgb) fail(`${name}: dot is still green while stopping`)
        }
        // Capture the header row (the dot + title + caption), the surface the
        // issue is about.
        const headerLoc = page.locator(header).first()
        const file = `${OUT}/${name}.png`
        await headerLoc.screenshot({ path: file })
        const bytes = statSync(file).size
        if (bytes < 1500) fail(`${name}: capture is only ${bytes} bytes`)
        console.log(`${name}: dot=${bg} captured ${bytes} bytes`)
        await page.close()
      }
      if (colors.running === colors.stopping) fail(`${surface}-${theme}: the dot colour did not change between working and stopping`)
      if (colors.running === colors.killing) fail(`${surface}-${theme}: the dot colour did not change between working and killing`)
    }
  }
} finally {
  await browser.close()
  proc.kill('SIGKILL')
  await once(proc, 'exit').catch(() => {})
}

if (pageErrors.length) { console.error('page errors:', pageErrors); failures++ }
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
// The vite child can keep the event loop alive past kill(); exit explicitly so
// the harness never hangs on teardown.
process.exit(0)
