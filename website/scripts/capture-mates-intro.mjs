/**
 * The "Meet your Mates." launch intro -- evidence frames and a recording.
 *
 *   01-intro-<theme>.png      the dialog over the dashboard
 *   02-not-now-<theme>.png    mid-flight after Not now: the dialog is gone and
 *                             Mate's avatar is on its way to the Mates rail row
 *   02-new-tag-<theme>.png    landed: the "New" tag on the Mates rail row
 *   03-from-tag-<theme>.png   the Mates row clicked from that tag: Mate's chat
 *                             is open and the tag is gone
 *   intro-try-now.gif         dark: the dialog opens, the two crew rows drift in
 *                             opposite directions, Try now, Mate's chat
 *   intro-not-now.gif         dark: Not now, the avatar flies from the sample
 *                             card to the rail and lands as the New tag
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`), as a fresh install whose only mate is Mate:
 * the real gate opens the real `StartupVideoModal`, which draws the real
 * `MeetYourMatesIntro` for the `component: "crewmates"` offer the stub serves.
 *
 * Each frame is asserted, not only photographed: no `<video>` in the dialog,
 * the focus trap, both rows moving, no fade over the rows, the verdict POST,
 * the flyer carrying the card's own avatar while the tag waits invisible, the
 * tag's stored route, and the address after each button.
 *
 * Usage:
 *   cd website && npm run build && node scripts/capture-mates-intro.mjs [outDir] [distDir]
 */
import { chromium } from 'playwright'
import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'mates-intro')
const DIST = process.argv[3]
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
/** The first Mate, as every install has it: addressable, never chatted with. */
const MATE = {
  name: 'mate', slug: 'mate', bound: true, slot_key: 'member-mate', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'mate', memory_version: 2, memory_owner: 'mate',
  model: '', description: '', source: 'kirocrew', member_id: 'mate',
  last_active_ts: 0, last_message: '',
}
// A chat slot too, or the home page creates one on boot, which the stub's slot
// list cannot answer.
const SLOTS = [
  { key: 'dashboard_main', title: 'New Session', mode: 'chat', created: new Date((now - 120) * 1000).toISOString(), last_ts: new Date((now - 120) * 1000).toISOString(), running: false, agent: 'kirocrew' },
  { key: 'member-mate', title: 'Mate', mode: 'member', created: new Date((now - 60) * 1000).toISOString(), last_ts: new Date((now - 60) * 1000).toISOString(), running: false, agent: 'mate' },
]
/** What `feature_videos.py` serves for the `crewmates` entry: no media, a component, a route. */
const OFFER = {
  video: {
    id: 'crewmates', feature: 'crewmates',
    title: 'Meet your Mates.', description: 'They find the work, do it, and ask only when they need you.',
    src: '', poster: '', duration_s: 0, doc: 'crew-members.md', min_version: '',
    cta_route: '/members?member=mate', component: 'crewmates', source: 'local',
  },
  enabled: false,
  download_enabled: true,
}
// Mate's greeting reads the crew board on this page; the shipped empty fixture.
const CREW_BOARD = JSON.parse(readFileSync(new URL('./fixtures/crew-board-empty.json', import.meta.url), 'utf8'))
// Expanded rail (`mc-nav` 0) so the row label and the tag read; Mates preview on.
const STORAGE = { 'mc-lang': 'en', 'mc-nav': '0', 'mc-members-panel-open': '0', 'mc-preview-crew': '1', 'mc-privacy-notice-v1': '1' }

const verdicts = []
const extraFor = (theme) => async (path, route) => {
  if (path === '/api/theme/boot') {
    await json(route, { mode: theme, theme: '', onboarded: true, import_onboarded: true, privacy_acked: true, crewmates_onboarded: false })
    return true
  }
  if (path === '/api/chat/slots' && route.request().method() === 'POST') { await json(route, SLOTS[0]); return true }
  if (path === '/api/feature-videos/next') { await json(route, OFFER); return true }
  if (path === '/api/feature-videos/feedback') {
    verdicts.push(JSON.parse(route.request().postData() || '{}'))
    await json(route, { ok: true })
    return true
  }
  if (path === '/api/members') { await json(route, { members: [MATE], default_agent: 'kirocrew' }); return true }
  if (path === '/api/teams') { await json(route, { teams: [] }); return true }
  if (path === '/api/crew-board') { await json(route, CREW_BOARD); return true }
  if (path === '/api/agents/installed') { await json(route, [{ name: 'kirocrew', kiro_agent: 'kirocrew', scope: 'global' }]); return true }
  if (path === '/api/agents/catalog') { await json(route, { agents: [{ name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew', scope: 'global' }], default_agent: 'kirocrew' }); return true }
  if (path === '/api/slack/config') { await json(route, { configured: false, connected: false }); return true }
  if (/^\/api\/members\/[^/]+\/thread$/.test(path)) { await json(route, { slot_key: 'member-mate', slug: 'mate', member: 'mate', created: false }); return true }
  if (/^\/api\/members\/[^/]+\/recap$/.test(path)) { await json(route, { slug: 'mate', member: 'mate', paused: [], recent: [] }); return true }
  if (/^\/api\/members\/[^/]+\/greet$/.test(path)) { await json(route, { ok: true, started: false }); return true }
  if (/^\/api\/members\/[^/]+\/activity$/.test(path)) { await json(route, { slug: 'mate', member: 'mate', capped: false, entries: [] }); return true }
  if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
  if (/^\/api\/members\/[^/]+\/goals$/.test(path)) { await json(route, { goals: [] }); return true }
  return false
}

const { srv, base } = await serveDist(DIST || undefined)
const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

const INTRO = '[data-testid="meet-your-mates-intro"]'
const TRY = '[data-testid="startup-video-try-it"]'
const NOT_NOW = '[data-testid="startup-video-not-now"]'
const TAG = '[data-feature-new-tag="members"]'
const FLYER = '[data-feature-new-tag-flyer="members"]'
const AVATAR = '[data-testid="mates-intro-avatar"]'
const MATES_ROW = '[data-onboarding-nav="members"]'

async function open(theme, { record = false } = {}) {
  verdicts.length = 0
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: theme,
    ...(record ? { recordVideo: { dir: OUT, size: { width: 1440, height: 900 } } } : {}),
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme, extra: extraFor(theme), slots: SLOTS, localStorageEntries: STORAGE })
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.locator(INTRO).waitFor({ state: 'visible', timeout: 30000 })
  // Past the dialog's 220ms entrance.
  await page.waitForTimeout(600)
  return { context, page }
}

async function assertIntro(page, tag) {
  check(`${tag} no player in the dialog`, await page.locator('[role="dialog"] video').count() === 0)
  // Focus on open is pinned by StartupVideoModal.test.tsx; here the chat page
  // behind the dialog mounts late and its composer takes focus for a moment
  // (the same race the clip dialog has on main), so the frame asserts the trap
  // instead: a Tab from inside the dialog stays inside it.
  await page.locator(TRY).focus()
  await page.keyboard.press('Tab')
  check(`${tag} Tab stays inside the dialog`, await page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]')))
  check(`${tag} two buttons only`, await page.locator('[role="dialog"] button').count() === 2)
  const x = async (id) => page.locator(`[data-testid="mates-intro-row-${id}"]`).evaluate(el => new DOMMatrixReadOnly(getComputedStyle(el).transform).e)
  const [l0, r0] = [await x('left'), await x('right')]
  await page.waitForTimeout(700)
  const [l1, r1] = [await x('left'), await x('right')]
  check(`${tag} top row slides left`, l1 < l0, `${l0.toFixed(1)} -> ${l1.toFixed(1)}`)
  check(`${tag} bottom row slides right`, r1 > r0, `${r0.toFixed(1)} -> ${r1.toFixed(1)}`)
  check(`${tag} rows hidden from the tree`, await page.locator('[data-testid="mates-intro-row-left"]').evaluate(el => !!el.closest('[aria-hidden="true"]')))
  // The rows run off the card with a hard clip: nothing painted over them.
  check(`${tag} no fade over the rows`, await page.locator('[data-testid="mates-intro-row-left"]').evaluate(el => {
    const box = el.parentElement
    return getComputedStyle(box).overflow === 'hidden' && [...box.children].every(c => !getComputedStyle(c).backgroundImage.includes('gradient'))
  }))
}

/** Not now, asserting the flight: the flyer is the card's avatar, the tag waits
 *  invisible until it lands, then the flyer is gone and the tag is on the row. */
async function notNow(page, tag) {
  const avatarSrc = await page.locator(AVATAR).getAttribute('src')
  await page.locator(NOT_NOW).click()
  check(`${tag} dialog closed`, await page.locator('[role="dialog"]').count() === 0)
  check(`${tag} avatar flies`, await page.locator(FLYER).count() === 1)
  check(`${tag} the flyer is the card's avatar`, await page.locator(FLYER).getAttribute('src') === avatarSrc)
  check(`${tag} tag waits invisible`, await page.locator(TAG).evaluate(el => el.dataset.featureNewTagState === 'arriving' && getComputedStyle(el).visibility === 'hidden'))
  // Mid-flight, for the still.
  await page.waitForTimeout(250)
  await page.screenshot({ path: join(OUT, `02-not-now-${tag.split(' ').pop()}.png`) })
  await page.locator(TAG).waitFor({ state: 'visible', timeout: 10000 })
  await page.locator(FLYER).waitFor({ state: 'detached', timeout: 10000 })
  // Past the pill's landing pop.
  await page.waitForTimeout(400)
  check(`${tag} tag landed`, await page.locator(TAG).evaluate(el => el.dataset.featureNewTagState === 'shown'))
}

const address = (page) => page.evaluate(() => location.pathname + location.search)

for (const theme of ['light', 'dark']) {
  const { context, page } = await open(theme)
  await page.screenshot({ path: join(OUT, `01-intro-${theme}.png`) })
  await assertIntro(page, `01 ${theme}`)

  // Not now: the verdict is seen, the dialog goes, the avatar flies to the Mates
  // row and lands as the tag.
  await notNow(page, `02 ${theme}`)
  check(`02 ${theme} verdict seen`, verdicts.length === 1 && verdicts[0].status === 'seen' && verdicts[0].id === 'crewmates', JSON.stringify(verdicts))
  check(`02 ${theme} tag reads New`, (await page.locator(TAG).textContent())?.trim() === 'New')
  check(`02 ${theme} tag remembers the route`, await page.evaluate(() => JSON.parse(localStorage.getItem('mc-feature-new-tag-routes') || '{}').members) === '/members?member=mate')
  check(`02 ${theme} stayed off the Mates page`, !(await address(page)).startsWith('/members'), await address(page))
  await page.screenshot({ path: join(OUT, `02-new-tag-${theme}.png`) })

  // From the tag: the Mates row opens Mate's chat and the tag clears.
  await page.locator(MATES_ROW).first().click()
  await page.waitForFunction(() => location.search === '?member=mate', null, { timeout: 15000 })
  await page.locator(TAG).waitFor({ state: 'detached', timeout: 10000 })
  await page.getByTestId('crewmate-switcher').locator('visible=true').first().waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(500)
  check(`03 ${theme} opened Mate's chat from the tag`, await address(page) === '/members?member=mate', await address(page))
  check(`03 ${theme} tag cleared`, await page.locator(TAG).count() === 0)
  check(`03 ${theme} route forgotten`, await page.evaluate(() => JSON.parse(localStorage.getItem('mc-feature-new-tag-routes') || '{}').members) === undefined)
  await page.screenshot({ path: join(OUT, `03-from-tag-${theme}.png`) })
  await context.close()
}

/* ── the recording: rows drifting, Try now, Mate's chat ─────────────────── */
{
  const { context, page } = await open('dark', { record: true })
  await page.waitForTimeout(3500)
  await page.locator(TRY).click()
  await page.waitForFunction(() => location.search === '?member=mate', null, { timeout: 15000 })
  await page.getByTestId('crewmate-switcher').locator('visible=true').first().waitFor({ state: 'visible', timeout: 30000 })
  check('rec Try now records seen', verdicts.length === 1 && verdicts[0].status === 'seen', JSON.stringify(verdicts))
  check('rec no tag after Try now', await page.locator(TAG).count() === 0)
  await page.waitForTimeout(2000)
  await page.close()
  await context.close()
  const webm = readdirSync(OUT).find(f => f.endsWith('.webm'))
  if (webm) {
    const src = join(OUT, webm)
    execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-ss', '1', '-i', src,
      '-vf', 'fps=10,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5',
      join(OUT, 'intro-try-now.gif')])
    rmSync(src)
  } else {
    check('rec written', false, 'no webm found')
  }
}

/* ── the recording: Not now, the avatar flies to the rail, the New tag ───── */
{
  const { context, page } = await open('dark', { record: true })
  await page.waitForTimeout(1500)
  await page.locator(NOT_NOW).click()
  await page.locator(TAG).waitFor({ state: 'visible', timeout: 10000 })
  check('rec Not now records seen', verdicts.length === 1 && verdicts[0].status === 'seen', JSON.stringify(verdicts))
  await page.waitForTimeout(1500)
  await page.close()
  await context.close()
  const webm = readdirSync(OUT).find(f => f.endsWith('.webm'))
  if (webm) {
    const src = join(OUT, webm)
    // 15 fps: the flight is 0.8 s, and at 10 fps it would be eight frames.
    execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-ss', '0.8', '-i', src,
      '-vf', 'fps=15,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5',
      join(OUT, 'intro-not-now.gif')])
    rmSync(src)
  } else {
    check('rec not-now written', false, 'no webm found')
  }
}

await browser.close()
srv.close()
if (failed) {
  console.error('mates-intro capture: assertion failure(s)')
  process.exit(1)
}
console.log(`mates-intro capture: frames + recording written to ${OUT}`)
