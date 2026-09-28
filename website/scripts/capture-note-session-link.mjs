/**
 * Real-browser screenshots + assertions for a note's `/chat?sid=` session link.
 *
 * Drives website/capture/note-session-link.html, which mounts the REAL
 * `MarkdownRenderer` inside the transcript's own note-row bubble and exposes
 * window.__measure().
 *
 * src/test/ChatPage.noteSessionLink.test.tsx pins the wiring and the resulting
 * attributes, but a reviewer cannot see from a diff that the link stops
 * advertising a second tab and starts switching session. Both arms are therefore
 * photographed AFTER A CLICK, which is where the user-visible difference is: one
 * opens a second browser tab and leaves the reader where they were, the other
 * changes the session in place. The tab count is read from the browser context,
 * not asserted in prose.
 *
 * Assertions, one per arm so no arm can pass for another's reason:
 *  - other/off: target="_blank", no title, and a click OPENS A SECOND PAGE while
 *    the session does not change -> the defect, so the before frame is proven
 *    rather than assumed.
 *  - other/on:  target ABSENT and the title names the destination session --
 *    both, because dropping the target while promising nothing would satisfy
 *    only the first.
 *  - other/on + click: the session changes AND no page is opened AND location is
 *    unchanged. "In place" is the conjunction; any one alone is not the claim.
 *  - self/on:   target="_blank" retained -> negative control. A self-link cannot
 *    resolve, so this is what shows the after frame's missing target comes from
 *    resolving the key rather than from the branch always dropping it.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-note-session-link.mjs http://127.0.0.1:6841 ../temp-screenshots/note-session-link
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/note-session-link'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1100, height: 420 }
const TARGET_TITLE = 'Release notes draft'
const HERE_TITLE = 'Packing checklist review'
const HREF = '/chat?sid=chat-2-1788000001'

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

/** One arm in its own context, so the page count is that arm's alone. */
const open = async (scene, fix) => {
  const context = await browser.newContext({ viewport: VIEWPORT })
  const page = await context.newPage()
  await page.goto(`${BASE}/capture/note-session-link.html?scene=${scene}&fix=${fix}&theme=dark`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-bubble] a')
  await page.waitForSelector('[data-readout]')
  return { context, page }
}

const log = (m, extra = '') => console.log(
  `${m.scene}/${m.fix}: target=${String(m.target).padEnd(6)} title=${JSON.stringify(m.title)} ` +
  `href=${m.href} active="${m.activeTitle}"${extra}`,
)

/** Click and let either outcome settle: a session switch, or a second page. */
const clickAndSettle = async (context, page) => {
  await page.click('[data-bubble] a')
  await page.waitForTimeout(600)
  return context.pages().length
}

// 1. The defect: clicking opens a second tab and leaves the reader put.
{
  const { context, page } = await open('other', 'off')
  const m = await page.evaluate(() => window.__measure())
  if (m.target !== '_blank') fail('other/off did not reproduce the second-tab affordance — the before frame would be meaningless')
  if (m.title) fail(`other/off should carry no tooltip, got ${JSON.stringify(m.title)}`)
  const pages = await clickAndSettle(context, page)
  const after = await page.evaluate(() => window.__measure())
  log(after, ` pages=${pages}`)
  if (pages !== 2) fail(`other/off click did not open a second tab (pages=${pages}) — the defect is unproven`)
  if (after.activeTitle !== HERE_TITLE) fail(`other/off unexpectedly switched session to "${after.activeTitle}"`)
  await page.bringToFront()
  await page.screenshot({ path: `${OUT}/before-external-link.png` })
  await context.close()
}

// 2. The same note after the fix, at rest.
{
  const { context, page } = await open('other', 'on')
  const m = await page.evaluate(() => window.__measure())
  log(m)
  if (m.target) fail(`other/on still advertises a second tab: target=${m.target}`)
  if (!m.title?.includes(TARGET_TITLE)) fail(`other/on tooltip does not name the destination: ${JSON.stringify(m.title)}`)
  if (m.href !== HREF) fail(`other/on rewrote the href: ${m.href}`)
  if (m.activeTitle !== HERE_TITLE) fail(`other/on started on the wrong session: ${m.activeTitle}`)
  await page.screenshot({ path: `${OUT}/after-session-link.png` })

  // 3. …and what activating it does: switches in place, no second tab.
  const pages = await clickAndSettle(context, page)
  const after = await page.evaluate(() => window.__measure())
  log(after, ` pages=${pages}`)
  if (after.activeTitle !== TARGET_TITLE) fail(`the click did not switch session: active is "${after.activeTitle}"`)
  if (pages !== 1) fail(`the click opened a tab as well (pages=${pages})`)
  if (after.locationHref !== m.locationHref) fail(`the click navigated instead of switching in place: ${m.locationHref} -> ${after.locationHref}`)
  await page.screenshot({ path: `${OUT}/after-switched-in-place.png` })
  await context.close()
}

// 4. Negative control: a link to the session already open stays inert.
{
  const { context, page } = await open('self', 'on')
  const m = await page.evaluate(() => window.__measure())
  log(m)
  if (m.target !== '_blank') fail('self/on lost the external affordance — a self-link must not become a switch')
  if (m.title) fail(`self/on gained a switch tooltip it cannot honour: ${JSON.stringify(m.title)}`)
  await page.screenshot({ path: `${OUT}/control-self-link-inert.png` })
  await context.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
