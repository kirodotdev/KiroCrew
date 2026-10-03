/**
 * Screenshot evidence for merging a fork back into its parent: the session-menu
 * entry, the dialog with its draft, the closed-parent refusal, and the card the
 * merge leaves in the parent, at desktop width, at phone width and in light.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures (gateway-free). The draft route answers
 * the way the handler does, with the counts the planner would compute for this
 * transcript, and the parent's detail carries the merge card exactly as the
 * note delivery writes it: an `inject` row whose meta holds `noteSession` and
 * `mergedFrom`.
 *
 * Frames:
 *   01-session-menu      the fork's right-click menu with "Merge into parent…"
 *   02-dialog-draft      the dialog with the drafted summary and its gap sentence
 *   03-parent-card       the parent chat showing the merge card
 *   04-parent-closed     a rejected closed-parent draft in ErrorNotice, with Ask the agent, Close and "Open the parent chat"
 *   05-dialog-phone      the dialog at 390px
 *   06-parent-card-light frame 03 in the light theme
 *   07-menu-row-disabled the menu on a fork still running, the row greyed with its reason
 *   08-dialog-drafting   the dialog while the summary is being written
 *   09-dialog-held       the dialog after merging into a parent that is mid-turn
 *   10-dialog-over-limit the text past the 4000-character limit, Merge disabled, the reason under the text
 *   11-dialog-redraft-confirm the question "Draft again" asks over edited text, Keep my text focused
 *   12-dialog-messages-remaining a draft that leaves newer fork messages waiting for the next merge, its label counting them in
 *   13-dialog-close-confirm the question a close asks over edited text, Keep my text focused
 *   14-dialog-draft-cut   a draft the gateway cut to the limit, with the line that says so under the text
 *   15-menu-row-incognito the menu on an incognito fork, the row greyed with why merging is off
 *   16-dialog-merging     the dialog while the merge is written: Merging… in the footer, no X, the text locked
 *   17-dialog-merge-refused a refused merge: the error under the kept, edited text, Merge off
 *   18-menu-row-channel-parent the menu on a fork of a Slack thread, the row greyed with the reason naming Slack
 *   19-menu-row-temporary the menu on a temporary fork, the row greyed with why merging is off
 *   20-parent-card-untitled the parent's merge card for a fork that had no title, so its label names "a fork"
 *   21-dialog-draft-retry a refused draft that can be tried again: the error, Close and "Draft again" in the footer
 *   22-dialog-nothing-to-merge the dialog when the parent already has everything: a muted status line and Close alone
 *   23-dialog-outdated-keep the question "Draft again" asks over edited text after a refused merge: keep the text under a new draft, or replace it
 *
 * Usage, from website/ after `npm run build`:
 *
 *   node scripts/capture-merge-back.mjs [--dist dist] [--out ../temp-screenshots/merge-back]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const args = process.argv.slice(2)
const flag = (name, fallback) => {
  const i = args.indexOf(name)
  return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : fallback
}
const DIST = resolve(flag('--dist', DEFAULT_DIST))
const OUT = resolve(flag('--out', '../temp-screenshots/merge-back'))

const PROJECT = '/home/user/workspace/upload-service'
const PARENT = 'chat-1-uploads'
const FORK = 'chat-2-uploads-fork'
const PARENT_TITLE = 'Speed up large uploads'
const FORK_TITLE = '↳ Fork of Speed up large uploads'
// The fork transcript's creation identity, which the merge card records.
const FORK_CREATED = '2026-10-02T18:00:00+00:00'
const SUMMARY = [
  'The fork tried resumable uploads to fix timeouts on files over 1 GB.',
  '',
  '- Chunked uploads with the `tus` protocol work: a 4 GB file survives a dropped connection and resumes from the last 8 MB chunk.',
  '- The server needs `tusd` behind the existing proxy; `client_max_body_size` stays at 10 MB because chunks are small.',
  '- Not verified yet: behaviour when two tabs upload the same file.',
].join('\n')
const GAP = 'If the parent chat moved on after this fork was made, the fork did not see those messages.'
/** `MAX_MERGE_SUMMARY_CHARS` in MergeBackDialog.tsx. */
const MAX_CHARS = 4000

/** The fixture summary's sentences, repeated until the text is past the limit:
 *  the over-limit frame should look like a real summary that ran long. */
function overLimitText() {
  const sentences = SUMMARY.split('\n').filter(Boolean).map(line => line.replace(/^- /, ''))
  const parts = []
  let length = 0
  for (let i = 0; length <= MAX_CHARS + 200; i++) {
    const sentence = sentences[i % sentences.length]
    parts.push(sentence)
    length += sentence.length + 1
  }
  return parts.join(' ')
}

/** A note that ran past the limit, as the gateway returns it: cut at a word
 *  boundary the way `_fit_summary` cuts, so it still nearly fills the limit. */
function gatewayCutText() {
  let cut = Array.from(overLimitText()).slice(0, MAX_CHARS - 1).join('')
  const boundary = Math.max(cut.lastIndexOf('\n'), cut.lastIndexOf(' '))
  if (boundary > MAX_CHARS / 2) cut = cut.slice(0, boundary)
  return cut.trimEnd() + '…'
}
const CUT_SUMMARY = gatewayCutText()

function scene({ forkRunning = false, parentRunning = false, forkMemoryMode = 'persistent', forkedFrom = `dashboard:${PARENT}`, mergedFromTitle = FORK_TITLE } = {}) {
  const now = Math.floor(Date.now() / 1000)
  const slots = [
    { key: PARENT, title: PARENT_TITLE, running: parentRunning, last_message: 'Plan: put tusd behind the proxy, then move the client to chunked uploads.', messages: 7, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT, modified: now, source_links: [], source_links_total: 0 },
    { key: FORK, title: FORK_TITLE, running: forkRunning, last_message: 'Resume works from the last chunk.', messages: 8, agent: 'kirocrew', memory_mode: forkMemoryMode, project: PROJECT, modified: now - 60, forked_from: forkedFrom, source_links: [], source_links_total: 0 },
  ]
  const copied = [
    { role: 'user', ts: now - 900, content: 'Uploads over 1 GB time out halfway. What are our options?', meta: { mid: 'p1' } },
    { role: 'assistant', ts: now - 890, content: 'Two options: raise the proxy timeouts, or switch to resumable chunked uploads so a dropped connection costs one chunk.', meta: { mid: 'p2' } },
  ]
  const fork = {
    running: false, has_more: false, total: 6, queue: [], project: PROJECT,
    messages: [
      ...copied,
      { role: 'user', ts: now - 600, content: 'Try the resumable route with tus.', meta: { mid: 'f1' } },
      { role: 'assistant', ts: now - 590, content: 'Chunked uploads with `tus` work: a 4 GB test file resumed from the last 8 MB chunk after I cut the connection.', meta: { mid: 'f2' } },
      { role: 'user', ts: now - 400, content: 'Does the proxy body limit still matter?', meta: { mid: 'f3' } },
      { role: 'assistant', ts: now - 390, content: 'No. Chunks are 8 MB, so `client_max_body_size` can stay at 10 MB. `tusd` sits behind the existing proxy.', meta: { mid: 'f4' } },
    ],
  }
  const parent = {
    running: false, has_more: false, total: 7, queue: [], project: PROJECT,
    messages: [
      ...copied,
      { role: 'user', ts: now - 500, content: 'Meanwhile, raise the proxy timeout to 10 minutes as a stopgap.', meta: { mid: 'p3' } },
      { role: 'assistant', ts: now - 490, content: 'Done: `proxy_read_timeout 600s` is set on the upload location.', meta: { mid: 'p4' } },
      {
        role: 'inject', ts: now - 30, content: `${SUMMARY}\n\n${GAP}`,
        meta: { mid: 'p5', noteSession: `dashboard:${PARENT}`, mergedFrom: { session: `dashboard:${FORK}`, slot: FORK, title: mergedFromTitle, createdAt: FORK_CREATED, through: 'f4', messages: 4 } },
      },
      { role: 'user', ts: now - 20, content: 'Good. Plan the switch to tus and keep the timeout until it ships.', meta: { mid: 'p6' } },
      { role: 'assistant', ts: now - 10, content: 'Plan: put `tusd` behind the proxy, move the client to chunked uploads, then drop the 10-minute timeout.', meta: { mid: 'p7' } },
    ],
  }
  return { slots, fork, parent }
}

async function bindRoutes(page, theme, data, { activeSlot, parentOpen = true, holdDraft = false, refuseDraft = '', refuseStatus = 502, cutDraft = false, deferMerge = false, holdMerge = false, refuseMerge = '', remaining = 0 }) {
  const fixedApi = makeFixedApi(PROJECT)
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/chat/slots') return json(route, parentOpen ? data.slots : data.slots.filter(s => s.key !== PARENT))
    if (path === `/api/chat/slots/${FORK}/merge-back/draft`) {
      if (!parentOpen) return json(route, { error: 'the parent chat is not open', code: 'parent_not_open' }, 409)
      // Never answered, so the dialog stays in its drafting state for its frame.
      if (holdDraft) return new Promise(() => {})
      // A refusal: 502 when the model's note failed, which drafting again can clear;
      // 409 when the parent already has everything, which it cannot.
      if (refuseDraft) return json(route, { error: refuseDraft === 'nothing_to_merge' ? 'nothing to merge' : 'the summary could not be written', code: refuseDraft }, refuseStatus)
      return json(route, { ok: true, summary: cutDraft ? CUT_SUMMARY : SUMMARY, through: 'f4', digest: 'ab'.repeat(32), messages: 4, remaining, trimmed: cutDraft, parent: PARENT })
    }
    if (path === `/api/chat/slots/${FORK}/merge-back`) {
      // Never answered, so the dialog stays in its merging state for its frame.
      if (holdMerge) return new Promise(() => {})
      if (refuseMerge) return json(route, { error: 'the fork changed since the draft', code: refuseMerge }, 409)
      if (deferMerge) return json(route, { ok: true, parent: PARENT, messages: 4, deferred: true })
    }
    if (path.startsWith(`/api/chat/slots/${PARENT}`)) return json(route, data.parent)
    if (path.startsWith(`/api/chat/slots/${FORK}`)) return json(route, data.fork)
    if (path === '/api/chat/folders') return json(route, [])
    return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  await page.addInitScript(([t, s]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
    localStorage.setItem('mc-active-slot-chat', s)
  }, [theme, activeSlot])
}

/** Every animation off, so an overlay is photographed at rest rather than on
 *  the transparent first frame of its entrance. */
async function freezeMotion(page) {
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}' })
}

async function openChat(page, base, text) {
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(`text=${text}`, { timeout: 20000 })
  await page.waitForTimeout(1200)
  await freezeMotion(page)
}

async function openForkMenu(page) {
  await page.getByText(FORK_TITLE).first().click({ button: 'right' })
  await mustSee(page, '[data-testid="merge-back"]')
}

/** The row on a fork that can merge: enabled, with no reason beside it. */
async function openEnabledForkMenu(page) {
  await openForkMenu(page)
  const row = page.locator('[data-testid="merge-back"]')
  if (await row.getAttribute('aria-disabled') === 'true') throw new Error('the Merge into parent… row is disabled')
}

async function openDialogFromMenu(page) {
  await openEnabledForkMenu(page)
  await page.locator('[data-testid="merge-back"]').click()
}

async function showParentCard(page) {
  const label = await mustSee(page, '[data-testid="merged-from-label"]')
  // The label a little below the top edge, where the row above it is
  // remeasured after the scroll and can paint over it, and the whole card
  // above the composer, which a centred scroll puts it behind.
  await label.evaluate(el => {
    el.scrollIntoView({ block: 'start' })
    let scroller = el.parentElement
    while (scroller && scroller.scrollHeight <= scroller.clientHeight) scroller = scroller.parentElement
    scroller?.scrollBy(0, -90)
  })
  await page.waitForTimeout(250)
  const covered = await label.evaluate(el => {
    const r = el.getBoundingClientRect()
    const hit = document.elementFromPoint(r.left + 10, r.top + r.height / 2)
    return !el.contains(hit)
  })
  if (covered) throw new Error('something paints over the merge card label')
}

/** Wait for the draft in the text box, not a timer: a frame taken first would
 *  show the dialog still drafting and pass it off as the finished state. The
 *  text must end with the gap sentence, the way the dialog opens it. */
async function waitForDraft(page) {
  await page.locator('[data-testid="merge-back-text"]').waitFor({ timeout: 10000 })
  await page.waitForFunction(() => (document.querySelector('[data-testid="merge-back-text"]')?.value || '').includes('resumable'))
  const text = await page.locator('[data-testid="merge-back-text"]').inputValue()
  if (!text.endsWith(GAP)) throw new Error(`draft text does not end with the gap sentence: ${JSON.stringify(text.slice(-120))}`)
  await page.waitForTimeout(250)
}

const EDIT = '\n\nTwo tabs uploading the same file is still open.'

/** The draft with a sentence of the person's own at its end, so a redraft or a
 *  close has something to lose. */
async function editDraft(page) {
  const box = page.locator('[data-testid="merge-back-text"]')
  await box.fill(`${await box.inputValue()}${EDIT}`)
}

/** The edited text is still in the box: nothing replaced or dropped it. */
async function mustKeepEdit(page) {
  const text = await page.locator('[data-testid="merge-back-text"]').inputValue()
  if (!text.endsWith('is still open.')) throw new Error('the edited text is gone')
}

/** The frame's state must be on screen, or the frame is not taken: a still of
 *  the wrong state would pass as evidence of the right one. */
async function mustSee(page, selector, text) {
  const el = page.locator(selector, text === undefined ? {} : { hasText: text }).first()
  await el.waitFor({ timeout: 10000 })
  if (!(await el.isVisible())) throw new Error(`${selector} is not visible`)
  return el
}

/** The refusal is a state, not a failure: a status line, no danger colour, no warning triangle. */
async function mustBeStateLine(page) {
  const line = page.locator('[data-testid="merge-back-refused"]')
  if (await line.getAttribute('role') !== 'status') throw new Error('the refusal is not a status line')
  if (await line.locator('.lucide-triangle-alert').count() !== 0) throw new Error('the refusal carries a warning triangle')
  if (await line.locator('.lucide-info').count() !== 1) throw new Error('the refusal has no info mark')
  const colour = await line.evaluate(el => getComputedStyle(el).color)
  const danger = await page.evaluate(() => {
    const probe = document.createElement('span')
    probe.className = 'text-danger'
    document.body.append(probe)
    const c = getComputedStyle(probe).color
    probe.remove()
    return c
  })
  if (colour === danger) throw new Error('the refusal is drawn in the danger colour')
}

async function shoot(browser, base, { theme = 'dark', viewport = { width: 1180, height: 760 }, activeSlot = FORK, parentOpen = true, waitText, forkRunning, parentRunning, forkMemoryMode, forkedFrom, mergedFromTitle, holdDraft, refuseDraft, refuseStatus, cutDraft, deferMerge, holdMerge, refuseMerge, remaining }, steps) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1 })
  const page = await context.newPage()
  await bindRoutes(page, theme, scene({ forkRunning, parentRunning, forkMemoryMode, forkedFrom, mergedFromTitle }), { activeSlot, parentOpen, holdDraft, refuseDraft, refuseStatus, cutDraft, deferMerge, holdMerge, refuseMerge, remaining })
  await openChat(page, base, waitText)
  for (const [name, step] of steps) {
    await step(page)
    const file = join(OUT, `${name}.png`)
    await page.screenshot({ path: file })
    console.log('wrote', file)
  }
  await context.close()
}

mkdirSync(OUT, { recursive: true })
const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
try {
  await shoot(browser, base, { waitText: 'Try the resumable route with tus.' }, [
    ['01-session-menu', openEnabledForkMenu],
    ['02-dialog-draft', async page => {
      await page.locator('[data-testid="merge-back"]').click()
      await waitForDraft(page)
    }],
  ])
  await shoot(browser, base, { activeSlot: PARENT, waitText: 'Plan the switch to tus' }, [
    ['03-parent-card', showParentCard],
  ])
  await shoot(browser, base, { parentOpen: false, waitText: 'Try the resumable route with tus.' }, [
    ['04-parent-closed', async page => {
      await openDialogFromMenu(page)
      const notice = await mustSee(page, '[data-testid="merge-back-refused"]', 'The parent chat is closed.')
      await mustSee(page, '[data-testid="merge-back-reopen-parent"]', 'Open the parent chat')
      await page.getByRole('button', { name: 'Ask the agent' }).waitFor({ timeout: 10000 })
      // The rejected draft uses ErrorNotice and keeps the direct recovery action.
      if (await notice.getAttribute('role') !== 'alert') throw new Error('the closed-parent refusal is not an alert')
      if (await notice.locator('.lucide-triangle-alert').count() !== 1) throw new Error('the closed-parent refusal has no warning triangle')
      if (await notice.locator('.lucide-info').count() !== 0) throw new Error('the closed-parent refusal still has an info mark')
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { viewport: { width: 390, height: 780 }, waitText: 'Try the resumable route with tus.' }, [
    ['05-dialog-phone', async page => {
      // The phone layout hides the sidebar behind its drawer; the session
      // header's own menu carries the same session entries.
      await page.getByRole('button', { name: /Session options/ }).first().click({ timeout: 10000 })
      await page.locator('[data-testid="merge-back"]').click()
      await waitForDraft(page)
    }],
  ])
  await shoot(browser, base, { theme: 'light', activeSlot: PARENT, waitText: 'Plan the switch to tus' }, [
    ['06-parent-card-light', showParentCard],
  ])
  await shoot(browser, base, { forkRunning: true, waitText: 'Try the resumable route with tus.' }, [
    ['07-menu-row-disabled', async page => {
      await openForkMenu(page)
      const row = page.locator('[data-testid="merge-back"]')
      if (await row.getAttribute('aria-disabled') !== 'true') throw new Error('the Merge into parent… row is not disabled')
      await mustSee(page, '[data-testid="merge-back"]', 'A turn is still running')
    }],
  ])
  await shoot(browser, base, { holdDraft: true, waitText: 'Try the resumable route with tus.' }, [
    ['08-dialog-drafting', async page => {
      await openDialogFromMenu(page)
      await mustSee(page, '[data-testid="merge-back-drafting"]', 'Writing a summary of the new messages…')
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { parentRunning: true, deferMerge: true, waitText: 'Try the resumable route with tus.' }, [
    ['09-dialog-held', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      await page.locator('[data-testid="merge-back-merge"]').click()
      await mustSee(page, '[data-testid="merge-back-held"]', 'Merged. The parent chat is in the middle of a turn, so the summary appears there when that turn ends.')
      await mustSee(page, '[data-testid="merge-back-close"]')
      await mustSee(page, '[data-testid="merge-back-open-parent"]')
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { waitText: 'Try the resumable route with tus.' }, [
    ['10-dialog-over-limit', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      const text = overLimitText()
      await page.locator('[data-testid="merge-back-text"]').fill(text)
      const count = page.locator('[data-testid="merge-back-count"]')
      await count.filter({ hasText: `${text.length} / ${MAX_CHARS}` }).waitFor({ timeout: 10000 })
      if (!/\btext-danger\b/.test(await count.getAttribute('class') || '')) throw new Error('the counter is not in text-danger')
      if (!(await page.locator('[data-testid="merge-back-merge"]').isDisabled())) throw new Error('Merge is not disabled over the limit')
      const reason = await mustSee(page, '[data-testid="merge-back-too-long"]', 'The summary is over the length limit. Shorten it.')
      if (!/\btext-danger\b/.test(await reason.getAttribute('class') || '')) throw new Error('the over-limit reason is not in text-danger')
      const boxRect = await page.locator('[data-testid="merge-back-text"]').boundingBox()
      const reasonRect = await reason.boundingBox()
      if (!boxRect || !reasonRect || reasonRect.y < boxRect.y + boxRect.height) throw new Error('the reason is not under the text box')
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { waitText: 'Try the resumable route with tus.' }, [
    ['11-dialog-redraft-confirm', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      await editDraft(page)
      await page.locator('[data-testid="merge-back-redraft"]').click()
      await mustSee(page, '[data-testid="merge-back-redraft-confirm"]', 'Replace your edited text with a new draft?')
      await mustSee(page, '[data-testid="merge-back-redraft-replace"]', 'Replace')
      const keep = await mustSee(page, '[data-testid="merge-back-redraft-keep"]', 'Keep my text')
      if (!(await keep.evaluate(el => el === document.activeElement))) throw new Error('Keep my text does not hold the focus')
      // The question replaced nothing before the person answered.
      await mustKeepEdit(page)
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { remaining: 3, waitText: 'Try the resumable route with tus.' }, [
    ['12-dialog-messages-remaining', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      await mustSee(page, '[data-testid="merge-back-remaining"]', '3 more messages did not fit in this draft. Merge again after this one to send them.')
      const covers = await mustSee(page, 'label', 'Summarizes the first 4 of 7 messages not merged yet')
      const remaining = page.locator('[data-testid="merge-back-remaining"]')
      const coversBox = await covers.boundingBox()
      const remainingBox = await remaining.boundingBox()
      if (!coversBox || !remainingBox || remainingBox.y <= coversBox.y) {
        throw new Error('the remaining-message line is not below the covered-message line')
      }
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { waitText: 'Try the resumable route with tus.' }, [
    ['13-dialog-close-confirm', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      await editDraft(page)
      await page.keyboard.press('Escape')
      await mustSee(page, '[data-testid="merge-back-close-confirm"]', 'Close and lose your edited text?')
      await mustSee(page, '[data-testid="merge-back-close-discard"]', 'Discard and close')
      const keep = await mustSee(page, '[data-testid="merge-back-close-keep"]', 'Keep my text')
      if (!(await keep.evaluate(el => el === document.activeElement))) throw new Error('Keep my text does not hold the focus')
      // The dialog is still open with the text: Escape asked instead of closing.
      await mustSee(page, '[data-testid="merge-back-text"]')
      await mustKeepEdit(page)
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { cutDraft: true, waitText: 'Try the resumable route with tus.' }, [
    ['14-dialog-draft-cut', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      const text = await page.locator('[data-testid="merge-back-text"]').inputValue()
      if (!text.startsWith(SUMMARY.split('\n')[0])) throw new Error('the text does not open with the drafted summary')
      if (!text.endsWith(`…\n\n${GAP}`)) throw new Error('the text does not end with the cut summary and the gap sentence')
      if (Array.from(text).length > MAX_CHARS) throw new Error('the cut text does not fit the limit')
      const line = await mustSee(page, '[data-testid="merge-back-cut"]', 'The summary was cut to fit the limit. It ends with “…” just above the closing note. Check that line before you merge.')
      const boxRect = await page.locator('[data-testid="merge-back-text"]').boundingBox()
      const lineRect = await line.boundingBox()
      if (!boxRect || !lineRect || lineRect.y < boxRect.y + boxRect.height) throw new Error('the cut line is not under the text box')
      // Show the end of the text, where the cut is: the frame is evidence of
      // what the line points at.
      await page.locator('[data-testid="merge-back-text"]').evaluate(el => { el.scrollTop = el.scrollHeight })
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { forkMemoryMode: 'incognito', waitText: 'Try the resumable route with tus.' }, [
    ['15-menu-row-incognito', async page => {
      await openForkMenu(page)
      const row = page.locator('[data-testid="merge-back"]')
      if (await row.getAttribute('aria-disabled') !== 'true') throw new Error('the Merge into parent… row is not disabled')
      await mustSee(page, '[data-testid="merge-back"]', 'Nothing leaves an incognito chat')
    }],
  ])
  await shoot(browser, base, { holdMerge: true, waitText: 'Try the resumable route with tus.' }, [
    ['16-dialog-merging', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      await page.locator('[data-testid="merge-back-merge"]').click()
      await mustSee(page, '[data-testid="merge-back-merge"]', 'Merging…')
      if (!(await page.locator('[data-testid="merge-back-text"]').isDisabled())) throw new Error('the text is not locked while merging')
      if (!(await page.locator('[data-testid="merge-back-cancel"]').isDisabled())) throw new Error('Cancel is not off while merging')
      if (await page.locator('[role="dialog"] button[aria-label="Close"]').count() !== 0) throw new Error('the X is still there while merging')
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { refuseMerge: 'merge_draft_stale', waitText: 'Try the resumable route with tus.' }, [
    ['17-dialog-merge-refused', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      await editDraft(page)
      await page.locator('[data-testid="merge-back-merge"]').click()
      const error = await mustSee(page, '[data-testid="merge-back-error"]', 'This fork changed since the summary was written')
      // The edited text is kept and Merge is off: the same draft could only be refused again.
      await mustKeepEdit(page)
      if (!(await page.locator('[data-testid="merge-back-merge"]').isDisabled())) throw new Error('Merge is not off after the refusal')
      const boxRect = await page.locator('[data-testid="merge-back-text"]').boundingBox()
      const errorRect = await error.boundingBox()
      if (!boxRect || !errorRect || errorRect.y <= boxRect.y) throw new Error('the error is not under the text')
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { forkedFrom: 'slack:1700000000.000001', parentOpen: false, waitText: 'Try the resumable route with tus.' }, [
    ['18-menu-row-channel-parent', async page => {
      await openForkMenu(page)
      const row = page.locator('[data-testid="merge-back"]')
      if (await row.getAttribute('aria-disabled') !== 'true') throw new Error('the Merge into parent… row is not disabled')
      await mustSee(page, '[data-testid="merge-back"]', 'The parent is a Slack chat')
    }],
  ])
  await shoot(browser, base, { forkMemoryMode: 'temporary', waitText: 'Try the resumable route with tus.' }, [
    ['19-menu-row-temporary', async page => {
      await openForkMenu(page)
      const row = page.locator('[data-testid="merge-back"]')
      if (await row.getAttribute('aria-disabled') !== 'true') throw new Error('the Merge into parent… row is not disabled')
      await mustSee(page, '[data-testid="merge-back"]', 'Nothing leaves a temporary chat')
    }],
  ])
  await shoot(browser, base, { activeSlot: PARENT, mergedFromTitle: '', waitText: 'Plan the switch to tus' }, [
    ['20-parent-card-untitled', async page => {
      await showParentCard(page)
      await mustSee(page, '[data-testid="merged-from-label"]', 'Merged from a fork')
      await mustSee(page, '[data-testid="merged-from-label"]', '4 messages')
    }],
  ])
  await shoot(browser, base, { refuseDraft: 'merge_summary_failed', waitText: 'Try the resumable route with tus.' }, [
    ['21-dialog-draft-retry', async page => {
      await openDialogFromMenu(page)
      await mustSee(page, '[data-testid="merge-back-refused"]', 'The summary could not be written. Try again.')
      await mustSee(page, '[data-testid="merge-back-cancel"]', 'Close')
      await mustSee(page, '[data-testid="merge-back-retry"]', 'Draft again')
      // A retryable refusal: no reopen, and nothing to merge yet.
      if (await page.locator('[data-testid="merge-back-reopen-parent"]').count() !== 0) throw new Error('a reopen button is offered for a refusal that is not about an open parent')
      if (await page.locator('[data-testid="merge-back-merge"]').count() !== 0) throw new Error('Merge is offered with no draft on screen')
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { refuseDraft: 'nothing_to_merge', refuseStatus: 409, waitText: 'Try the resumable route with tus.' }, [
    ['22-dialog-nothing-to-merge', async page => {
      await openDialogFromMenu(page)
      await mustSee(page, '[data-testid="merge-back-refused"]', 'The parent chat already has everything in this fork.')
      await mustBeStateLine(page)
      await mustSee(page, '[data-testid="merge-back-cancel"]', 'Close')
      // Nothing to do, so nothing else to press: no retry, no reopen, no merge, no agent hand-off.
      for (const id of ['merge-back-retry', 'merge-back-reopen-parent', 'merge-back-merge']) {
        if (await page.locator(`[data-testid="${id}"]`).count() !== 0) throw new Error(`${id} is offered when the parent already has everything`)
      }
      if (await page.getByRole('button', { name: 'Ask the agent' }).count() !== 0) throw new Error('Ask the agent is offered when the parent already has everything')
      await page.waitForTimeout(250)
    }],
  ])
  await shoot(browser, base, { refuseMerge: 'merge_draft_stale', waitText: 'Try the resumable route with tus.' }, [
    ['23-dialog-outdated-keep', async page => {
      await openDialogFromMenu(page)
      await waitForDraft(page)
      await editDraft(page)
      await page.locator('[data-testid="merge-back-merge"]').click()
      await mustSee(page, '[data-testid="merge-back-error"]', 'This fork changed since the summary was written')
      await page.locator('[data-testid="merge-back-redraft"]').click()
      // Over an outdated draft the question is a different one: keeping the text
      // drafts again behind it, so its keep answer says so instead of "Keep my text".
      await mustSee(page, '[data-testid="merge-back-redraft-outdated-confirm"]', 'The chats changed since this draft was written, so Merge needs a new draft. Keep your text as it is, or replace it with the new draft?')
      if (await page.locator('[data-testid="merge-back-redraft-confirm"]').count() !== 0) throw new Error('the usual Draft again question is asked over an outdated draft')
      // The question states the refusal itself, so the notice is not shown twice over.
      if (await page.locator('[data-testid="merge-back-error"]').count() !== 0) throw new Error('the refusal notice is shown beside the question that states it')
      const keep = await mustSee(page, '[data-testid="merge-back-redraft-outdated-keep"]', 'Draft again, keep my text')
      if (!(await keep.evaluate(el => el === document.activeElement))) throw new Error('the keep answer does not hold the focus')
      await mustSee(page, '[data-testid="merge-back-redraft-replace"]', 'Replace')
      // Asking replaced nothing, and Merge is still off until the person answers.
      await mustKeepEdit(page)
      if (!(await page.locator('[data-testid="merge-back-merge"]').isDisabled())) throw new Error('Merge is on before the draft was drafted again')
      await page.waitForTimeout(250)
    }],
  ])
} finally {
  await browser.close()
  srv.close()
}
