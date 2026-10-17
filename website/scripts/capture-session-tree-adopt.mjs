/**
 * Screenshot + clip harness, and behaviour check, for ADOPT and RELEASE in the
 * sidebar's conductor lane.
 *
 * This ASSERTS as well as photographs. jsdom mocks framer-motion out entirely, so the
 * unit tests can pin which rows RENDER but not the thing a reviewer wants to see, which
 * is a branch visibly moving under a new parent and coming back. Hence a real browser
 * and a per-beat assertion that exits non-zero: a blank or wrong frame has to fail here
 * rather than ship as evidence.
 *
 * The beat that matters is 3. `chat-wave` has never been expanded -- nobody has ever
 * opened it, so it is collapsed like every other row -- and the adoption puts a branch
 * the person is WATCHING underneath it. Without the lane's parent-changed expand those
 * rows unmount on an action the person did not take, so the assertion is that they are
 * still on screen at one level deeper, and the control is that `chat-wave` is absent
 * from the persisted expanded set before the adoption and present after it.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6181 --strictPort        # in another shell
 *   node scripts/capture-session-tree-adopt.mjs http://127.0.0.1:6181 [outDir]
 *   RECORD_VIDEO=1 node scripts/capture-session-tree-adopt.mjs http://127.0.0.1:6181 [outDir]
 *
 * The clip is one continuous take over all four beats, so the movement between them is
 * what the reader sees. webm out of Playwright; convert with ffmpeg if an mp4 is wanted.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6181'
const OUT = process.argv[3] || '../temp-screenshots/session-tree-adopt'
const WAVE = 'chat-wave'
const LANE = 'chat-lane'
const RECORD = process.env.RECORD_VIDEO === '1'
/** Long enough for the movement to be legible in the clip, skipped when only the
 *  still frames and the assertions are wanted. */
const BEAT = RECORD ? 1400 : 250

mkdirSync(OUT, { recursive: true })

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed = true
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const VIEWPORT = { width: 620, height: 720 }
const context = await browser.newContext({
  viewport: VIEWPORT,
  deviceScaleFactor: RECORD ? 1 : 2,
  ...(RECORD ? { recordVideo: { dir: `${OUT}/video-raw`, size: VIEWPORT } } : {}),
})
const page = await context.newPage()
page.on('pageerror', e => { console.log(`FAIL pageerror — ${e.message}`); failed = true })
// The dev server PROXIES `/api` to whatever gateway is running on this box, so without
// this the harness reads that gateway's real theme config -- one run came out
// gruvbox-light -- and the clip's appearance depends on the machine it was recorded on.
// The stub answers every boot fixture itself, so nothing leaves the browser.
//
// `extra` is needed because this harness runs against the DEV server, where source
// modules are served by their own path: the stub's `**/api/**` pattern also matches
// `/src/api/client.ts`, and answering that with a fixture serves JSON where the browser
// requires JavaScript, so the page never mounts. The sibling harnesses do not hit this
// because they serve the BUILT bundle, which has no such path.
await stubDashboardApi(page, {
  theme: 'dark',
  folders: [],
  extra: async (path, route) => {
    if (path.startsWith('/api/')) return false
    await route.continue()
    return true
  },
})
logPageProblems(page)

/** Row keys in rendered document order, with the depth the lane gave each one. */
const rows = () => page.$$eval('[data-slot-key]', els => els.map(el => ({
  key: el.getAttribute('data-slot-key'),
  depth: el.closest('[data-conductor-depth]')?.getAttribute('data-conductor-depth') ?? null,
})))
const keys = async () => (await rows()).map(r => r.key).join(' ')
const depthOf = async key => (await rows()).find(r => r.key === key)?.depth ?? 'absent'

/** The PERSISTED expanded set -- the control for whether the expand was the lane's
 *  doing rather than a row that happened to be open already. */
const expandedSet = () => page.evaluate(() => {
  try { return JSON.parse(localStorage.getItem('mc-sidebar-conductor-expanded') || '[]') }
  catch { return ['<unparseable>'] }
})

/**
 * The colour theme is applied ASYNCHRONOUSLY after mount, so a frame taken too early
 * carries a different theme than its siblings. Seeding localStorage does not win that
 * race, so wait for `data-theme` to stop moving.
 */
async function settleTheme() {
  let prev = null
  for (let i = 0; i < 20; i++) {
    const now = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
    if (now && now === prev) return now
    prev = now
    await page.waitForTimeout(250)
  }
  return prev
}

const shot = name => page.locator('.sidebar-inner').screenshot({ path: `${OUT}/${name}.png` })

await page.goto(`${BASE}/capture/session-tree-adopt.html?theme=dark`)
await page.waitForSelector('[data-capture-ready]')
await page.waitForSelector(`[data-slot-key="${LANE}"]`)
const theme = await settleTheme()
// Exact, not just "settled": the appearance is part of the evidence, and a theme read
// off a live gateway is how a clip ends up looking different on the next machine.
check('theme is the pinned Kiro dark', theme === 'kiro-dark', String(theme))

// ── Beat 1: at rest. Two conductors at the top level, the second one collapsed over a
// branch of its own -- the state every session starts in.
{
  const atRest = await keys()
  check('beat 1: two roots, both collapsed', atRest === `${WAVE} ${LANE}`, atRest)
  const before = await expandedSet()
  check('beat 1: nothing is expanded yet', before.length === 0, JSON.stringify(before))
  await shot('01-at-rest')
  await page.waitForTimeout(BEAT)
}

// ── Beat 2: the person opens the branch they want to watch. This is what makes beat 3
// a LOSS rather than a change of view: these rows are on screen before the adoption.
{
  await page.click(`[data-testid="conductor-chevron-${LANE}"]`)
  await page.waitForTimeout(300)
  await page.click('[data-testid="conductor-chevron-chat-verbs"]')
  await page.waitForTimeout(300)
  const opened = await keys()
  check('beat 2: the whole branch is on screen, three levels deep',
    opened === `${WAVE} ${LANE} chat-fold chat-verbs chat-cold`, opened)
  check('beat 2: the grandchild sits at depth 2', await depthOf('chat-cold') === '2', await depthOf('chat-cold'))
  await shot('02-branch-open')
  await page.waitForTimeout(BEAT)
}

// ── Beat 3: the adoption. An agent calls `session_adopt`, the next slots frame cites a
// new creator, and the branch moves. `chat-wave` was never opened by anyone, so the
// lane has to open it or the rows from beat 2 vanish.
{
  const priorExpanded = await expandedSet()
  check('control: the new parent is NOT expanded before the adoption',
    !priorExpanded.includes(WAVE), JSON.stringify(priorExpanded))

  await page.evaluate(() => window.__treeAdopt())
  await page.waitForTimeout(600)

  const moved = await keys()
  check('beat 3: every row from beat 2 is still on screen',
    moved === `${WAVE} ${LANE} chat-fold chat-verbs chat-cold`, moved)
  check('beat 3: the adopted conductor is now one level deep',
    await depthOf(LANE) === '1', await depthOf(LANE))
  check('beat 3: its grandchild moved down with it',
    await depthOf('chat-cold') === '3', await depthOf('chat-cold'))
  const nowExpanded = await expandedSet()
  check('beat 3: the lane opened the new parent itself',
    nowExpanded.includes(WAVE), JSON.stringify(nowExpanded))
  await shot('03-adopted')
  await page.waitForTimeout(BEAT)
}

// ── Beat 4: the release. The citation is cleared, so the row returns to the top level
// and takes its branch with it -- only its own edge upward went.
{
  await page.evaluate(() => window.__treeRelease())
  await page.waitForTimeout(600)

  const back = await keys()
  check('beat 4: the released conductor is a root again', back === `${WAVE} ${LANE} chat-fold chat-verbs chat-cold`, back)
  check('beat 4: it sits at the top level', await depthOf(LANE) === '0', await depthOf(LANE))
  check('beat 4: its branch came back with it', await depthOf('chat-cold') === '2', await depthOf('chat-cold'))
  await shot('04-released')
  await page.waitForTimeout(BEAT)
}

// The video is finalized on context close and its path is only readable from the page
// before that, so read it first and then close in order.
const clip = RECORD ? await page.video()?.path() : null
await context.close()
await browser.close()
if (clip) console.log(`WEBM ${clip}`)
console.log(failed ? 'FAILED' : 'all beats ok')
process.exit(failed ? 1 : 0)
