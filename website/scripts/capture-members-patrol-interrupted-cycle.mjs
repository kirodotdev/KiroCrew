/**
 * Screenshots for the patrol block's NEW stopped reason `interrupted_cycle`.
 *
 * This PR makes an autonudge loop claim its cycle on disk BEFORE the turn it
 * claims, so a gateway restart landing mid-delivery neither replays a delivered
 * nudge nor bills an undelivered one. The record left behind is held INACTIVE
 * with `stopped_reason: 'interrupted_cycle'`, awaiting a re-activation that
 * settles the claimed cycle — a code no loop could carry before. The Crew
 * Members side panel renders a stopped loop's reason under "Auto patrol"
 * (`member-patrol-reason`), so that line is where this PR becomes user-visible.
 *
 * `interrupted_cycle` has no entry in that page's PATROL_STOPPED_REASON catalog,
 * so the render site's documented fallback shows the CODE itself. These frames
 * photograph it as it really is, beside a pre-existing reason that does have a
 * sentence: the missing string is a separate open finding, and a frame that hid
 * it would be evidence of something this PR does not ship.
 *
 * Drives the isolated capture entry (website/capture/members-page.html), which
 * mounts the REAL MembersPage against the shipped stylesheet, theme tokens and
 * i18n catalog, over the SHARED roster stub (lib/members-fixtures.mjs) with one
 * added `/api/autonudge` route — registered after it, so it wins. Every frame
 * asserts the selected member AND the patrol block's `data-state` AND the
 * reason line's exact text before writing, so a frame cannot be written from a
 * state its filename denies.
 *
 * Frames:
 *   01-interrupted-cycle-dark         radar selected: reason line reads `interrupted_cycle`
 *   02-interrupted-cycle-block-dark   the same block, cropped so the line is legible
 *   03-cycle-cap-dark                 fixer selected: reason reads its sentence (contrast)
 *   04-interrupted-cycle-light        the new reason again, light theme
 *
 * Usage:
 *   node_modules/.bin/vite --host 127.0.0.1 --port 6841 --strictPort   # another shell
 *   node scripts/capture-members-patrol-interrupted-cycle.mjs http://127.0.0.1:6841 \
 *     ../temp-screenshots/members-patrol-interrupted-cycle
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { routeMembersApi } from './lib/members-fixtures.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/members-patrol-interrupted-cycle'
mkdirSync(OUT, { recursive: true })

const NOW = Date.now() / 1000

/** The code this PR introduces, and the pre-existing one it is shown against. */
const NEW_REASON = 'interrupted_cycle'
const OLD_REASON_TEXT = 'Reached its wake limit.'

/** A stopped loop as `GET /api/autonudge` delivers it: inactive, with a reason.
 *  `active: false` plus a present record is what MembersPage reads as 'stopped'. */
const stoppedLoop = (slug, stoppedReason, extra = {}) => ({
  id: `loop-${slug}`, slot_key: `member-${slug}`, active: false, banner: '',
  message: 'Sweep the queue and report only real signals.',
  idle_secs: 1200, max_cycles: 24, cycle_count: 3,
  last_fire_ts: NOW - 6 * 60, next_due_ts: 0,
  created_ts: NOW - 3600, max_runtime_secs: 0, gate: false,
  stopped_reason: stoppedReason, ...extra,
})
// radar was interrupted mid-delivery by a restart; fixer hit its wake limit.
const LOOPS = [
  stoppedLoop('radar', NEW_REASON, { cycle_count: 4 }),
  stoppedLoop('fixer', 'cycle_cap', { cycle_count: 24, last_fire_ts: NOW - 4 * 3600 }),
]
const SLOT_DETAIL = { key: 'member-radar', title: 'radar', running: false, messages: [] }

// This node ships its own libstdc++ on LD_LIBRARY_PATH, which the cached
// headless shell cannot load; drop it for the browser only.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv, executablePath: chromiumExecutable() })
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme) {
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1 })
  await routeMembersApi(page, SLOT_DETAIL)
  // Registered after the shared stub so this one wins: the roster fixture
  // answers `{}` for the registry, which the page reads as nothing armed.
  await page.route(u => new URL(u).pathname === '/api/autonudge', route =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ enabled: true, loops: LOOPS }),
    }))
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('radar', { exact: true }).first().waitFor({ timeout: 30_000 })
  return page
}

/**
 * Select a member from the roster and hold until its patrol block has settled on
 * `stopped` AND the reason line reads `expected`. Waiting on the TEXT is what
 * makes the frame safe: the side panel keeps the previous member's block mounted
 * for a moment after the click, so asserting only `data-state` could photograph
 * the wrong member's reason.
 */
async function selectStoppedMember(page, name, expected) {
  await page.getByText(name, { exact: true }).first().click()
  await page.locator('[data-testid="member-thread-header"]').getByText(name, { exact: true }).waitFor({ timeout: 30_000 })
  await page.waitForFunction(
    (want) => {
      const block = document.querySelector('[data-testid="member-patrol"]')
      const reason = document.querySelector('[data-testid="member-patrol-reason"]')
      return block?.getAttribute('data-state') === 'stopped' && reason?.textContent?.trim() === want
    },
    expected,
    { timeout: 30_000 },
  )
  if (await page.getByText('Something went wrong').isVisible().catch(() => false)) {
    throw new Error('ErrorBoundary visible — the frame would show a crash, not the feature')
  }
  // Let the block's cross-fade settle so the frame is the resting layout.
  await page.waitForTimeout(400)
  return (await page.getByTestId('member-patrol-reason').textContent() || '').trim()
}

// 01/02 — the new reason, dark. The line reads the CODE, because the catalog
// holds no sentence for it yet; asserting the code is what keeps this honest.
{
  const page = await newPage('dark')
  const reason = await selectStoppedMember(page, 'radar', NEW_REASON)
  check('01 reason line reads the new code', reason === NEW_REASON, `reason="${reason}"`)
  await page.screenshot({ path: `${OUT}/01-interrupted-cycle-dark.png` })
  await page.getByTestId('member-patrol').screenshot({ path: `${OUT}/02-interrupted-cycle-block-dark.png` })
  await page.close()
}

// 03 — a pre-existing reason in the same block, for contrast: this one has a
// catalog sentence, so what differs on screen is the missing string alone.
{
  const page = await newPage('dark')
  const reason = await selectStoppedMember(page, 'fixer', OLD_REASON_TEXT)
  check('03 pre-existing reason reads its sentence', reason === OLD_REASON_TEXT, `reason="${reason}"`)
  check('03 pre-existing reason is not a raw code', !/_/.test(reason), `reason="${reason}"`)
  await page.screenshot({ path: `${OUT}/03-cycle-cap-dark.png` })
  await page.close()
}

// 04 — the new reason again under the light theme, same assertion.
{
  const page = await newPage('light')
  const reason = await selectStoppedMember(page, 'radar', NEW_REASON)
  check('04 reason line reads the new code (light)', reason === NEW_REASON, `reason="${reason}"`)
  await page.screenshot({ path: `${OUT}/04-interrupted-cycle-light.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
