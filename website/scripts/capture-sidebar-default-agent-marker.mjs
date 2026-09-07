/**
 * Screenshot harness for the sidebar's inherited-default agent marker (#6529).
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli.
 *
 * Every frame renders the SAME PAIR: two records with the same resolved alias and
 * opposite stored state -- one with no stored agent (resolves the current default
 * at run time) and one pinned to `kirocrew`, which happens to BE the current
 * default. That pair is the whole point: before this change both rendered the bare
 * alias, so nothing on the row distinguished them.
 *
 *   1. session rows, comfortable width  -> the marker separates the pair
 *   2. history rows (Older Sessions)    -> the same pair, second surface
 *   3. session rows at SIDEBAR_MIN + tags -> the disclosed cost, measured
 *
 * Frame 3 exists to SHOW the trade-off rather than assert it is fine: the label
 * grows from 8 characters to 18 and the name span already carries `max-w-[50%]`
 * once the row has tags, so this frame is what a reviewer judges the truncation
 * on. The `title` is why a clipped label stays readable.
 *
 * Each frame ASSERTS its own payload before writing the PNG -- a capture that
 * silently photographs a stale bundle is worse than no capture, because it looks
 * like evidence. `--verify-only` runs the assertions and writes nothing.
 *
 * Usage: node scripts/capture-sidebar-default-agent-marker.mjs [outDir] [--verify-only]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const VERIFY_ONLY = args.includes('--verify-only')
const OUT = args.find(a => !a.startsWith('--')) || '/tmp/sidebar-default-agent-marker'

if (!VERIFY_ONLY) mkdirSync(OUT, { recursive: true })

const DEFAULT_AGENT = 'kirocrew'
/** The marker's visible spelling, English catalog. */
const MARKED = `${DEFAULT_AGENT} \u00b7 default`

// Fresh, not a literal date: the dormant-session collapse measures age against
// now, and a hardcoded timestamp would quietly stop rendering rows once it aged
// past the threshold. The collapse is also pinned off in localStorage below.
const NOW_ISO = new Date(Date.now() - 60_000).toISOString()
const NOW_EPOCH = Math.floor(Date.now() / 1000)

const TAGS = [
  { id: 't-review', name: 'review', color: '#7aa2f7', order: 0 },
  { id: 't-dashboard', name: 'dashboard', color: '#9ece6a', order: 1 },
]

/** The pair, as live slots. `agent: ''` is the legacy agent-less record. */
const pairSlots = (tags = []) => [
  { key: 'chat-inherited', title: 'Sidebar marker follow-up', messages: 12, running: false, agent: '', mode: '', tags, last_ts: NOW_ISO, last_turn_ts: NOW_ISO },
  { key: 'chat-pinned', title: 'Pinned to the same alias', messages: 8, running: false, agent: DEFAULT_AGENT, mode: '', tags, last_ts: NOW_ISO, last_turn_ts: NOW_ISO },
]

/** The pair, as archived sessions. The inherited one recorded no agent at all,
 *  which is the shape 28% of real transcripts on a live install have. */
const PAIR_HISTORY = [
  { key: 'dashboard_chat-old-inherited', title: 'Archived, no agent recorded', messages: 40, modified: NOW_EPOCH, created: NOW_ISO },
  { key: 'dashboard_chat-old-pinned', title: 'Archived, pinned to kirocrew', messages: 31, agent: DEFAULT_AGENT, modified: NOW_EPOCH, created: NOW_ISO },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/**
 * Open /chat with the pair seeded.
 *
 * `history` seeds the Older Sessions pane; `sidebarWidth` narrows the rail.
 */
async function openSidebar({ slots, history = [], sidebarWidth = 320, deepLinkHistory = false }) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  const extra = async (path, route) => {
    // What ACTUALLY feeds ChatSidebar's `defaultAgent` prop: ChatPage reads it
    // from `useAgents`, which takes `default_agent` off GET /api/agents. The
    // stub's own named route for that path returns a bare ARRAY, so
    // `d.default_agent` is undefined and the prop is '' -- which is the boot
    // window, the one state that renders NO marker. This override is why the
    // frame photographs the thing it exists to show.
    if (path === '/api/agents') {
      await json(route, {
        agents: [{ name: DEFAULT_AGENT, source: 'builtin' }, { name: 'oncall', source: 'aim' }],
        default_agent: DEFAULT_AGENT,
      })
      return true
    }
    // Kept for the same reason even though nothing on this surface reads it:
    // the shared ['default-agent'] query is invalidated on refresh events, and
    // a `{}` answer there would leave a second consumer disagreeing with the
    // first about what the default is.
    if (path === '/api/config/default-agent') {
      await json(route, { default_agent: DEFAULT_AGENT })
      return true
    }
    if (path.startsWith('/api/sessions')) {
      await json(route, { sessions: history, has_more: false })
      return true
    }
    if (path === '/api/chat/tags') {
      await json(route, TAGS)
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots,
    extra,
    localStorageEntries: {
      'mc-lang': 'en',
      'mc-sidebar-width': String(sidebarWidth),
      'mc-sidebar-pinned': 'true',
      // Both rows are minutes old, but pin the collapse off anyway so this
      // harness cannot start rendering an empty list as it ages.
      'mc-session-stale-collapse-ms': '0',
    },
  })
  await page.goto(base + '/chat' + (deepLinkHistory ? '?history=1' : ''), { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  return { context, page }
}

/** Every rendered agent label in the sidebar, in document order. */
const labels = page => page.$$eval('.session-agent-label', els =>
  els.map(el => (el.querySelector('span')?.textContent || '').replace(/\u00A0/g, '').trim()).filter(Boolean))

/**
 * Fail LOUDLY when the payload is not what the frame claims.
 *
 * The failure this guards is specific and has happened before: `pod up
 * --provision` and an incremental build both skip work when they think nothing
 * changed, so the served bundle can be the PRE-CHANGE one while the source tree
 * is correct. A PNG of that is indistinguishable from a PNG of the fix.
 */
function assertPair(found, frame) {
  const marked = found.filter(l => l === MARKED).length
  const bare = found.filter(l => l === DEFAULT_AGENT).length
  console.log(`  ${frame}: labels=${JSON.stringify(found)}`)
  if (marked < 1) throw new Error(`${frame}: no '${MARKED}' label rendered -- stale bundle, or the marker regressed`)
  if (bare < 1) throw new Error(`${frame}: no bare '${DEFAULT_AGENT}' label rendered -- the pinned row lost its plain label`)
}

/**
 * Write one frame as a viewport CLIP rather than an element screenshot.
 *
 * The sidebar has no stable wrapper selector to address (`aside` /
 * `[data-sidebar]` do not exist), and a clip is what the sibling harnesses in
 * this folder use. The nav rail ends around x=232, so the clip starts there:
 * a frame that leads with the rail spends half its width on chrome that has
 * nothing to do with what is being shown.
 */
const NAV_RAIL_W = 232
const shoot = async (page, name, { width, height = 460 }) => {
  if (VERIFY_ONLY) return
  const out = join(OUT, name)
  await page.screenshot({ path: out, clip: { x: NAV_RAIL_W, y: 0, width, height } })
  console.log('  wrote', out)
}

// 1 — session rows at a comfortable width: the pair, separated.
{
  const { context, page } = await openSidebar({ slots: pairSlots() })
  assertPair(await labels(page), 'session rows')
  await shoot(page, '01-session-rows-marked.png', { width: 340 })
  await context.close()
}

// 2 — the same pair as archived sessions in the Older Sessions pane.
{
  // One unrelated live slot: with an empty slots list ChatPage is in its
  // create-a-session path rather than its steady state, and the sidebar renders
  // no list at all.
  const { context, page } = await openSidebar({
    slots: [{ key: 'chat-live', title: 'An open session', messages: 3, running: false, agent: 'oncall', mode: '', tags: [], last_ts: NOW_ISO, last_turn_ts: NOW_ISO }],
    history: PAIR_HISTORY,
    deepLinkHistory: true,
  })
  // The deep link should have expanded the pane; click it if it did not, so this
  // frame reports on the ROWS rather than on how the pane came to be open.
  const disclosure = page.getByRole('button', { name: /^older sessions$/i }).first()
  await disclosure.waitFor({ state: 'visible', timeout: 10_000 })
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click()
  await page.getByTitle(PAIR_HISTORY[0].title).first().waitFor({ state: 'visible', timeout: 10_000 })
  assertPair(await labels(page), 'history rows')
  await shoot(page, '02-history-rows-marked.png', { width: 340, height: 900 })
  await context.close()
}

// 3 — SIDEBAR_MIN with tags on both rows: the disclosed truncation cost.
{
  const { context, page } = await openSidebar({ slots: pairSlots(TAGS.map(t => t.id)), sidebarWidth: 180 })
  // Deliberately NOT assertPair: at this width the label may legitimately be
  // ellipsized, which is the fact the frame reports. Assert the DOM still holds
  // the full string in `title`, which is what keeps a clipped row readable.
  const titles = await page.$$eval('.session-agent-label span[title]', els => els.map(el => el.getAttribute('title')))
  console.log(`  narrow rows: titles=${JSON.stringify(titles)}`)
  if (!titles.includes(MARKED)) throw new Error(`narrow rows: no span carries title='${MARKED}' -- a clipped label would be unreadable`)
  await shoot(page, '03-narrow-sidebar-with-tags.png', { width: 200 })
  await context.close()
}

await browser.close()
srv.close()
console.log(VERIFY_ONLY ? 'verify-only: all frames asserted' : 'all frames written to ' + OUT)
