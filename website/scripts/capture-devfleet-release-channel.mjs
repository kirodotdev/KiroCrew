/**
 * Screenshots of the Dev Fleet release-channel worktree rows (PR #10066).
 *
 * Drives the ISOLATED capture entry (website/capture/devfleet-release-channel.html),
 * which mounts the REAL DevFleetPage with `fetch` stubbed at the network seam to
 * serve the `/fleet` payload. Every state under review is decided by that payload,
 * so each page load here IS the scenario the reviewer asked to see.
 *
 * Each scene asserts its headline state — and, where the claim is an ABSENCE,
 * also asserts the thing that must not be there — before shooting, so this can
 * never quietly emit a screenshot of an error boundary, a prerequisite gate, or
 * the wrong row class. The frames are then compared for distinct byte sizes:
 * two identical frames mean the fixture failed, not that the states match.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-devfleet-release-channel.mjs http://127.0.0.1:6812 ../temp-screenshots/devfleet-release-channel-10066
 */
import { chromium } from 'playwright'
import { mkdirSync, statSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/devfleet-release-channel-10066'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

const shot = []

// Whether Create is live is the entire difference between the benign row and the
// blocked one, and the two look alike in a still. Returns an assertion the scene
// runs before shooting, so a frame can never claim a state the run did not check.
const createEnabled = (want) => async (page, scene) => {
  const btn = page.getByRole('button', { name: 'Create worktree' })
  const disabled = await btn.isDisabled()
  if (disabled === want) {
    throw new Error(`scene=${scene}: Create worktree must be ${want ? 'enabled' : 'disabled'}`)
  }
}

async function shoot(scene, mustSee, mustNotSee, name, assert) {
  await page.goto(`${BASE}/capture/devfleet-release-channel.html?scene=${scene}&theme=dark`)
  for (const text of mustSee) {
    await page.waitForSelector(`text=${text}`, { timeout: 15000 })
  }
  // Absence is only meaningful once the row itself is on screen, which the
  // mustSee loop above has just established.
  for (const text of mustNotSee) {
    const n = await page.locator(`text=${text}`).count()
    if (n !== 0) throw new Error(`scene=${scene}: "${text}" must NOT be present, found ${n}`)
  }
  // Some claims are about a control's STATE, not about text: whether Create is
  // live is the whole difference between the benign and blocked rows, and a
  // screenshot of a disabled button is indistinguishable from a muted one at a
  // glance. Assert it here so the frame is evidence of a state the run verified.
  if (assert) await assert(page, scene)
  // Park the pointer away from the table: a hovered row raises its action
  // toolbar over the row name in every one of these harnesses.
  await page.mouse.move(4, 4)
  await page.waitForTimeout(400) // let the relative timestamps settle
  const path = `${OUT}/${name}`
  await page.screenshot({ path, fullPage: false })
  shot.push({ name, size: statSync(path).size })
  console.log(`captured ${name}`)
}

// The lane is materialized: the badge carries the release this checkout sits on,
// and the Behind cell names the channel tip as its denominator so it cannot be
// read as the feature row's behind-main figure.
await shoot('adopted', ['release-channel-stable', '0.5.0', 'tip'], [], '01-release-channel-adopted-dark.png')

// The lane exists with no worktree: a muted placeholder row is the only surface
// the feature is discoverable from, and its Create button must read as live.
await shoot('placeholder', ['release-channel-stable', 'no worktree yet', 'Create'], [],
  '02-release-channel-placeholder-create-dark.png')

// A BRANCH checkout occupying the reserved basename is NOT adopted: ordinary
// controls, an ordinary behind-main count, and no version badge.
await shoot('taken', ['release-channel-stable', '↓5'], ['0.5.0'],
  '03-release-channel-name-taken-branch-dark.png')

// The steady state: the tree is AT the channel tip, so the badge takes its ok
// variant and the Behind cell is a dash. Asserting no tip-denominated count
// survives is the point — the warn variant in shot 01 cannot stand in for this.
await shoot('at-tip', ['release-channel-stable', '0.5.3'], [String.raw`/↓\d+\s*tip/`],
  '04-release-channel-at-tip-dark.png')

// Benign and documented, NOT an incident: nothing resolvable here yet, said as
// plain information with Create still live, and no ErrorNotice anywhere.
await shoot('unpublished', ['release-channel-stable', 'No published stable release resolvable here yet'],
  ['could not be resolved'], '05-release-channel-unpublished-create-live-dark.png',
  createEnabled(true))

// The failure variant of that same row: Create disabled, the short label in the
// cell, and the full git message in the row-scoped ErrorNotice below it.
await shoot('blocked-placeholder',
  ['release-channel-stable', 'stable could not be resolved', 'Could not resolve host'], [],
  '06-release-channel-blocked-placeholder-dark.png', createEnabled(false))

// The second variant the placeholder frame cannot show: an ADOPTED row whose
// resolve() failed. It keeps the release its tree holds and routes the failure to
// the same ErrorNotice rather than to a Badge title, which is not keyboard-reachable.
await shoot('blocked-adopted',
  ['release-channel-stable', '0.5.0', 'Could not resolve host'], [],
  '07-release-channel-blocked-adopted-dark.png')

// Two frames of identical size are what a silently-failed fixture looks like.
const sizes = new Set(shot.map((s) => s.size))
if (sizes.size !== shot.length) {
  throw new Error(`frames are not distinct: ${shot.map((s) => `${s.name}=${s.size}`).join(', ')}`)
}
console.log(shot.map((s) => `${s.name} ${s.size}B`).join('\n'))

await browser.close()
