/**
 * Screenshot harness, and behaviour check, for the conductor lane's CREATOR ANCHOR:
 * a crew whose conductor is a crew member, whose own thread the chat page never lists.
 *
 * Three frames over the same listed rows through the REAL `ChatSidebar`:
 *   before -- the store holds only the listed rows, which is what the lane saw before
 *             the fix: every worker at the top level wearing the "opened by a closed
 *             session" glyph, about a member that was open and dispatching.
 *   after  -- the store also holds the member's row (as it always did in the product;
 *             only `ChatPage`'s surface filter kept it from the sidebar). The lane
 *             borrows it as a dimmed anchor, SHUT by default: one row with the worker
 *             count and the subtree's needs-you badge on it.
 *   opened -- one press on that anchor's chevron, and every worker nests under it.
 *
 * Serves the capture page from the DEV server (`/capture/session-tree-member-anchor.html`),
 * with every `/api/**` boot fixture answered by the shared stub.
 *
 * Usage: node scripts/capture-session-tree-member-anchor.mjs [devBase] [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6181'
const OUT = process.argv[3] || '../temp-screenshots/session-tree-member-anchor'
const MEMBER = 'member-kirocrew-pipeline-conductor'
const WORKERS = ['chat-2124', 'chat-2125', 'chat-2130']

mkdirSync(OUT, { recursive: true })

let failed = false
const check = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed = true
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const context = await browser.newContext({ viewport: { width: 620, height: 640 }, deviceScaleFactor: 2 })
const page = await context.newPage()
page.on('pageerror', e => { console.log(`FAIL pageerror — ${e.message}`); failed = true })
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

const rows = () => page.$$eval('[data-slot-key]', els => els.map(el => ({
  key: el.getAttribute('data-slot-key'),
  depth: el.closest('[data-conductor-depth]')?.getAttribute('data-conductor-depth') ?? null,
  anchor: el.closest('[data-conductor-depth]')?.getAttribute('data-conductor-anchor') ?? null,
})))
const keys = async () => (await rows()).map(r => r.key).join(' ')
const rowOf = async key => (await rows()).find(r => r.key === key)
const orphanGlyphs = () => page.$$eval('[data-testid^="conductor-orphan-"]', els => els.map(el => el.getAttribute('data-orphan-of')))

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

await page.goto(`${BASE}/capture/session-tree-member-anchor.html?theme=dark`)
await page.waitForSelector('[data-capture-ready]')
await page.waitForSelector('[data-slot-key="chat-2124"]')
console.log('theme:', await settleTheme())
await page.waitForTimeout(300)

// ── before: the member row is not in the store ─────────────────────────────
console.log('before:', await keys())
for (const w of WORKERS) {
  const r = await rowOf(w)
  check(`before: ${w} sits at the top level`, r?.depth === '0', `depth=${r?.depth}`)
}
const glyphsBefore = await orphanGlyphs()
check('before: every worker wears the orphan glyph naming the member',
  WORKERS.every((_, i) => glyphsBefore[i] === MEMBER) && glyphsBefore.length === WORKERS.length,
  JSON.stringify(glyphsBefore))
check('before: the member row is absent', !(await keys()).includes(MEMBER))
await shot('before-member-driven-crew-orphaned')

// ── after: the store holds the member row, as the product's store always did ───
await page.evaluate(() => window.__withMember())
await page.waitForSelector(`[data-slot-key="${MEMBER}"]`)
await page.waitForTimeout(400)
console.log('after: ', await keys())
const member = await rowOf(MEMBER)
check('after: the member row is drawn as a top-level anchor', member?.depth === '0' && member?.anchor === 'true',
  `depth=${member?.depth} anchor=${member?.anchor}`)
// Collapsed by default: the crew is ONE row carrying its worker count and the
// subtree's badges, and no worker is on screen until the chevron is pressed.
for (const w of WORKERS) check(`after: ${w} is behind the chevron by default`, !(await rowOf(w)))
const count = await page.$eval(`[data-testid="conductor-child-count-${MEMBER}"]`, el => el.textContent)
check('after: the shut anchor counts its workers', count === String(WORKERS.length), `count=${count}`)
const needsYou = await page.$eval(`[data-testid="conductor-needs-you-${MEMBER}"]`, el => el.textContent).catch(() => null)
check('after: the shut anchor carries the worker that needs you', needsYou === '1', `needsYou=${needsYou}`)
check('after: no orphan glyph remains', (await orphanGlyphs()).length === 0, JSON.stringify(await orphanGlyphs()))
const opacity = await page.$eval(`[data-slot-key="${MEMBER}"]`, el =>
  getComputedStyle(el.closest('[data-conductor-depth]')).opacity)
check('after: the anchor is dimmed', opacity !== '1', `opacity=${opacity}`)
const title = await page.$eval(`[data-session-row="${MEMBER}"]`, el => el.getAttribute('title') || '')
check('after: the anchor says where it opens', /Members page/.test(title), title)
await shot('after-member-anchor-collapsed-by-default')

// ── opened: one press on the anchor's chevron shows the workers under it ─────
await page.click(`[data-testid="conductor-chevron-${MEMBER}"]`)
await page.waitForSelector('[data-slot-key="chat-2124"]')
await page.waitForTimeout(400)
console.log('opened:', await keys())
for (const w of WORKERS) {
  const r = await rowOf(w)
  check(`opened: ${w} nests one level under the member`, r?.depth === '1', `depth=${r?.depth}`)
}
// The child count stays (it says how many the row opened, open or shut); the
// subtree aggregate leaves, because the worker that needs you is now on screen.
check('opened: the count stays with the row', !!(await page.$(`[data-testid="conductor-child-count-${MEMBER}"]`)))
check('opened: the aggregate leaves with the fold', !(await page.$(`[data-testid="conductor-needs-you-${MEMBER}"]`)))
await shot('after-member-anchor-opened-nests-workers')

// The unrelated chat conductor beside it is unaffected in every frame: shut by
// default with its one worker counted, and it nests that worker once opened.
const pairCount = await page.$eval('[data-testid="conductor-child-count-chat-2134"]', el => el.textContent)
check('control: the chat conductor is shut with its worker counted', pairCount === '1', `count=${pairCount}`)
await page.click('[data-testid="conductor-chevron-chat-2134"]')
await page.waitForSelector('[data-slot-key="chat-2135"]')
const w1 = await rowOf('chat-2135')
check('control: the chat-conductor pair nests once opened', w1?.depth === '1', `depth=${w1?.depth}`)

await browser.close()
console.log(failed ? 'RESULT: FAIL' : 'RESULT: ok')
process.exit(failed ? 1 : 0)
