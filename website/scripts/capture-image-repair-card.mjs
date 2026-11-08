/**
 * Screenshot harness for the error card the many-image 2000px wedge leaves.
 *
 * The transcript carries the gateway's worded many-image rejection as an
 * `error` row stamped with the `image_repair` kind (what `chat_runner` writes
 * from the tagged exception). The built SPA is served over `serveDist` with
 * every /api call stubbed, so no live gateway and no real wedge is needed; the
 * REAL themed card renders (its `bg-danger-subtle` / `bg-bg-elevated` classes),
 * and each state is driven by clicking the real button with the repair endpoint
 * stubbed to the response that produces it.
 *
 *   node scripts/capture-image-repair-card.mjs <outDir> [--dist <dist>]
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
const OUT = args.find((a, i) => !FLAGS.has(a) && !FLAGS.has(args[i - 1])) || '../temp-screenshots/image-repair-card'
const DIST = flag('--dist')
const SLOT = 'chat-image-repair'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

// What `_format_acp_error` produces for the many-image dimension frame.
const WEDGE_ROW = "❌ An image already in this conversation is larger than the model's 2000-pixel limit for conversations with many images, so the whole turn was refused. Retrying will not help. Recover by repairing the oversized images in place -- downscaling them in the stored conversation -- which loses no turns, unlike starting a fresh session. (request_id: b209a047)"

mkdirSync(OUT, { recursive: true })
const now = Date.now() / 1000

const slotRow = {
  key: SLOT, title: 'Review the dashboard screenshots', running: false,
  last_message: WEDGE_ROW, messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 400, content: 'Here are the ten dashboard screenshots — walk through each one.', meta: { mid: 'u-1' } },
    { role: 'assistant', ts: now - 300, content: 'Looked at all ten; the layout holds from 320px up.', meta: { mid: 'a-1' } },
    { role: 'user', ts: now - 120, content: 'now compare them side by side', meta: { mid: 'u-2' } },
    { role: 'error', ts: now - 110, content: WEDGE_ROW, cls: 'msg msg-err', meta: { kind: 'image_repair', mid: 'e-1' } },
  ],
}

// The repair endpoint's next response, swapped per state before each click.
let repairResponse = { status: 200, body: { slot: SLOT, repaired: true, resumed: true } }
// Milliseconds to hold the repair response, so the "Repairing images…" pending
// state can be screenshotted before it resolves. Reset to 0 after each use.
let repairDelayMs = 0
// The fresh-session (reset-conversation) endpoint stands in for the way out the
// failed card offers. Held open with a delay so the "Starting…" pending state
// and then the done state can each be screenshotted.
let resetDelayMs = 0

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots: [slotRow], detail,
    viewport: { width: 1400, height: 950 }, dist: DIST,
  })
  const { page } = h
  // Stub the repair endpoint; its response is whatever `repairResponse` holds
  // when the button is pressed. Registered before load so the first click hits it.
  await page.route('**/api/chat/slots/*/repair-images', async route => {
    if (repairDelayMs) await new Promise(r => setTimeout(r, repairDelayMs))
    return route.fulfill({ status: repairResponse.status, contentType: 'application/json', body: JSON.stringify(repairResponse.body) })
  })
  // Stub the fresh-session (reset-conversation) endpoint the failed card's way
  // out calls, so its pending/done states can be captured without a real reset.
  await page.route('**/api/chat/slots/*/reset-conversation', async route => {
    if (resetDelayMs) await new Promise(r => setTimeout(r, resetDelayMs))
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slot: SLOT, reset: true, replay: true }) })
  })

  let failures = 0
  const assert = (label, ok) => { console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`); if (!ok) failures += 1 }
  const shot = async name => { const f = `${OUT}/${name}.png`; await page.screenshot({ path: f }); console.log('wrote', f) }

  for (const theme of ['dark', 'light']) {
    // idle
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1200 })
    const card = page.locator('[data-testid="error-card"][data-image-repair="true"]')
    assert(`${theme}: the wedge row renders the repair card`, await card.count() === 1)
    const btn = card.locator('[data-testid="error-card-repair-images"]')
    assert(`${theme}: the card offers Repair images and resume`, await btn.count() === 1)
    assert(`${theme}: the card carries danger colours when idle`, /bg-danger-subtle/.test((await card.getAttribute('class')) || ''))
    await shot(`01-idle-${theme}`)

    // pending: hold the response so the "Repairing images…" spinner is on screen.
    repairResponse = { status: 200, body: { slot: SLOT, repaired: true, resumed: true } }
    repairDelayMs = 4000
    await btn.click()
    await page.waitForTimeout(500)
    assert(`${theme}: pending disables the button`, await btn.isDisabled())
    await shot(`02-pending-${theme}`)
    await page.waitForTimeout(4000)  // let the held response resolve
    repairDelayMs = 0

    // done (resumed): reload idle, stub repaired+resumed, click, settle
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1000 })
    repairResponse = { status: 200, body: { slot: SLOT, repaired: true, resumed: true } }
    await card.locator('[data-testid="error-card-repair-images"]').click(); await page.waitForTimeout(700)
    assert(`${theme}: done drops the Repair button`, await card.locator('[data-testid="error-card-repair-images"]').count() === 0)
    assert(`${theme}: done drops the danger colours`, !/bg-danger-subtle/.test((await card.getAttribute('class')) || ''))
    await shot(`03-done-${theme}`)

    // done_not_resumed: reload idle, stub repaired+not-resumed
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1000 })
    repairResponse = { status: 200, body: { slot: SLOT, repaired: true, resumed: false } }
    await card.locator('[data-testid="error-card-repair-images"]').click(); await page.waitForTimeout(700)
    assert(`${theme}: not-resumed keeps a Resume affordance`, await card.locator('[data-testid="error-card-repair-images-resume"]').count() === 1)
    await shot(`04-done-not-resumed-${theme}`)

    // failed (backup exists): reload idle, stub 409 image_repair_backup_exists
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1000 })
    repairResponse = { status: 409, body: { slot: SLOT, code: 'image_repair_backup_exists', error: 'a .pre-image-repair.bak sidecar already exists; move it aside first.' } }
    await card.locator('[data-testid="error-card-repair-images"]').click(); await page.waitForTimeout(700)
    assert(`${theme}: failed drops the Repair button`, await card.locator('[data-testid="error-card-repair-images"]').count() === 0)
    assert(`${theme}: failed offers a fresh session`, await card.locator('[data-testid="error-card-repair-images-fresh-session"]').count() === 1)
    assert(`${theme}: failed shows the single plain-language reason line`, await card.locator('[data-testid="error-card-repair-images-hint"]').count() === 1)
    await shot(`05-failed-${theme}`)

    // failed -> fresh-session PENDING: the failed card is on screen with its
    // "Start a fresh session" way out. Hold the reset-conversation response open
    // so the button's "Starting…" pending state can be captured, then let it
    // resolve and capture the done state. This is the path UX could not evaluate
    // from stills before: the failed card's fresh-session pending and done.
    const fresh = card.locator('[data-testid="error-card-repair-images-fresh-session"]')
    resetDelayMs = 1500
    await fresh.click()
    await page.waitForTimeout(400)
    assert(`${theme}: fresh-session press shows the pending spinner`, await card.locator('[data-testid="error-card-repair-images-fresh-session"] .animate-spin').count() === 1)
    await shot(`07-failed-fresh-pending-${theme}`)
    await page.waitForTimeout(1600)
    resetDelayMs = 0
    assert(`${theme}: fresh-session settles to a done confirmation`, await card.locator('[data-testid="error-card-repair-images-fresh-session-done"]').count() === 1)
    await shot(`08-failed-fresh-done-${theme}`)

    // retryable refusal: reload idle, stub a GENERIC 409 (image_repair_failed:
    // a moved transcript / unverifiable scan / OSError). The card stays idle and
    // the "Couldn't repair the images" notice shows above the composer, so the
    // Repair button remains pressable.
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 1000 })
    repairResponse = { status: 409, body: { slot: SLOT, code: 'image_repair_failed', error: 'the transcript moved under the repair; not re-issued' } }
    await card.locator('[data-testid="error-card-repair-images"]').click(); await page.waitForTimeout(700)
    assert(`${theme}: a generic refusal shows the Couldn't-repair notice`, await page.locator('[data-testid="refused-press-error"]').count() === 1)
    assert(`${theme}: a generic refusal keeps the Repair button pressable`, await card.locator('[data-testid="error-card-repair-images"]').count() === 1)
    await shot(`06-retryable-refusal-${theme}`)
  }
  await h.close()
  if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
}

main().catch(err => { console.error(err); process.exit(1) })
