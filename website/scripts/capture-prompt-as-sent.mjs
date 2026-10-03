/**
 * Screenshot harness for the "Prompt as sent" section of the chat side-panel
 * Context tab (Developer Mode).
 *
 * Runs the REAL built SPA (website/dist) through the shared transcript harness:
 * a static server over dist, /api/** answered from fixtures, /api/ws bound so
 * the app's socket does not hang. No gateway, no kiro-cli — only the network is
 * stubbed, so the panel, the segment bar and the disclosure rows render exactly
 * as they do in production.
 *
 * The two fixtures are read from files so they can be produced by the BACKEND's
 * own record path (`kiro_crew.prompt_trace.announce_user_span` + `record`,
 * whose spans come from `context_blocks.block_spans`) rather than hand-typed:
 *   <fixtureDir>/context-trace.json          -- GET /api/telemetry/context-trace body
 *   <fixtureDir>/prompt-trace.json           -- GET /api/telemetry/prompt-trace body
 *   <fixtureDir>/prompt-trace-uncarved.json  -- the same prompts recorded with no
 *                                               announced user span (optional)
 *   <fixtureDir>/prompt-trace-redacted.json + context-trace-redacted.json
 *                                            -- a fifth turn whose message held a
 *                                               credential, served scrubbed (optional)
 *
 * Usage:  node scripts/capture-prompt-as-sent.mjs <fixtureDir> [outDir]
 * Output: <outDir> || $KIROCREW_SCRATCH || os.tmpdir()/prompt-as-sent-cap/
 *   prompt-as-sent.png (section collapsed), prompt-as-sent-expanded.png
 */
import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const SLOT = 'chat-1'
const PROJECT = '/home/user/workspace/KiroCrew'
const FIXTURES = process.argv[2]
if (!FIXTURES) {
  console.error('usage: node scripts/capture-prompt-as-sent.mjs <fixtureDir> [outDir]')
  process.exit(2)
}
const OUT = process.argv[3] || join(process.env.KIROCREW_SCRATCH || tmpdir(), 'prompt-as-sent-cap')
mkdirSync(OUT, { recursive: true })

const CONTEXT_TRACE = JSON.parse(readFileSync(join(FIXTURES, 'context-trace.json'), 'utf8'))
const PROMPT_TRACE = JSON.parse(readFileSync(join(FIXTURES, 'prompt-trace.json'), 'utf8'))
const UNCARVED_PATH = join(FIXTURES, 'prompt-trace-uncarved.json')
const PROMPT_TRACE_UNCARVED = existsSync(UNCARVED_PATH) ? JSON.parse(readFileSync(UNCARVED_PATH, 'utf8')) : null
const REDACTED_PATH = join(FIXTURES, 'prompt-trace-redacted.json')
const PROMPT_TRACE_REDACTED = existsSync(REDACTED_PATH) ? JSON.parse(readFileSync(REDACTED_PATH, 'utf8')) : null
const CONTEXT_TRACE_REDACTED = existsSync(join(FIXTURES, 'context-trace-redacted.json'))
  ? JSON.parse(readFileSync(join(FIXTURES, 'context-trace-redacted.json'), 'utf8'))
  : null

const now = () => Date.now() / 1000
const slots = [{
  key: SLOT,
  title: 'See the prompt each turn sends to the AI',
  running: false,
  last_message: 'Add outbound recording and show it in the Context tab.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(now()),
  source_links: [],
  source_links_total: 0,
}]
const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: now() - 600, content: 'Add outbound recording and show it in the Context tab.' },
    { role: 'assistant', ts: now() - 300, content: 'Done. Open the Context tab in Developer Mode.' },
  ],
}

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
    viewport: { width: 640, height: 1180 },
    deviceScaleFactor: 2,
  })
  const { page } = h

  await page.route('**/api/telemetry/context-trace**', route => json(route, CONTEXT_TRACE))
  await page.route('**/api/telemetry/prompt-trace**', route => json(route, PROMPT_TRACE))

  await h.load('dark')

  // Developer Mode on, side panel open on a Context tab; reload so the SPA boots
  // straight into the view (this init script runs LAST on the next navigation,
  // so it wins over the harness's localStorage.clear()).
  await page.addInitScript(([slot]) => {
    localStorage.setItem('mc-dev-mode', '1')
    localStorage.setItem('mc-activity-open:' + slot, 'true')
    localStorage.setItem(
      'mc-panel-tabs:' + slot,
      JSON.stringify({ activeId: 'context', tabs: [{ id: 'context', kind: 'context', title: 'Context' }] }),
    )
  }, [SLOT])
  await page.reload({ waitUntil: 'domcontentloaded' })

  const section = page.getByTestId('prompt-as-sent')
  await section.waitFor({ timeout: 20000 })
  // Park the pointer off the chart before every frame: a hover highlight left
  // by the last click reads as a stray mark to a cold reader.
  const shot = async name => {
    // The side panel scrolls: bring the section's LAST row into view first so
    // the whole section, heading to last row, is what the frame shows, then
    // prove it — a row outside the viewport was never cold-read by anyone.
    const rows = section.locator('[data-prompt-segment]')
    if (await rows.count()) await rows.last().scrollIntoViewIfNeeded()
    else await section.scrollIntoViewIfNeeded()
    await page.waitForTimeout(150)
    const hidden = await page.evaluate(() => {
      const sec = document.querySelector('[data-testid="prompt-as-sent"]')
      if (!sec) return 'no section'
      // A long unbroken token can scroll an ancestor sideways; a frame shot
      // that way clips the left column. Park every ancestor at x = 0 first.
      for (let el = sec; el; el = el.parentElement) if (el.scrollLeft) el.scrollLeft = 0
      const vh = window.innerHeight
      const vw = window.innerWidth
      const out = []
      for (const el of [sec.querySelector('strong'), ...sec.querySelectorAll('[data-prompt-segment]')]) {
        const r = el.getBoundingClientRect()
        if (r.top < 0 || r.bottom > vh || r.left < 0 || r.right > vw) {
          out.push(`${(el.textContent || '').slice(0, 30)} @ top=${Math.round(r.top)} bottom=${Math.round(r.bottom)} left=${Math.round(r.left)} right=${Math.round(r.right)} (vw=${vw}, vh=${vh})`)
        }
      }
      return out.length ? JSON.stringify(out) : ''
    })
    if (hidden) throw new Error(`${name}: section parts outside the viewport: ${hidden}`)
    await page.mouse.move(0, 0)
    await page.waitForTimeout(150)
    await page.screenshot({ path: join(OUT, name) })
  }
  // No first-run gate may sit on top of the frame.
  const dialogs = await page.locator('[role="dialog"]').count()
  if (dialogs) throw new Error(`unexpected dialog open: ${dialogs}`)
  await section.scrollIntoViewIfNeeded()
  await page.waitForTimeout(600)

  const body = await page.locator('body').innerText()
  // The backend carved the user's span, so the message has a row of its own
  // and neither the helper nor the header row claims it.
  const required = ['Prompt as sent', 'Copy all', 'Rules · Must-follow rules (built-in)', 'How to format replies (built-in) · 1 of 2', 'Text available · open it under Prompt as sent', 'in the order they were sent — the bar shows them to scale', 'the pieces below, added up', 'Your message', "Which app you're using (built-in)", 'Header added before your message (built-in)', 'How to format replies (built-in) · 2 of 2']
  const dots = await page.locator('[data-prompt-dot]').count()
  if (dots < 1) throw new Error('no prompt dots rendered')
  if ((await page.locator('[data-start-row] [data-prompt-dot]').count()) < 1) throw new Error('session-start row carries no dot')
  console.log('prompt dots:', dots)
  const missing = required.filter(t => !body.includes(t))
  if (missing.length) throw new Error(`assert failed: missing=${JSON.stringify(missing)}\n${body.slice(0, 2500)}`)
  if (body.includes('+ your message')) throw new Error('header row claims the message although it has a row of its own')
  console.log('ASSERT OK: section heading, helper, copy button, grouped block rows, user row and ordinal present')

  await shot('prompt-as-sent.png')
  console.log('wrote', join(OUT, 'prompt-as-sent.png'))

  // Expand the user's own row and the rules row: the text behind a segment is
  // the whole point of the section.
  await section.locator('[data-prompt-segment="your_message"] button').click()
  await section.locator('[data-prompt-segment="critical_rules"] button').click()
  await page.waitForTimeout(400)
  const expanded = await section.innerText()
  if (!expanded.includes('CRITICAL RULES')) throw new Error('expanded text not visible:\n' + expanded.slice(0, 1500))
  // The newest turn is the one selected on load. Offsets are code points; the
  // fixture text is BMP-only, so slice() reads them directly.
  const selected = PROMPT_TRACE.turns[PROMPT_TRACE.turns.length - 1]
  const userSpan = selected.spans.find(sp => sp.label === 'your_message')
  if (!expanded.includes(selected.text.slice(userSpan.start, userSpan.end))) throw new Error('the user row does not open to the user text:\n' + expanded.slice(0, 1500))
  await section.scrollIntoViewIfNeeded()
  await shot('prompt-as-sent-expanded.png')
  console.log('wrote', join(OUT, 'prompt-as-sent-expanded.png'))

  // A turn whose prompt fell out of the ring (or predates a gateway restart):
  // the section says so instead of showing a neighbour's text.
  await page.locator('button[data-turn="2"]').click()
  await page.waitForTimeout(400)
  const none = await section.innerText()
  if (!none.includes('Not kept: no prompt text is held for this turn')) throw new Error('expected the none state:\n' + none.slice(0, 800))
  await section.scrollIntoViewIfNeeded()
  await shot('prompt-as-sent-none.png')
  console.log('wrote', join(OUT, 'prompt-as-sent-none.png'))

  const newestRec = PROMPT_TRACE.turns[PROMPT_TRACE.turns.length - 1]
  // A member session's acknowledged turn: sized before the essentials receipt
  // was dropped on the wire, so assembled > sent. Not a wrong match; the
  // section says which number is which instead of warning.
  const swapped = {
    ...PROMPT_TRACE,
    turns: PROMPT_TRACE.turns.map((t, i, arr) => (i === arr.length - 1 ? { ...t, assembled_chars: t.chars + 812 } : t)),
  }
  // The usage row was sized from the ASSEMBLED prompt, so its blocks sum to the
  // assembled total: the skipped part (the essentials receipt) was rules text.
  const swappedTrace = {
    ...CONTEXT_TRACE,
    turns: CONTEXT_TRACE.turns.map((t, i, arr) =>
      i === arr.length - 1
        ? {
            ...t,
            total_chars: newestRec.chars + 812,
            blocks: { ...t.blocks, critical_rules: (t.blocks.critical_rules ?? 0) + 812 },
          }
        : t,
    ),
  }
  await page.route('**/api/telemetry/context-trace**', route => json(route, swappedTrace))
  await page.route('**/api/telemetry/prompt-trace**', route => json(route, swapped))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await section.waitFor({ timeout: 20000 })
  await page.getByTestId('prompt-assembled-vs-sent').waitFor({ timeout: 10000 })
  // Cause first, then the two numbers: the benign note must not share its shape
  // with the wrong-match warning, which is also "two numbers disagree".
  const assembledNote = await page.getByTestId('prompt-assembled-vs-sent').innerText()
  if (!/^Built [\d,.\u202f\u00a0]+ · sent [\d,.\u202f\u00a0]+; [\d,.\u202f\u00a0]+ characters were reused from an earlier turn and not repeated\. The totals above count what was built\./.test(assembledNote)) {
    throw new Error('assembled-vs-sent note must say what happened in the reader\'s words:\n' + assembledNote)
  }
  await section.scrollIntoViewIfNeeded()
  await shot('prompt-as-sent-assembled.png')
  console.log('wrote', join(OUT, 'prompt-as-sent-assembled.png'))
  await page.route('**/api/telemetry/context-trace**', route => json(route, CONTEXT_TRACE))
  await page.route('**/api/telemetry/prompt-trace**', route => json(route, PROMPT_TRACE))

  // The uncarved variant: a prompt recorded with no announced user span, so the
  // message sits inside the request header and both the helper and that row say so.
  if (PROMPT_TRACE_UNCARVED) {
    await page.route('**/api/telemetry/prompt-trace**', route => json(route, PROMPT_TRACE_UNCARVED))
    await page.reload({ waitUntil: 'domcontentloaded' })
    await section.waitFor({ timeout: 20000 })
    const uncarved = await section.innerText()
    for (const t of [/Header added before your message \(built-in\) \+ your message · [\d,.\u202f\u00a0]+ of its [\d,.\u202f\u00a0]+ characters are your message/, 'in the order they were sent — the bar shows them to scale']) {
      if (!(typeof t === 'string' ? uncarved.includes(t) : t.test(uncarved))) throw new Error(`uncarved state missing "${t}":\n` + uncarved.slice(0, 1200))
    }
    // The summary's "Your message" total names its count in the pointer; with the
    // row suffix that is the whole telling (the helper no longer repeats it).
    const userTotal = await page.locator('[data-category-row="message"]').innerText()
    if (!/no row of its own below — your message wasn't marked separately in this turn; its [\d,.\u202f\u00a0]+ characters sit inside Header added before your message \(built-in\)/.test(userTotal)) {
      throw new Error('uncarved "Your message" total must carry its count:\n' + userTotal)
    }
    if (await section.locator('[data-prompt-segment="your_message"]').count()) throw new Error('uncarved record must not have a user row')
    await section.scrollIntoViewIfNeeded()
    await shot('prompt-as-sent-uncarved.png')
    console.log('wrote', join(OUT, 'prompt-as-sent-uncarved.png'))
    await page.route('**/api/telemetry/prompt-trace**', route => json(route, PROMPT_TRACE))
  }

  // A turn whose message held a credential: the ring keeps it, the endpoint
  // serves it scrubbed, and the section says so — decision-critical copy, since
  // a developer might otherwise act on the masked value as what the model saw.
  if (PROMPT_TRACE_REDACTED && CONTEXT_TRACE_REDACTED) {
    await page.route('**/api/telemetry/context-trace**', route => json(route, CONTEXT_TRACE_REDACTED))
    await page.route('**/api/telemetry/prompt-trace**', route => json(route, PROMPT_TRACE_REDACTED))
    await page.reload({ waitUntil: 'domcontentloaded' })
    await section.waitFor({ timeout: 20000 })
    await page.getByTestId('prompt-redacted').waitFor({ timeout: 10000 })
    await section.locator('[data-prompt-segment="your_message"] button').click()
    await page.waitForTimeout(300)
    const redactedText = await section.innerText()
    if (!redactedText.includes('[REDACTED: credential]')) throw new Error('the masked value is not visible in the opened row')
    if (!redactedText.includes('masked here, and Copy all copies the masked text; the AI received the original. The counts below measure the masked text, so a piece\'s count can differ slightly from the totals above, which measure the original.')) throw new Error('the redaction note must put the Copy all consequence first and say which counts are masked-length:\n' + redactedText.slice(0, 600))
    if (redactedText.includes('AKIA')) throw new Error('a credential reached the frame')
    await shot('prompt-as-sent-redacted.png')
    console.log('wrote', join(OUT, 'prompt-as-sent-redacted.png'))
    await page.route('**/api/telemetry/context-trace**', route => json(route, CONTEXT_TRACE))
    await page.route('**/api/telemetry/prompt-trace**', route => json(route, PROMPT_TRACE))
  }

  // The prompt-trace read failing: its own notice, so "could not load" is not
  // mistaken for "not kept". The chart above still renders from its own trace.
  await page.route('**/api/telemetry/prompt-trace**', route =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'prompt trace unavailable' }) }),
  )
  await page.reload({ waitUntil: 'domcontentloaded' })
  // Its own testid: a text wait on "500" is satisfied by the chart's y-axis tick.
  const notice = page.getByTestId('prompt-trace-error')
  await notice.waitFor({ timeout: 20000 })
  if (await page.getByTestId('prompt-as-sent').count()) throw new Error('the section must not render while its read failed')
  await notice.scrollIntoViewIfNeeded()
  const noticeBox = await notice.boundingBox()
  const vp = page.viewportSize()
  if (!noticeBox || noticeBox.y < 0 || noticeBox.y + noticeBox.height > vp.height) throw new Error('the error notice is not inside the frame')
  await page.mouse.move(0, 0)
  await page.waitForTimeout(150)
  await page.screenshot({ path: join(OUT, 'prompt-as-sent-fetch-error.png') })
  console.log('wrote', join(OUT, 'prompt-as-sent-fetch-error.png'))
  await page.route('**/api/telemetry/prompt-trace**', route => json(route, PROMPT_TRACE))

  // The bounds states, staged so a human sees them before release: the newest
  // record cut at the per-turn cap, and two earlier turns pushed out of the ring.
  // Coherent with the chart: the usage row and the record name the same size
  // (both come from one string), only the retained text is shorter.
  const newest = PROMPT_TRACE.turns[PROMPT_TRACE.turns.length - 1]
  const bigTotal = newest.chars + 2_500_000
  // Coherent all the way down: the extra 2.5M is a paste in the user's own
  // message, so the block sizes sum to the new total and the chart's bands
  // rise with the marker instead of sitting flat under it.
  const boundedTrace = {
    ...CONTEXT_TRACE,
    turns: CONTEXT_TRACE.turns.map((t, i, arr) =>
      i === arr.length - 1
        ? { ...t, total_chars: bigTotal, blocks: { ...t.blocks, your_message: (t.blocks.your_message ?? 0) + 2_500_000 } }
        : t,
    ),
  }
  const bounded = {
    ...PROMPT_TRACE,
    turns: [{ ...newest, truncated: true, chars: bigTotal, assembled_chars: bigTotal }],
    dropped: 2,
  }
  await page.route('**/api/telemetry/context-trace**', route => json(route, boundedTrace))
  await page.route('**/api/telemetry/prompt-trace**', route => json(route, bounded))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await section.waitFor({ timeout: 20000 })
  await page.getByTestId('prompt-truncated').waitFor({ timeout: 10000 })
  await page.getByTestId('prompt-dropped').waitFor({ timeout: 10000 })
  // Seven-digit y-axis labels at this scale must sit whole inside the chart.
  const clipped = await page.evaluate(() => {
    const svg = document.querySelector('[data-testid="context-breakdown"] svg') ?? document.querySelector('svg')
    if (!svg) return 'no svg'
    const box = svg.getBoundingClientRect()
    const bad = Array.from(svg.querySelectorAll('text.tabular-nums'))
      .map(t => ({ text: t.textContent, left: t.getBoundingClientRect().left }))
      .filter(t => t.left < box.left)
    return bad.length ? JSON.stringify(bad) : ''
  })
  if (clipped) throw new Error('y-axis labels clipped: ' + clipped)
  await section.scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  await shot('prompt-as-sent-bounds.png')
  console.log('wrote', join(OUT, 'prompt-as-sent-bounds.png'))

  // The whole session evicted to make room: nothing to open, and it says why.
  await page.route('**/api/telemetry/context-trace**', route => json(route, CONTEXT_TRACE))
  await page.route('**/api/telemetry/prompt-trace**', route => json(route, { ...PROMPT_TRACE, turns: [], evicted: true }))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await section.waitFor({ timeout: 20000 })
  const evictedText = await section.innerText()
  if (!evictedText.includes("Cleared to make room for other chats' prompts.")) throw new Error('evicted state not rendered:\n' + evictedText.slice(0, 500))
  await section.scrollIntoViewIfNeeded()
  await shot('prompt-as-sent-evicted.png')
  console.log('wrote', join(OUT, 'prompt-as-sent-evicted.png'))

  // Copy all, both outcomes. Success needs the clipboard permission the
  // headless context does not grant by default; failure is staged by taking
  // both clipboard paths away.
  await page.route('**/api/telemetry/prompt-trace**', route => json(route, PROMPT_TRACE))
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.reload({ waitUntil: 'domcontentloaded' })
  await section.waitFor({ timeout: 20000 })
  await section.getByRole('button', { name: 'Copy all' }).click()
  await section.getByRole('button', { name: 'Copied' }).waitFor({ timeout: 5000 })
  await section.scrollIntoViewIfNeeded()
  await shot('prompt-as-sent-copied.png')
  console.log('wrote', join(OUT, 'prompt-as-sent-copied.png'))

  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    document.execCommand = () => false
  })
  await page.reload({ waitUntil: 'domcontentloaded' })
  await section.waitFor({ timeout: 20000 })
  await section.getByRole('button', { name: 'Copy all' }).click()
  await page.getByText("Couldn't copy").waitFor({ timeout: 5000 })
  await section.scrollIntoViewIfNeeded()
  await shot('prompt-as-sent-copy-failed.png')
  console.log('wrote', join(OUT, 'prompt-as-sent-copy-failed.png'))

  await h.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
