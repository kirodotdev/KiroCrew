/**
 * Screenshots + assertions for the session-switch boundary row (#9086).
 *
 * Drives capture/session-switch-notice.html: the REAL ChatPane against a fixed
 * conversation. Three scenes per theme: `before` (a reset leaves no row),
 * `after` (the row a mid-conversation model switch appends sits between the
 * two turns, drawn as a notice card, not as a reply) and `variants` (one row
 * per switch: agent, model, reasoning effort, workspace).
 *
 * Asserted before each PNG is written: each scene shows exactly its expected
 * notice cards, in order and with their exact text, and the before scene
 * shows none.
 *
 * Starts its own vite dev server on a loopback port.
 *
 * Usage:
 *   node scripts/capture-9086-session-switch-notice.mjs ../temp-screenshots/9086-session-switch-notice
 */
import { chromium } from 'playwright'
import { spawn } from 'node:child_process'
import { mkdirSync, statSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/9086-session-switch-notice'
const PORT = Number(process.argv[3]) || 6886
const NOTICE = 'Model switched to claude-opus-4.8. Replies from here on use the new model.'
const EXPECTED = {
  before: [],
  after: [NOTICE],
  variants: [
    '🔄 Switched to agent: kirocrew-worker',
    NOTICE,
    'Reasoning effort switched to high. Replies from here on use the new reasoning effort.',
    'Workspace switched to release-review. Replies from here on use the new workspace.',
  ],
}
mkdirSync(OUT, { recursive: true })

async function startVite() {
  const proc = spawn('npx', ['vite', '--host', '127.0.0.1', '--port', String(PORT), '--strictPort'], {
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const log = []
  proc.stdout.on('data', c => log.push(String(c)))
  proc.stderr.on('data', c => log.push(String(c)))
  const base = `http://127.0.0.1:${PORT}`
  const url = `${base}/capture/session-switch-notice.html`
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

const { proc, base } = await startVite()
const pageErrors = []
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const browser = await chromium.launch()
try {
  for (const theme of ['dark', 'light']) {
    for (const state of ['before', 'after', 'variants']) {
      const name = `${state}-${theme}`
      const page = await browser.newPage({ viewport: { width: 820, height: 900 }, deviceScaleFactor: 2 })
      page.on('pageerror', e => pageErrors.push(String(e)))
      await page.goto(`${base}/capture/session-switch-notice.html?theme=${theme}&state=${state}`, { waitUntil: 'load' })
      await page.getByText('Draft a reply to the changelog thread.').waitFor()
      await page.evaluate(() => Promise.all(document.getAnimations().map(a => { a.finish(); return Promise.resolve() })))
      const cards = page.getByTestId('notice-card-text')
      const count = await cards.count()
      const expected = EXPECTED[state]
      if (count !== expected.length) fail(`${name}: expected ${expected.length} notice card(s), saw ${count}`)
      else {
        for (let i = 0; i < count; i++) {
          const text = (await cards.nth(i).innerText()).trim()
          if (!text.endsWith(expected[i])) fail(`${name}: notice ${i} text is ${JSON.stringify(text)}`)
        }
      }
      // Crop to the conversation: from the first user row to the last reply.
      // The fixture serves no API, so the composer's config banners below it
      // are capture noise, not part of this change.
      const top = await page.getByText('Summarize the open review threads').boundingBox()
      const bottom = await page.getByText('Here is a draft:').boundingBox()
      const root = await page.locator('[data-capture-root]').boundingBox()
      if (!top || !bottom || !root) throw new Error(`${name}: transcript rows not laid out`)
      const y = Math.max(0, top.y - 24)
      const file = `${OUT}/${name}.png`
      await page.screenshot({
        path: file,
        clip: { x: root.x, y, width: root.width, height: bottom.y + bottom.height + 24 - y },
      })
      const bytes = statSync(file).size
      if (bytes < 1500) fail(`${name}: capture is only ${bytes} bytes`)
      console.log(`${name}: ${count} notice card(s), captured ${bytes} bytes`)
      await page.close()
    }
  }
} finally {
  await browser.close()
  proc.kill('SIGKILL')
}

if (pageErrors.length) { console.error('page errors:', pageErrors); failures++ }
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
process.exit(0)
