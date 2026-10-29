/**
 * Screenshots + assertions for #20398: the Stop escalation has one name
 * ("force stop"), and a refused Stop is said in plain words.
 *
 * Drives capture/stop-copy-20398.html (the REAL ChatPane / ChatEmbed against
 * stubbed fetch). Each scene asserts its text BEFORE writing the PNG:
 *   - embed-armed: the armed Stop's inline hint says "force stop" and what
 *     stays and what goes, with no "kill" or "reset".
 *   - pane-killing: the status caption says "Force stopping…".
 *   - pane-refused-404: a refused Stop for a missing chat reads in plain
 *     words, with no raw backend text.
 *   - pane-refused-500: an unknown refusal keeps the backend text as a
 *     secondary line under a plain message.
 *
 * Starts its own vite dev server on a loopback port.
 *
 *   node scripts/capture-stop-copy-20398.mjs ../temp-screenshots/stop-copy-20398 [port]
 *
 * CAPTURE_VITE_CONFIG=<file> runs vite with that config instead (for a worktree
 * whose node_modules is a symlink outside vite's served root).
 */
import { chromium } from 'playwright'
import { spawn } from 'node:child_process'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/stop-copy-20398'
const PORT = Number(process.argv[3]) || 6839
mkdirSync(OUT, { recursive: true })

async function startVite() {
  const proc = spawn('npx', ['vite', ...(process.env.CAPTURE_VITE_CONFIG ? ['--config', process.env.CAPTURE_VITE_CONFIG] : []), '--host', '127.0.0.1', '--port', String(PORT), '--strictPort'], {
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const log = []
  proc.stdout.on('data', c => log.push(String(c)))
  proc.stderr.on('data', c => log.push(String(c)))
  const base = `http://127.0.0.1:${PORT}`
  const url = `${base}/capture/stop-copy-20398.html`
  const deadline = Date.now() + 120_000
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) throw new Error(`vite exited (${proc.exitCode}):\n${log.join('')}`)
    try {
      if ((await fetch(url)).ok) return { proc, base }
    } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 300))
  }
  proc.kill('SIGKILL')
  throw new Error(`vite did not answer ${url} within 120s:\n${log.join('')}`)
}

const { proc, base } = await startVite()
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }
const browser = await chromium.launch()
const open = async (query) => {
  const page = await browser.newPage({ viewport: { width: 820, height: 460 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => fail(`page error: ${e}`))
  await page.goto(`${base}/capture/stop-copy-20398.html?${query}`, { waitUntil: 'load', timeout: 120000 })
  await page.evaluate(() => document.getAnimations().forEach(a => a.pause()))
  return page
}

try {
  for (const theme of ['dark', 'light']) {
    {
      const page = await open(`surface=embed&state=soft_pending&theme=${theme}`)
      const hint = page.getByTestId('chat-embed-stop-hint')
      await hint.waitFor({ timeout: 30000 })
      const text = await hint.textContent()
      if (!/force stop/i.test(text) || /\bkill|\breset/i.test(text)) fail(`embed-armed: ${text}`)
      if (!/your messages stay; in-progress work and queued messages are discarded/.test(text)) fail(`embed-armed shape: ${text}`)
      await page.screenshot({ path: `${OUT}/embed-armed-${theme}.png` })
      console.log(`embed-armed-${theme}: ${text}`)
      await page.close()
    }
    {
      const page = await open(`surface=pane&state=killing&theme=${theme}`)
      const caption = page.getByTestId('chat-pane-status-caption')
      await caption.waitFor({ timeout: 30000 })
      const text = await caption.textContent()
      if (text !== 'Force stopping…') fail(`pane-killing caption: ${text}`)
      await page.screenshot({ path: `${OUT}/pane-killing-${theme}.png` })
      console.log(`pane-killing-${theme}: ${text}`)
      await page.close()
    }
    for (const code of ['404', '500']) {
      const page = await open(`surface=pane&state=idle&fail=${code}&theme=${theme}`)
      await page.getByTestId('stop-button-armed').click({ timeout: 30000 })
      const notice = page.getByTestId('chat-pane-stop-error')
      await notice.waitFor({ timeout: 15000 })
      const text = await notice.textContent()
      if (code === '404') {
        if (!text.includes("This chat wasn't found, so nothing was stopped. Reload the page.")) fail(`404: ${text}`)
        if (/not found\)|slot_not_found/.test(text)) fail(`404 shows raw text: ${text}`)
      } else {
        if (!text.includes("The server hit a problem and couldn't stop this turn. Press Stop again.")) fail(`500: ${text}`)
        if (!text.includes('turn worker crashed while cancelling')) fail(`500 lost its detail: ${text}`)
      }
      await page.screenshot({ path: `${OUT}/pane-refused-${code}-${theme}.png` })
      console.log(`pane-refused-${code}-${theme}: ${text}`)
      await page.close()
    }
  }
} catch (e) {
  fail(String(e))
} finally {
  await browser.close()
  proc.kill('SIGKILL')
}

if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
process.exit(0)
