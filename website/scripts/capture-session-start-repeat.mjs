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
 * Asserts: the newest error card says why there is no Resume at body weight
 * and offers the shared Restart gateway button instead (#13791); the first
 * press only arms it, the second sends one stubbed `/api/restart` and the card
 * turns to its restarting line with the button gone; the hint names the cost
 * (everything running on the gateway); a refused (403) restart shows an
 * ErrorNotice with the server's sentence and no agent hand-off, and one line
 * addressed to whoever runs the gateway with the command and its copy
 * control, and drops the button; the endpoint's 409 refusal shows the notice
 * with its reason and the terminal hint, and drops the button; any other
 * failure (a 500) keeps the button with the cost line and the retry hint; the
 * restarting frame mutes the failure prose above it; the older timeout row is plain prose (no hint, no
 * button); the composer beneath offers no Resume. A real restart's reload ends
 * the flow, so there is no post-restart frame.
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
  // The restart the card's button sends (#13791). Registered after the
  // harness's catch-all, so it wins; counted so the frames can assert that the
  // arming press sends nothing and the second press sends exactly one.
  // `refuse` answers the way the owner gate does for a viewer who is not the
  // owner, for the refusal frames.
  let restarts = 0
  let refuse = false
  let serverError = false
  let crash = false
  await page.route('**/api/restart', route => {
    restarts += 1
    if (refuse) {
      return route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ error: 'Only the gateway owner can restart it.' }) })
    }
    if (serverError) {
      // The endpoint's own pre-restart refusal (`updates.py`), not invented copy:
      // it checks the interpreter before its 200 and answers 409 with its code.
      return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ ok: false, error: 'Cannot restart: invalid Python executable path', code: 'restart_interpreter_missing' }) })
    }
    if (crash) {
      // aiohttp's own answer to an unhandled handler exception.
      return route.fulfill({ status: 500, contentType: 'text/plain', body: '500 Internal Server Error\n\nServer got itself in trouble' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, status: 'restarting' }) })
  })
  // Each frame starts from a fresh page load, so every frame starts from a clean card.
  const freshLoad = async theme => {
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1200 })
  }
  for (const theme of ['dark', 'light']) {
    restarts = 0
    refuse = false
    await freshLoad(theme)
    const cards = page.locator('[data-testid="error-card"]')
    assert(`${theme}: both timeout rows render as error cards`, await cards.count() === 2)
    const older = cards.nth(0)
    const newest = cards.nth(1)
    assert(`${theme}: the older row is settled prose (no hint)`, await older.locator('[data-testid="error-card-session-start-restart-hint"], [data-testid="error-card-session-start-repeat-hint"]').count() === 0)
    assert(`${theme}: the older row offers no Resume and no restart`, await older.locator('[data-testid="error-card-continue"], [data-testid="error-card-restart-gateway"]').count() === 0)
    assert(`${theme}: the newest row keeps the gateway's own sentence`, /timed out after 90s/.test(await newest.innerText()))
    const hint = newest.locator('[data-testid="error-card-session-start-restart-hint"]')
    assert(`${theme}: the newest row says why there is no Resume`, await hint.count() === 1)
    assert(`${theme}: the hint is not muted`, !/text-muted/.test((await hint.getAttribute('class')) || ''))
    console.log('hint:', JSON.stringify(await hint.innerText()))
    assert(`${theme}: the hint names the cost (everything running on the gateway)`, /interrupts everything running on the gateway/.test(await hint.innerText()))
    const restart = newest.locator('[data-testid="error-card-restart-gateway"]')
    assert(`${theme}: the newest row offers the restart button`, await restart.count() === 1)
    assert(`${theme}: the newest row offers NO Resume`, await newest.locator('[data-testid="error-card-continue"]').count() === 0)
    // The composer's Resume control is `composer-continue`; its "press Resume"
    // placeholder is the other half of the same promise, so both must be gone.
    assert(`${theme}: the composer offers no Resume either`, await page.locator('[data-testid="composer-continue"]').count() === 0)
    const placeholder = (await page.locator('textarea[data-composer-input]').getAttribute('placeholder')) || ''
    assert(`${theme}: the composer does not say "press Resume"`, !/resume/i.test(placeholder))
    await shot(`01-second-failure-card-${theme}`)

    // The shared RestartGatewayButton arms on the first press: a restart
    // stops every running chat, so one click must not do it.
    await restart.click()
    await page.waitForTimeout(300)
    assert(`${theme}: the first press only arms`, /click again/i.test(await restart.innerText()))
    assert(`${theme}: arming sends nothing`, restarts === 0)
    await shot(`02-restart-armed-${theme}`)

    await restart.click()
    const restarting = newest.locator('[data-testid="error-card-restarting"]')
    await restarting.waitFor({ state: 'visible' })
    await page.waitForTimeout(400)
    assert(`${theme}: the second press sends exactly one restart`, restarts === 1)
    assert(`${theme}: the button is gone once the restart is under way`, await newest.locator('[data-testid="error-card-restart-gateway"]').count() === 0)
    assert(`${theme}: the failure prose is muted once the restart is accepted`, await newest.locator('[data-testid="error-card-session-start-prose"].text-muted').count() === 1)
    console.log('restarting:', JSON.stringify(await restarting.innerText()))
    await shot(`03-restarting-${theme}`)


    // A refusal (a viewer who is not the owner): ErrorNotice with the
    // server's sentence, the terminal hint back with its copy control, and
    // the button still there.
    refuse = true
    restarts = 0
    await freshLoad(theme)
    const card = page.locator('[data-testid="error-card"]').nth(1)
    const again = card.locator('[data-testid="error-card-restart-gateway"]')
    await again.click()
    await again.click()
    const notice = card.locator('[data-testid="error-card-restart-failed"]')
    await notice.waitFor({ state: 'visible' })
    await page.waitForTimeout(400)
    assert(`${theme}: the refusal is an alert carrying the server's sentence`, (await notice.getAttribute('role')) === 'alert' && /owner/i.test(await notice.innerText()))
    // No Ask-the-agent: that chat would need the same failing session start.
    assert(`${theme}: the refusal offers no agent hand-off`, await notice.getByRole('button').count() === 0)
    // One instruction, addressed to whoever runs the gateway: the command is
    // theirs, so the refused viewer is not also told to run it.
    const refusedHint = card.locator('[data-testid="error-card-restart-refused-hint"]')
    const refusedText = await refusedHint.innerText()
    assert(`${theme}: the refusal says who can restart, with the command they run`, /whoever runs this gateway/i.test(refusedText) && /kirocrew restart/.test(refusedText))
    assert(`${theme}: the refused viewer is not told to run the command`, await card.locator('[data-testid="error-card-session-start-repeat-hint"]').count() === 0)
    assert(`${theme}: the card explains itself once (no restart hint beside the refusal)`, await card.locator('[data-testid="error-card-session-start-restart-hint"]').count() === 0)
    const copy = card.locator('[data-testid="error-card-restart-command-copy"] button')
    assert(`${theme}: the command chip carries the copy control`, await copy.count() === 1)
    assert(`${theme}: a refused viewer is not offered a button that cannot work`, await card.locator('[data-testid="error-card-restart-gateway"]').count() === 0)
    await shot(`04-restart-refused-${theme}`)
    await copy.click()
    await page.waitForTimeout(300)
    const copied = (await copy.getAttribute('aria-label')) || ''
    console.log('copy outcome:', JSON.stringify(copied))
    assert(`${theme}: the copy control reports an outcome`, copied !== 'Copy command')
    await copy.hover()
    await page.waitForTimeout(300)
    await shot(`05-command-copy-outcome-${theme}`)

    // The endpoint's 409 for a restart that cannot run (a missing interpreter
    // here): the notice with its reason and the terminal hint. The button
    // goes, because pressing again meets the same refusal.
    refuse = false
    serverError = true
    await freshLoad(theme)
    const errCard = page.locator('[data-testid="error-card"]').nth(1)
    const press = errCard.locator('[data-testid="error-card-restart-gateway"]')
    await press.click()
    await press.click()
    await errCard.locator('[data-testid="error-card-restart-failed"]').waitFor({ state: 'visible' })
    await page.waitForTimeout(400)
    assert(`${theme}: a 409 refusal drops the button a retry cannot pass`, await press.count() === 0)
    assert(`${theme}: a 409 refusal is not the owner refusal (no owner line)`, await errCard.locator('[data-testid="error-card-restart-refused-hint"]').count() === 0)
    assert(`${theme}: a 409 refusal offers one next step, the terminal`, await errCard.locator('[data-testid="error-card-session-start-repeat-hint"]').count() === 1 && await errCard.locator('[data-testid="error-card-restart-retry-hint"]').count() === 0)
    assert(`${theme}: a 409 refusal leads with plain words, its reason below`, /did not restart[\s\S]*invalid Python executable path/i.test(await errCard.locator('[data-testid="error-card-restart-failed"]').innerText()))
    await shot(`06-restart-server-error-${theme}`)
    serverError = false

    // Any other failure (an unhandled 500 here): the notice, the cost line and
    // the retry hint, and the button stays, because a second press can work.
    crash = true
    await freshLoad(theme)
    const crashCard = page.locator('[data-testid="error-card"]').nth(1)
    const retry = crashCard.locator('[data-testid="error-card-restart-gateway"]')
    await retry.click()
    await retry.click()
    await crashCard.locator('[data-testid="error-card-restart-failed"]').waitFor({ state: 'visible' })
    await page.waitForTimeout(400)
    assert(`${theme}: a 500 keeps the retry`, await retry.isEnabled())
    assert(`${theme}: a 500 offers one next step beside the button (press again, or the terminal)`, await crashCard.locator('[data-testid="error-card-restart-retry-hint"]').count() === 1 && await crashCard.locator('[data-testid="error-card-session-start-repeat-hint"]').count() === 0)
    assert(`${theme}: the retry keeps the cost line, and says what survives`, /interrupts everything running on the gateway[\s\S]*can be resumed/.test(await crashCard.locator('[data-testid="error-card-session-start-restart-hint"]').innerText()))
    await shot(`07-restart-retry-${theme}`)
    crash = false
  }
  await h.close()
  if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
}

main().catch(err => { console.error(err); process.exit(1) })
