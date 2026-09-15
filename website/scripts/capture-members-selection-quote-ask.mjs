/**
 * Screenshots for Quote / Ask on a Crew Members thread (selection actions).
 *
 * Drives the isolated capture entry (website/capture/members-page.html),
 * which mounts the REAL MembersPage — and, through it, the real ChatPane and
 * SideChat. The selection is genuine Chromium input (a triple-click on the
 * assistant reply), so the toolbar that appears is the shipped one. Every
 * frame asserts its state before writing:
 *   01-toolbar      the selection toolbar over a member reply offers Quote,
 *                   Ask about this and Copy (before: Copy only)
 *   02-ask          Ask swapped the detail drawer to the Side Chat for the
 *                   member's slot, seeded with the selection as a blockquote;
 *                   the thread composer stayed empty
 *   03-quote        Quote landed the selection in the THREAD composer as a
 *                   blockquote (the Side Chat untouched)
 *   04-drawer-swap  .webm — the drawer's details → Side Chat crossfade on
 *                   Ask, and the "Details" way back (a still frame cannot
 *                   show the transition)
 *   05-return       back on details after an Ask, the header carries the
 *                   draft-marked "Side Chat" action (absent without a draft)
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6831 --strictPort   # in another shell
 *   node scripts/capture-members-selection-quote-ask.mjs http://127.0.0.1:6831 ../temp-screenshots/members-selection-quote-ask
 */
import { chromium } from 'playwright'
import { makeChecker } from './lib/prepare-split-chat-page.mjs'
import { routeMembersApi } from './lib/members-fixtures.mjs'
import { mkdirSync, readdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'

// Some dev hosts run a version-manager-built node that injects its own lib dir
// into LD_LIBRARY_PATH; Chromium inherits it and its system Mesa/LLVM then fail
// to load. Drop the injection before launching.
delete process.env.LD_LIBRARY_PATH

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/members-selection-quote-ask'
mkdirSync(OUT, { recursive: true })

const REPLY = 'Six new issues: four covered by open PRs, one routed to needs-human, one queued as auto-fixable.'

const MEMBERS = [
  { name: 'radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false, kiro_agent: 'kirocrew-autofix', workspace: 'autofix', memory_store: 'default', model: '', last_active_ts: 1000, last_message: 'Six new issues: four covered by open PRs.' },
  { name: 'fixer', slug: 'fixer', bound: true, slot_key: 'member-fixer', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '', last_active_ts: 900, last_message: 'Two PRs opened for the queue.' },
]

const browser = await chromium.launch()
const { check, failed } = makeChecker()

async function newPage(theme = 'dark', viewport = { width: 1440, height: 860 }, contextOpts = {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, ...contextOpts })
  const page = await context.newPage()
  // Gateway-free: the shared Members-page API stub (scripts/lib/members-fixtures.mjs)
  // answers every REAL call the page, its ChatPane and the SideChat make; this
  // harness only supplies its own roster and the radar thread under test.
  await routeMembersApi(page, {
    key: 'member-radar', title: 'radar', running: false, messages: [
      { role: 'user', content: 'What did you triage tonight?', ts: '2026-09-06T01:00:00Z' },
      { role: 'assistant', content: REPLY, ts: '2026-09-06T01:00:05Z' },
    ],
  }, { members: MEMBERS })
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('radar', { exact: true }).first().click()
  await page.getByText(REPLY).waitFor()
  return page
}

/** A genuine triple-click paragraph selection on the reply — the browser makes
 *  the selection, the shipped toolbar reacts to it. */
async function selectReply(page) {
  const reply = page.getByText(REPLY)
  await reply.click({ clickCount: 3, position: { x: 40, y: 8 } })
  // Fails loudly against the pre-fix pane (Copy only) instead of shooting a
  // frame without the actions under test.
  await page.getByRole('button', { name: 'Ask about this' }).waitFor({ state: 'visible', timeout: 5_000 })
  // The toolbar mounts through a 150ms opacity/scale animation; a frame taken
  // the moment it is "visible" catches it semi-transparent over the selection.
  await page.waitForTimeout(300)
}

const threadComposer = (page) => page.locator('[data-chat-pane] textarea[data-composer-input]').first()
const sideComposer = (page) => page.locator('[data-side-chat-input][data-side-chat-slot="member-radar"] textarea[data-composer-input]')

// 01 — the toolbar over a member reply: Quote / Ask about this / Copy.
{
  const page = await newPage()
  await selectReply(page)
  // The toolbar is the popover holding the Ask button (the message footer has
  // its own Copy buttons, so count inside the popover, not page-wide).
  const toolbar = page.getByRole('button', { name: 'Ask about this', exact: true }).locator('xpath=ancestor::div[contains(@class,"z-[9999]")]')
  const labels = await toolbar.getByRole('button').allTextContents()
  check('01-toolbar three actions', labels.map(s => s.trim()).join(' / ') === 'Quote / Ask about this / Copy', `buttons=${labels.map(s => s.trim()).join(' / ')}`)
  await page.screenshot({ path: `${OUT}/01-toolbar-quote-ask-copy.png` })
  await page.close()
}

// 02 — Ask: the drawer becomes the member's Side Chat, seeded with the selection.
{
  const page = await newPage()
  await selectReply(page)
  await page.getByRole('button', { name: 'Ask about this', exact: true }).click()
  await page.getByTestId('member-side-chat').waitFor()
  const side = sideComposer(page)
  await side.waitFor()
  // The seed poll waits for THIS slot's Side Chat composer, then the listener
  // prefixes the selection as a blockquote.
  await page.waitForFunction(
    (sel) => (document.querySelector(sel)?.value || '').startsWith('> '),
    '[data-side-chat-input][data-side-chat-slot="member-radar"] textarea[data-composer-input]',
    { timeout: 5_000 },
  )
  const seeded = await side.inputValue()
  check('02-ask side chat seeded', seeded.includes('> Six new issues'), `side draft=${JSON.stringify(seeded.slice(0, 40))}`)
  check('02-ask thread composer untouched', (await threadComposer(page).inputValue()) === '', 'main draft empty')
  const title = await page.getByText('Side Chat', { exact: true }).count()
  check('02-ask drawer titled Side Chat', title >= 1, `titles=${title}`)
  await page.screenshot({ path: `${OUT}/02-ask-side-chat-seeded.png` })
  await page.close()
}

// 03 — Quote: the selection lands in the thread's own composer.
{
  const page = await newPage()
  await selectReply(page)
  await page.getByRole('button', { name: 'Quote', exact: true }).click()
  await page.waitForFunction(
    (sel) => (document.querySelector(sel)?.value || '').startsWith('> '),
    '[data-chat-pane] textarea[data-composer-input]',
    { timeout: 5_000 },
  )
  const draft = await threadComposer(page).inputValue()
  check('03-quote thread composer quoted', draft.includes('> Six new issues'), `draft=${JSON.stringify(draft.slice(0, 40))}`)
  check('03-quote no side chat opened', (await page.getByTestId('member-side-chat').count()) === 0, 'drawer stays on details')
  // Let the transit flight land before the frame.
  await page.waitForTimeout(900)
  await page.screenshot({ path: `${OUT}/03-quote-in-thread-composer.png` })
  await page.close()
}

// 04 — recording: the drawer swaps details → Side Chat on Ask (crossfade), and
// "Details" brings the member card back. Playwright names the video by a random
// id, so the new file is found by diffing the directory listing.
{
  const before = new Set(readdirSync(OUT).filter(f => f.endsWith('.webm')))
  const page = await newPage('dark', { width: 1440, height: 860 }, { recordVideo: { dir: OUT, size: { width: 1440, height: 860 } } })
  await page.waitForTimeout(600)
  await selectReply(page)
  await page.waitForTimeout(500)
  await page.getByRole('button', { name: 'Ask about this', exact: true }).click()
  await page.getByTestId('member-side-chat').waitFor()
  await sideComposer(page).waitFor()
  await page.waitForTimeout(1400)
  await page.getByTestId('member-drawer-details').click()
  await page.getByTestId('member-drawer').waitFor()
  await page.waitForTimeout(1000)
  const ctx = page.context()
  await page.close()
  await ctx.close() // flushes the video file
  const produced = readdirSync(OUT).filter(f => f.endsWith('.webm') && !before.has(f))
  check('04-drawer-swap recording produced', produced.length === 1, `new webm files=${produced.length}`)
  if (produced.length === 1) renameSync(join(OUT, produced[0]), join(OUT, '04-drawer-details-to-side-chat.webm'))
}

// 05 — the way back: after Ask seeded the Side Chat, "Details" returns to the
// member card and the header now carries a draft-marked **Side Chat** action
// (absent while the Side Chat holds nothing) — the control that lets a user
// who checked details mid-question get back to their text.
{
  const page = await newPage()
  // Before any Ask there is no draft, so no way back exists yet.
  check('05-return absent without a draft', (await page.getByTestId('member-drawer-side-chat').count()) === 0, 'no button before Ask')
  await selectReply(page)
  await page.getByRole('button', { name: 'Ask about this', exact: true }).click()
  await page.getByTestId('member-side-chat').waitFor()
  await sideComposer(page).waitFor()
  await page.waitForFunction(
    (sel) => (document.querySelector(sel)?.value || '').startsWith('> '),
    '[data-side-chat-input][data-side-chat-slot="member-radar"] textarea[data-composer-input]',
    { timeout: 5_000 },
  )
  await page.getByTestId('member-drawer-details').click()
  await page.getByTestId('member-drawer').waitFor()
  const back = page.getByTestId('member-drawer-side-chat')
  await back.waitFor({ state: 'visible', timeout: 5_000 })
  check('05-return button reads Side Chat', ((await back.textContent()) || '').trim() === 'Side Chat', `text=${JSON.stringify(await back.textContent())}`)
  check('05-return button explains itself', ((await back.getAttribute('title')) || '').includes('unsent question'), `title=${JSON.stringify(await back.getAttribute('title'))}`)
  check('05-return side chat unmounted', (await page.getByTestId('member-side-chat').count()) === 0, 'drawer shows details')
  // Crossfade out of the Side Chat view before the frame.
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/05-details-with-side-chat-return.png` })
  await page.close()
}

await browser.close()
if (failed()) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
