/**
 * Screenshot harness for the error card a context overflow leaves.
 *
 * The transcript is the field report's shape: a user message, then the claude
 * adapter's "Input is too long" refusal as the gateway words it, stamped with
 * the structural `context_overflow` kind `chat_runner` writes from the
 * exception. The built SPA is served over `serveDist` with every /api call
 * stubbed, so no live gateway and no real overflow is needed; every frame is
 * asserted from the live DOM before it is written.
 *
 *   node scripts/capture-context-overflow.mjs <outDir> [--dist <dist>]
 *
 * Asserts: the card offers "Start a fresh session" and NO Resume, the hint says
 * what carries over, the composer beneath offers no Resume, and after the
 * press the card says to send the message again.
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')

const args = process.argv.slice(2)
const FLAGS = new Set(['--dist'])
const flag = name => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined }
const OUT = args.find((a, i) => !FLAGS.has(a) && !FLAGS.has(args[i - 1])) || '../temp-screenshots/context-overflow'
const DIST = flag('--dist')
const SLOT = 'chat-context-overflow'
// Derived from this script's own location (scripts/ -> website/ -> repo root),
// never hardcoded: this path RENDERS into the captured screenshot.
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

// What `_format_acp_error` produces for the claude adapter's frame.
const OVERFLOW_ROW = '❌ This chat is too big for the model to read in one request (Input is too long), often because it holds many images such as screenshots. Retrying or switching models will not help. Start a fresh session.'

mkdirSync(OUT, { recursive: true })
const now = Date.now() / 1000

const slotRow = {
  key: SLOT, title: 'Verify the pricing page', running: false,
  last_message: OVERFLOW_ROW, messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 400, content: 'Check every breakpoint of the pricing page and screenshot each one.', meta: { mid: 'u-1' } },
    { role: 'assistant', ts: now - 300, content: 'All 91 breakpoints checked. The table wraps cleanly from 320px up.', meta: { mid: 'a-1' } },
    { role: 'user', ts: now - 120, content: 'hi', meta: { mid: 'u-2' } },
    { role: 'error', ts: now - 110, content: OVERFLOW_ROW, cls: 'msg msg-err', meta: { kind: 'context_overflow', mid: 'e-1' } },
  ],
}

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots: [slotRow], detail,
    viewport: { width: 1400, height: 950 }, dist: DIST,
  })
  const { page } = h
  let failures = 0
  const assert = (label, ok) => {
    console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
    if (!ok) failures += 1
  }
  const shot = async name => {
    const file = `${OUT}/${name}.png`
    await page.screenshot({ path: file })
    console.log('wrote', file)
  }
  for (const theme of ['dark', 'light']) {
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1200 })
    const card = page.locator('[data-testid="error-card"][data-context-overflow="true"]')
    assert(`${theme}: the overflow row renders the fresh-session card`, await card.count() === 1)
    assert(`${theme}: the card keeps the gateway's sentence`, /Input is too long/.test(await card.innerText()))
    const hint = card.locator('[data-testid="error-card-fresh-session-hint"]')
    assert(`${theme}: the hint says the images are not re-read`, /instead of re-reading the images/.test(await hint.innerText()))
    assert(`${theme}: the card offers NO Resume`, await card.locator('[data-testid="error-card-continue"]').count() === 0)
    assert(`${theme}: the composer offers no Resume either`, await page.locator('[data-testid="composer-continue"]').count() === 0)
    const btn = card.locator('[data-testid="error-card-fresh-session"]')
    assert(`${theme}: the card offers Start a fresh session`, await btn.count() === 1)
    await shot(`01-overflow-card-${theme}`)
    await btn.click()
    await page.waitForTimeout(600)
    assert(`${theme}: after the press the button is gone`, await card.locator('[data-testid="error-card-fresh-session"]').count() === 0)
    assert(`${theme}: after the press the card says to send again`, /Type your next message below/.test(await hint.innerText()))
    assert(`${theme}: the recovered card drops the danger colours`, !/bg-danger-subtle/.test((await card.getAttribute('class')) || ''))
    assert(`${theme}: the recovered card drops the stale instruction`, !/Input is too long/.test(await card.innerText()))
    await shot(`02-fresh-session-ready-${theme}`)
  }
  await h.close()
  if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
}

main().catch(err => { console.error(err); process.exit(1) })
