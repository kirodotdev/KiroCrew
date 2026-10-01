/**
 * Record the Recent sort moving the OPEN crewmate to the top when the user
 * messages it -- the one visible change in the roster-recency fix.
 *
 * WHY A RECORDING: the move is a relocation of a persistent, already-identified
 * element on a user action, so before/after stills cannot show continuity; and
 * the row now carries framer's `layout`, which a still cannot show at all.
 *
 * WHY THE ISOLATED ENTRY rather than a pod: what is under review is the roster's
 * own reaction to a pushed `member_projection` frame, and that arrives in this
 * harness through the SAME door as in production (`memberProjectionStore.apply`,
 * fired as a `capture:frame` of kind `recency`). A pod would add a gateway, an
 * agent turn and an onboarding flow, none of which is part of the move.
 *
 * The recording's own claim is asserted, not assumed: the cold row must start
 * BELOW the others and be first afterwards, and no roster refetch may have run
 * (`/api/members` call count unchanged), or the run fails rather than writing a
 * frame that documents the wrong thing.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort   # in another shell
 *   node scripts/record-members-recency-move.mjs http://127.0.0.1:6832 <outDir>
 *
 * The GIF is assembled by Pillow because Playwright's bundled ffmpeg carries no
 * gif encoder; set PYTHON to an interpreter that has PIL, FFMPEG to an ffmpeg.
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, rmSync, writeFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/members-recency-move'
const FF = process.env.FFMPEG || 'ffmpeg'
const PY = process.env.PYTHON || 'python3'
const W = 1180, H = 720
mkdirSync(OUT, { recursive: true })

let failed = false
const check = (name, ok, detail = '') => {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

/** Three crewmates whose Recent order is NOT their alphabetical order, so the
 *  move cannot be mistaken for the list merely re-sorting by name. `beta-lead`
 *  is the cold one -- the crewmate the user is about to message. */
const ROSTER = [
  { name: 'gamma-writer', slug: 'gamma-writer', bound: true, slot_key: 'member-gamma-writer', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', last_active_ts: 3000, last_message: 'Draft is up for review.' },
  { name: 'alpha-scout', slug: 'alpha-scout', bound: true, slot_key: 'member-alpha-scout', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', last_active_ts: 2000, last_message: 'Scouting the queue now.' },
  { name: 'beta-lead', slug: 'beta-lead', bound: true, slot_key: 'member-beta-lead', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', last_active_ts: 1000, last_message: 'Standing by.' },
]

const SLOT_DETAIL = {
  key: 'member-beta-lead',
  title: 'beta-lead',
  running: false,
  messages: [
    { role: 'assistant', content: 'Standing by.', ts: '2026-09-30T05:00:00Z' },
  ],
}

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: W, height: H },
  recordVideo: { dir: OUT, size: { width: W, height: H } },
  timezoneId: 'UTC',
  locale: 'en',
})
const page = await context.newPage()
page.on('pageerror', (e) => console.error(`  pageerror: ${e.message}`))

// The shared stub FIRST, then the two narrower handlers: Playwright tries the
// most recently registered route first, so a specific handler installed before
// the catch-all never runs -- which would leave the counter below reading 0 for
// every run and turn "no refetch" into a claim about nothing.
await routeMembersApi(page, SLOT_DETAIL, { members: ROSTER })

let rosterFetches = 0
await page.route((u) => new URL(u).pathname === '/api/members', (route) => {
  rosterFetches += 1
  return route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ members: ROSTER, default_agent: 'kirocrew' }),
  })
})
// The teams read, answered EMPTY rather than left to the shared stub's `{}`
// fallback: the roster's teams query treats a shape with no `teams` key as a
// failure and paints "Could not load teams" over the list -- a notice in the
// frame that is neither part of the move nor true of the product.
await page.route((u) => new URL(u).pathname === '/api/teams', (route) =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ teams: [] }) }),
)

await page.goto(`${BASE}/capture/members-page.html?theme=dark`)
await page.waitForSelector('[data-capture-root]')
const rows = () => page.locator('[data-testid="member-roster"] li button').first().page()
const names = async () =>
  (await page.locator('[data-testid="member-roster"] li').allInnerTexts())
    .map((t) => t.split('\n')[0].trim())
    .filter((n) => /^(alpha-scout|beta-lead|gamma-writer)$/.test(n))

await page.getByText('beta-lead', { exact: true }).first().waitFor()
await page.waitForTimeout(1400)
// The CONTROL for the counter below: the page fetches the roster to mount at
// all, so a zero here means the handler is shadowed rather than that nothing
// refetched -- and every later "no refetch" reading would be vacuous.
check('the roster-fetch counter observes a real fetch', rosterFetches >= 1, `fetches=${rosterFetches}`)
const teamsError = await page.getByText(/Could not load teams/i).count()
check('no stub-shaped error notice over the roster', teamsError === 0, `notices=${teamsError}`)
const initial = await names()
check('starts in Recent order, not alphabetical', initial.join() === 'gamma-writer,alpha-scout,beta-lead', initial.join(' '))

// Open the coldest crewmate. Opening alone must NOT reorder the list: the hold
// exists so rows do not move while the user works it.
await page.getByText('beta-lead', { exact: true }).first().click()
await page.waitForTimeout(1600)
const afterOpen = await names()
check('opening a crewmate does not reorder', afterOpen.join() === initial.join(), afterOpen.join(' '))
const fetchesBefore = rosterFetches

// A background crewmate advances QUIETLY first, outranking everyone. This is the
// row the hold exists for, and it is what separates lifting ONE row from
// re-sorting the list: a whole re-sort would publish this recency too and put
// gamma-writer first instead of the crewmate the user actually messaged.
await page.evaluate(() => {
  window.dispatchEvent(
    new CustomEvent('capture:frame', {
      detail: { kind: 'recency', slug: 'gamma-writer', ts: 50000, seq: 8 },
    }),
  )
})
await page.waitForTimeout(1200)
const afterQuiet = await names()
check('a quiet background advance moves nothing', afterQuiet.join() === initial.join(), afterQuiet.join(' '))

// THE MOVE: the user's own send advances the open crewmate's recency, carried by
// a pushed projection frame. Its `ts` is BELOW the background row's on purpose --
// the open row goes first because the user messaged it, not because it now ranks
// highest.
await page.evaluate(() => {
  window.dispatchEvent(
    new CustomEvent('capture:frame', {
      detail: { kind: 'recency', slug: 'beta-lead', ts: 9000, seq: 9 },
    }),
  )
})
await page.waitForTimeout(2600)
const afterSend = await names()
check('the open crewmate is now first', afterSend[0] === 'beta-lead', afterSend.join(' '))
// ONE row moved: the other two keep their order relative to each other, which a
// re-sort would not have left alone now that gamma-writer outranks both.
check('only the open row moved', afterSend.join() === 'beta-lead,gamma-writer,alpha-scout', afterSend.join(' '))
check('no roster refetch drove the move', rosterFetches === fetchesBefore, `fetches=${rosterFetches}`)

// A further background advance still moves nothing.
await page.evaluate(() => {
  window.dispatchEvent(
    new CustomEvent('capture:frame', {
      detail: { kind: 'recency', slug: 'alpha-scout', ts: 99000, seq: 10 },
    }),
  )
})
await page.waitForTimeout(1800)
const afterBackground = await names()
check('a background advance moves nothing', afterBackground.join() === afterSend.join(), afterBackground.join(' '))

const video = page.video()
if (!video) throw new Error('playwright recorded no video')
await context.close()
await browser.close()
const src = join(OUT, 'recency-move.webm')
renameSync(await video.path(), src)
console.log('WEBM', src, `${(statSync(src).size / 1e6).toFixed(2)} MB`)

// Crop to the roster column and assemble a GIF: the PR body needs one file a
// reviewer can see inline without a player, and the thread pane is not part of
// what moved.
const framesDir = join(OUT, 'frames')
rmSync(framesDir, { recursive: true, force: true })
mkdirSync(framesDir, { recursive: true })
const ff = spawnSync(FF, ['-y', '-i', src, '-vf', `crop=430:${H}:0:0`, '-r', '10', join(framesDir, 'f-%04d.png')], { stdio: 'ignore' })
if (ff.status !== 0) {
  console.log('FRAMES skipped -- ffmpeg failed; webm kept at', src)
  process.exit(failed ? 1 : 0)
}
writeFileSync(join(OUT, 'mkgif.py'), `
import glob, sys
from PIL import Image
files = sorted(glob.glob(sys.argv[1] + '/f-*.png'))
frames = [Image.open(f).convert('RGB') for f in files]
q = [f.quantize(colors=96, method=Image.Quantize.MEDIANCUT) for f in frames]
q[0].save(sys.argv[2], save_all=True, append_images=q[1:], duration=100, loop=0, optimize=True)
print('GIF', sys.argv[2], len(q), 'frames')
`)
const gif = join(OUT, 'recency-move.gif')
const r = spawnSync(PY, [join(OUT, 'mkgif.py'), framesDir, gif], { encoding: 'utf-8' })
process.stdout.write(r.stdout || '')
if (r.status !== 0) {
  console.log('GIF skipped:', r.stderr)
  process.exit(failed ? 1 : 0)
}
const mb = statSync(gif).size / 1e6
console.log(`GIF size ${mb.toFixed(2)} MB`)
// The review lanes skip a committed or attached file over 10 MB, so an oversize
// GIF is a silent no-evidence rather than a loud failure.
check('gif under the 10 MB lane ceiling', mb < 9.5, `${mb.toFixed(2)} MB`)
process.exit(failed ? 1 : 0)
