/**
 * Screenshots + assertions for the message reaction row (#22482).
 *
 * Drives capture/message-reactions.html: the REAL UserMessage/AssistantMessage
 * rows against fixed local state (no gateway). Two scenes per theme — idle
 * (row hidden, nothing painted by default) and active (a reaction already
 * placed, stays visible without hover) — so the PR shows both the "nothing
 * extra most of the time" default and the marker use case the issue asks for.
 *
 * A `hover` scene was tried and dropped: this harness's synthetic
 * `page.mouse.move` does not register CSS `:hover` on the pre-existing
 * `data-message-actions` row either (confirmed against main, same opacity-0
 * result), so it is an environment limitation of this isolated capture page,
 * not something to assert about the reaction row specifically. The real hover
 * reveal is exercised by jsdom-based `fireEvent`/class-presence tests instead
 * (`MessageReactions.wiring.test.tsx`), which do not depend on the browser's
 * live hover state.
 *
 * Asserted, not assumed, per scene: two inline toggles plus one overflow
 * trigger render (staying under max-two-buttons-per-row), an active inline
 * emoji carries aria-pressed="true", and an idle row's controls are not painted
 * (opacity 0).
 *
 * Starts its own vite dev server on a loopback ephemeral port.
 *
 * Usage:
 *   node scripts/capture-22482-message-reactions.mjs ../temp-screenshots/22482-message-reactions
 */
import { chromium } from 'playwright'
import { spawn } from 'node:child_process'
import { mkdirSync, statSync } from 'node:fs'
import { once } from 'node:events'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const OUT = process.argv[2] || '../temp-screenshots/22482-message-reactions'
const PORT = Number(process.argv[3]) || 6841
mkdirSync(OUT, { recursive: true })

const BASE = `http://127.0.0.1:${PORT}`
const READY_URL = `${BASE}/capture/message-reactions.html?theme=dark&scene=idle`
const BOOT_TIMEOUT_MS = 90_000
const POLL_INTERVAL_MS = 300

const sleep = ms => new Promise(r => setTimeout(r, ms))

const WEBSITE = fileURLToPath(new URL('..', import.meta.url))
const VITE = join(WEBSITE, 'node_modules', 'vite', 'bin', 'vite.js')

/** Spawn the vite dev server, capturing its combined output for error text.
 *  Vite's CLI runs directly under this Node, so `proc` IS the server: started
 *  through `npx`, `proc` would be the launcher, and killing it would leave the
 *  server running on the capture port after the script ends. */
function spawnVite() {
  const proc = spawn(process.execPath, [VITE, '--host', '127.0.0.1', '--port', String(PORT), '--strictPort'], {
    cwd: WEBSITE,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const chunks = []
  const collect = c => chunks.push(String(c))
  proc.stdout.on('data', collect)
  proc.stderr.on('data', collect)
  return { proc, readLog: () => chunks.join('') }
}

/** Poll READY_URL until it answers 200, the server exits, or the boot budget
 *  elapses — whichever comes first. */
async function waitForReady(proc, readLog) {
  for (let elapsed = 0; elapsed < BOOT_TIMEOUT_MS; elapsed += POLL_INTERVAL_MS) {
    if (proc.exitCode !== null) throw new Error(`vite exited (${proc.exitCode}):\n${readLog()}`)
    try {
      if ((await fetch(READY_URL)).ok) return
    } catch { /* server not listening yet */ }
    await sleep(POLL_INTERVAL_MS)
  }
  throw new Error(`vite did not answer ${READY_URL} within ${BOOT_TIMEOUT_MS}ms:\n${readLog()}`)
}

/** Stop the server and wait for it to exit. Safe after it already exited. */
async function stopVite(proc) {
  if (proc.exitCode !== null || proc.signalCode !== null) return
  const exited = once(proc, 'exit')
  proc.kill('SIGKILL')
  await exited
}

const base = BASE
const pageErrors = []
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures++ }

// One cleanup scope covers server boot, browser launch and every capture, so
// the server is stopped however the run ends.
const { proc, readLog } = spawnVite()
let browser = null
try {
  await waitForReady(proc, readLog)
  browser = await chromium.launch()
  // `shown` counts buttons actually PAINTED (opacity > 0). Every button keeps
  // its slot in the row (reserved space, no layout shift); an unplaced one is
  // transparent at rest and opaque on hover, so opacity — not display — is what
  // distinguishes the states.
  const shownCount = async locator => locator.evaluateAll(
    els => els.filter(el => Number(getComputedStyle(el).opacity) > 0).length,
  )

  for (const theme of ['dark', 'light']) {
    for (const scene of ['idle', 'active', 'hover', 'error', 'error-limit', 'error-remove', 'load-failed', 'menu-open']) {
      const name = `${scene}-${theme}`
      const page = await browser.newPage({ viewport: { width: 820, height: 420 }, deviceScaleFactor: 2 })
      page.on('pageerror', e => pageErrors.push(String(e)))
      await page.goto(`${base}/capture/message-reactions.html?theme=${theme}&scene=${scene}`, { waitUntil: 'networkidle' })
      const root = page.locator('[data-capture-root]')
      await root.waitFor()

      const bars = page.getByTestId('message-reaction-bar')
      const barCount = await bars.count()
      if (barCount !== 2) fail(`${name}: expected 2 reaction bars (user + assistant), found ${barCount}`)

      const assistantBar = bars.nth(1)

      if (scene === 'menu-open') {
        // Open the assistant reply's add menu so the capture shows the fixed
        // set with a check on the placed one (the UX review's evidence gap).
        await assistantBar.getByTestId('reaction-add-trigger').click()
        await page.getByTestId('reaction-menu-item').first().waitFor()
      }
      // Placed emoji render as always-visible chips (the marker); the single
      // add `+` trigger (one control, under max-two-buttons-per-row) reveals on
      // hover. `chips` counts PLACED chips; `addTrigger` is the add control.
      const chips = assistantBar.getByTestId('reaction-chip')
      const addTrigger = assistantBar.getByTestId('reaction-add-trigger')

      if (scene === 'idle') {
        // No reaction: no chips, and the add trigger reserves its slot but
        // paints nothing at rest (transparent), like the action strip.
        if ((await chips.count()) !== 0) fail(`${name}: idle row should have no placed chips, found ${await chips.count()}`)
        const addShown = await shownCount(addTrigger)
        if (addShown !== 0) fail(`${name}: idle add trigger should paint 0, painted ${addShown}`)
      }
      if (scene === 'active') {
        // 👍 placed on the assistant reply shows as a marker, opaque at rest,
        // drawing the emoji itself (not a bare +). The markers are display-only
        // (span, not button), so removal lives in the + menu.
        if ((await chips.count()) !== 1) fail(`${name}: a reacted reply should show exactly one placed marker, found ${await chips.count()}`)
        const shown = await shownCount(chips)
        if (shown !== 1) fail(`${name}: the placed marker should be painted at rest, painted ${shown}`)
        const emoji = await chips.nth(0).getAttribute('data-emoji')
        if (emoji !== '👍') fail(`${name}: the placed marker should draw 👍, got ${emoji}`)
        // With a reaction placed the `+` stays visible at rest: the markers are
        // not clickable, so it is the only visible way to remove one.
        const addShown = await shownCount(addTrigger)
        if (addShown !== 1) fail(`${name}: a reacted row should keep the add control visible at rest, painted ${addShown}`)
      }
      if (scene === 'hover') {
        // The add trigger reveals on hover so a first reaction is reachable.
        const addShown = await shownCount(addTrigger)
        if (addShown !== 1) fail(`${name}: hover should reveal the add trigger, painted ${addShown}`)
      }
      if (scene === 'error' || scene === 'error-limit' || scene === 'error-remove') {
        // Every per-message failure keeps the `+` control visible at rest, so
        // the retry path is reachable without hovering.
        const addShown = await shownCount(addTrigger)
        if (addShown !== 1) fail(`${name}: error row should keep the add control visible, painted ${addShown}`)
      }
      if (scene === 'error') {
        // The add-failure notice names the emoji.
        const notice = assistantBar.getByText('Could not add 👍. Open the reactions button and choose it again.')
        if ((await notice.count()) !== 1) fail(`${name}: expected the emoji-named error notice, found ${await notice.count()}`)
      }
      if (scene === 'error-limit') {
        // The per-chat limit notice states the cap and that the reactions to
        // remove may be on any message.
        const notice = assistantBar.getByText('This chat has reached its limit of 500 reactions, counting every message, including ones scrolled out of view. Remove one to add another.')
        if ((await notice.count()) !== 1) fail(`${name}: expected the reaction-limit notice, found ${await notice.count()}`)
      }
      if (scene === 'error-remove') {
        // The remove-failure notice names the emoji and the menu entry that
        // retries it; the 👍 it could not remove is still placed.
        const notice = assistantBar.getByText('Could not remove 👍. Open the reactions button and choose Remove 👍 reaction again.')
        if ((await notice.count()) !== 1) fail(`${name}: expected the remove-failure notice, found ${await notice.count()}`)
        const emoji = await chips.nth(0).getAttribute('data-emoji')
        if (emoji !== '👍') fail(`${name}: the 👍 the remove failed on should still be placed, got ${emoji}`)
      }
      if (scene === 'load-failed') {
        // The chat-level notice sits above the transcript rows, outside every
        // message's reaction bar.
        const notice = page.getByText('Could not load reactions for this chat. Retrying automatically.')
        if ((await notice.count()) !== 1) fail(`${name}: expected the chat-level load-failed notice, found ${await notice.count()}`)
        if ((await bars.nth(0).getByText('Could not load reactions for this chat. Retrying automatically.').count()) !== 0) {
          fail(`${name}: the load-failed notice must be chat-level, not inside a message's bar`)
        }
      }
      if (scene === 'menu-open') {
        // The add menu open: every emoji as a row, with a check on placed ones.
        const items = assistantBar.page().getByTestId('reaction-menu-item')
        if ((await items.count()) !== 4) fail(`${name}: open menu should list all 4 emoji, found ${await items.count()}`)
      }

      const file = `${OUT}/${name}.png`
      await root.screenshot({ path: file })
      const bytes = statSync(file).size
      if (bytes < 1500) fail(`${name}: capture is only ${bytes} bytes`)
      console.log(`${name}: captured ${bytes} bytes`)
      await page.close()
    }
  }
} finally {
  if (browser) await browser.close()
  await stopVite(proc)
}

if (pageErrors.length) { console.error('page errors:', pageErrors); failures++ }
if (failures) { console.error(`${failures} assertion failure(s)`); process.exit(1) }
console.log('ALL GREEN')
process.exit(0)
