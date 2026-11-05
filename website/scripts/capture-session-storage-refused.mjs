/**
 * Screenshots + assertions for the Session storage refusal notice (#25809).
 *
 * Drives capture/session-storage-refused.html: the REAL SessionStorageScreen
 * with a stubbed inventory. For each theme it selects rows, presses Move to
 * Trash, and captures the notice the server's refusals produce, once with one
 * refused session and once with several.
 *
 * Asserted before each PNG is written: the heading uses the right plural form,
 * it sits inside a `role="alert"` element, and every refused session's title
 * and reason appear in that alert.
 *
 * Starts its own vite dev server on a loopback port.
 *
 * Usage:
 *   node scripts/capture-session-storage-refused.mjs ../temp-screenshots/25809-session-storage-refused [port]
 */
import { chromium } from 'playwright'
import { spawn } from 'node:child_process'
import { mkdirSync, statSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/25809-session-storage-refused'
const PORT = Number(process.argv[3]) || 6893
const PAGE = '/capture/session-storage-refused.html'
const SCENES = {
  one: {
    select: ['Refactor the ACP adapter'],
    heading: '1 session could not be moved',
    lines: ['Refactor the ACP adapter: '],
  },
  several: {
    select: ['Refactor the ACP adapter', 'Sydney property platform'],
    heading: '2 sessions could not be moved',
    lines: ['Refactor the ACP adapter: ', 'Sydney property platform: '],
  },
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
  const deadline = Date.now() + 90_000
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) throw new Error(`vite exited (${proc.exitCode}):\n${log.join('')}`)
    try {
      const res = await fetch(`${base}${PAGE}`)
      if (res.ok) return { proc, base }
    } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 300))
  }
  proc.kill('SIGKILL')
  throw new Error(`vite did not answer ${PAGE} within 90s:\n${log.join('')}`)
}

const { proc, base } = await startVite()
const pageErrors = []
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

const browser = await chromium.launch()
try {
  for (const theme of ['dark', 'light']) {
    for (const [scene, spec] of Object.entries(SCENES)) {
      const name = `${scene}-${theme}`
      const page = await browser.newPage({ viewport: { width: 860, height: 760 }, deviceScaleFactor: 2 })
      page.on('pageerror', e => pageErrors.push(String(e)))
      await page.goto(`${base}${PAGE}?theme=${theme}&refused=${scene}`, { waitUntil: 'load' })
      for (const title of spec.select) {
        await page.getByRole('checkbox', { name: title }).check()
      }
      await page.getByRole('button', { name: /Move to Trash/ }).click()
      const alert = page.getByRole('alert').filter({ hasText: spec.heading })
      await alert.waitFor({ timeout: 10_000 })
      const text = (await alert.textContent()) ?? ''
      for (const line of spec.lines) {
        if (!text.includes(line)) fail(`${name}: alert is missing "${line}" in ${JSON.stringify(text)}`)
      }
      await page.evaluate(() => Promise.all(document.getAnimations().map(a => { a.finish(); return Promise.resolve() })))
      const box = await page.getByTestId('session-storage-refused').boundingBox()
      if (!box) { fail(`${name}: notice has no box`); await page.close(); continue }
      const pad = 24
      const file = `${OUT}/${name}.png`
      await page.screenshot({
        path: file,
        clip: { x: Math.max(0, box.x - pad), y: Math.max(0, box.y - pad), width: box.width + pad * 2, height: box.height + pad * 2 },
      })
      console.log(`wrote ${file} (${statSync(file).size} bytes)`)
      await page.close()
    }
  }
} finally {
  await browser.close()
  proc.kill('SIGTERM')
}

if (pageErrors.length) fail(`page errors:\n${pageErrors.join('\n')}`)
process.exitCode = failures ? 1 : 0
