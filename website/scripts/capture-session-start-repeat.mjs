/**
 * Screenshot harness for the error card after a session start failed twice.
 *
 * The transcript is the field report's own shape: the user's message, the
 * gateway's `session/new` timeout row, the `recovery` inject a Resume press
 * lands as, and the same timeout again. Both timeout rows carry the structural
 * `session_start_failed` kind `chat_runner` stamps from the exception. The
 * built SPA is served over `serveDist` with every /api call stubbed, so no live
 * gateway and no real double-timeout is needed; every frame is asserted from
 * the live DOM before it is written.
 *
 *   node scripts/capture-session-start-repeat.mjs <outDir> [--dist <dist>]
 *
 * Asserts: the newest error card carries the restart hint at body weight with
 * the command as a code chip and offers NO Resume; the older timeout row is
 * plain prose (no hint, no button); the composer beneath offers no Resume.
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
const OUT = args.find((a, i) => !FLAGS.has(a) && !FLAGS.has(args[i - 1])) || '../temp-screenshots/session-start-repeat'
const DIST = flag('--dist')
const SLOT = 'chat-session-start'
// Derived from this script's own location (scripts/ -> website/ -> repo root),
// never hardcoded: this path RENDERS into the captured screenshot.
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

// Byte for byte what `AcpRuntime._session_start_stalled` produces on a
// complete roster: the budget line plus `_mcp_init_progress`'s suffix.
const TIMEOUT_ROW = 'Request session/new timed out after 90s (4/4 session-injected MCP server(s) reported; the stall is later in session startup, not in those servers)'
const RESUME_ROW = '[Continue — requested by the user] The previous turn was interrupted before it finished and the user has asked you to carry on.'

mkdirSync(OUT, { recursive: true })
const now = Date.now() / 1000

const slotRow = {
  key: SLOT, title: 'Parser cleanup', running: false,
  last_message: TIMEOUT_ROW, messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 260, content: 'Rename getUserName to getUsername across the parser.', meta: { mid: 'u-1' } },
    { role: 'error', ts: now - 170, content: TIMEOUT_ROW, cls: 'msg msg-err', meta: { kind: 'session_start_failed', mid: 'e-1' } },
    { role: 'inject', ts: now - 165, content: RESUME_ROW, cls: 'msg msg-inject', meta: { injectKind: 'recovery', mid: 'i-1' } },
    { role: 'error', ts: now - 70, content: TIMEOUT_ROW, cls: 'msg msg-err', meta: { kind: 'session_start_failed', mid: 'e-2' } },
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
    const cards = page.locator('[data-testid="error-card"]')
    assert(`${theme}: both timeout rows render as error cards`, await cards.count() === 2)
    const older = cards.nth(0)
    const newest = cards.nth(1)
    assert(`${theme}: the older row is settled prose (no hint)`, await older.locator('[data-testid="error-card-session-start-repeat-hint"]').count() === 0)
    assert(`${theme}: the older row offers no Resume`, await older.locator('[data-testid="error-card-continue"]').count() === 0)
    assert(`${theme}: the newest row keeps the gateway's own sentence`, /timed out after 90s/.test(await newest.innerText()))
    const hint = newest.locator('[data-testid="error-card-session-start-repeat-hint"]')
    assert(`${theme}: the newest row carries the restart hint`, await hint.count() === 1)
    assert(`${theme}: the hint is not muted`, !/text-muted/.test((await hint.getAttribute('class')) || ''))
    const chip = newest.locator('[data-testid="error-card-restart-command"]')
    assert(`${theme}: the command is a code chip`, await chip.count() === 1 && (await chip.evaluate(el => el.tagName)) === 'CODE')
    console.log('hint:', JSON.stringify(await hint.innerText()))
    assert(`${theme}: the newest row offers NO Resume`, await newest.locator('[data-testid="error-card-continue"]').count() === 0)
    // The composer's Resume control is `composer-continue`; its "press Resume"
    // placeholder is the other half of the same promise, so both must be gone.
    assert(`${theme}: the composer offers no Resume either`, await page.locator('[data-testid="composer-continue"]').count() === 0)
    const placeholder = (await page.locator('textarea[data-composer-input]').getAttribute('placeholder')) || ''
    assert(`${theme}: the composer does not say "press Resume"`, !/resume/i.test(placeholder))
    await shot(`01-second-failure-card-${theme}`)
  }
  await h.close()
  if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
}

main().catch(err => { console.error(err); process.exit(1) })
