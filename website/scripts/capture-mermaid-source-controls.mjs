/**
 * Screenshot + video harness for issue #5902: the source view and copy-source
 * control on a rendered Mermaid diagram in the chat transcript.
 *
 * Before this change a rendered diagram carried one control, enlarge. The source
 * it was rendered from was already held by the component but nothing surfaced
 * it, so a reader could neither read it nor take it away.
 *
 * Three passes, because each needs its own fixture or browser options:
 *
 *   A (valid diagram)    01 diagram      the default view: source toggle + enlarge
 *                        02 source       the toggle pressed: source verbatim, the
 *                                        diagram hidden, copy in place of enlarge
 *                        03 copied       the copy control's confirmation
 *                        04 full-window  the whole dashboard, for context
 *   B (invalid diagram)  05 failed       the failed-render state: copy offered
 *                                        alone, no toggle, source as its own
 *                                        evidence under the notice
 *   C (valid + video)    toggle.webm     the diagram <-> source round trip, which
 *                                        a still frame cannot show
 *
 * Every pass runs the REAL built SPA (website/dist) through the shared
 * transcript harness with every /api/** call answered from fixtures. No gateway,
 * no token.
 *
 * Each frame is gated on an assertion about the RENDERED state rather than on the
 * file being written: a stale bundle or a silently unrendered control fails the
 * run instead of producing a screenshot of the old UI.
 *
 * Usage: node scripts/capture-mermaid-source-controls.mjs [outDir]
 */
import { mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || process.env.PROBE_OUT || '../temp-screenshots/mermaid-source-5902'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const MERMAID_SOURCE = [
  'graph TD',
  '  A[Booking request] --> B{Payment mode?}',
  '  B -->|prepaid| C[Charge via PSP]',
  '  B -->|pay on arrival| D[Hold voucher]',
  '  C --> E[Confirm]',
  '  D --> E[Confirm]',
].join('\n')

// Deliberately unparseable: `graph` with a bare arrow to nothing. The block's
// failure path paints the source as its own evidence, which is the state frame
// 05 documents.
const BROKEN_SOURCE = 'graph TD\n  A[Unclosed --> \n  B{{{'

const fence = (src) => ['```mermaid', src, '```'].join('\n')

function scene(slot, title, lead, src) {
  const md = [lead, '', fence(src)].join('\n')
  return {
    slots: [{
      key: slot, title, running: false, last_message: 'Rendered the booking flow.',
      messages: 2, agent: 'kirocrew', project: PROJECT, updated: now,
    }],
    detail: {
      key: slot, title, agent: 'kirocrew', project: PROJECT, running: false,
      messages: [
        { role: 'user', content: 'Draw the booking flow.', ts: now - 60 },
        { role: 'assistant', content: md, ts: now - 30 },
      ],
    },
  }
}

async function open(slot, title, lead, src, opts = {}) {
  const { slots, detail } = scene(slot, title, lead, src)
  const harness = await openTranscriptHarness({
    slot, project: PROJECT, slots, detail,
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: opts.recordVideo ? 1 : 2,
    ...(opts.recordVideo ? { recordVideo: opts.recordVideo } : {}),
  })
  await harness.load('dark', { selector: 'figure', settle: 1200 })
  return harness
}

const shot = async (page, locator, name) => {
  await locator.evaluate(el => el.scrollIntoView({ block: 'center' }))
  await page.waitForTimeout(350)
  await locator.screenshot({ path: join(OUT, name) })
  console.log('captured', name)
}

// ── Pass A: the valid diagram ────────────────────────────────────────────────
{
  const harness = await open('chat-mermaid-5902', 'Mermaid diagram controls',
    'The orchestrator fans a booking out to payment before confirming:', MERMAID_SOURCE)
  const { page } = harness
  const toggle = page.getByTestId('mermaid-source-toggle')
  const enlarge = page.getByTestId('mermaid-enlarge')
  await toggle.waitFor({ state: 'visible', timeout: 20000 })
  const block = page.locator('figure').locator('..').first()
  await block.hover()
  await page.waitForTimeout(200)

  // 01 — the default view: toggle + enlarge, and copy deliberately absent because
  // on the rendered diagram the object of "copy" would be ambiguous.
  if (!(await enlarge.isVisible())) throw new Error('01: enlarge not visible -- stale bundle?')
  if (await page.getByTestId('mermaid-copy-source').count() !== 0) {
    throw new Error('01: copy is offered on the rendered diagram')
  }
  if (await page.getByTestId('mermaid-source').count() !== 0) {
    throw new Error('01: a source block is showing before the toggle was pressed')
  }
  if (await toggle.getAttribute('aria-pressed') !== 'false') {
    throw new Error('01: toggle does not report the diagram view')
  }
  if (await page.locator('figure svg[aria-roledescription]').count() === 0) {
    throw new Error('01: no rendered diagram -- mermaid did not resolve')
  }
  await shot(page, block, '01-diagram-view.png')

  // 02 — the source view: the fence VERBATIM, the diagram hidden, enlarge
  // replaced by copy so the row still holds two.
  await toggle.click()
  const source = page.getByTestId('mermaid-source')
  await source.waitFor({ state: 'visible', timeout: 5000 })
  const shown = (await source.textContent()) ?? ''
  if (shown.trim() !== MERMAID_SOURCE.trim()) {
    throw new Error(`02: source view text is not the fence verbatim:\n${shown}`)
  }
  if (await page.locator('figure').first().isVisible()) {
    throw new Error('02: the diagram is still visible behind the source view')
  }
  if (await enlarge.count() !== 0) throw new Error('02: enlarge is still offered from the source view')
  if (await toggle.getAttribute('aria-pressed') !== 'true') {
    throw new Error('02: toggle does not report the source view')
  }
  const copy = page.getByTestId('mermaid-copy-source')
  if (!(await copy.isVisible())) throw new Error('02: copy is not offered beside the source')
  const rowCount = await toggle.locator('..').locator('button').count()
  if (rowCount > 2) throw new Error(`02: action row holds ${rowCount} buttons, cap is 2`)
  await block.hover()
  await shot(page, block, '02-source-view.png')

  // 03 — the confirmation. Asserted on the accessible name, which is where the
  // transient state lives, and which also proves the write actually succeeded:
  // a refused write reads "failed" here, not "copied".
  await copy.click()
  await page.waitForTimeout(150)
  const label = await copy.getAttribute('aria-label')
  if (!/copied/i.test(label ?? '')) {
    throw new Error(`03: copy did not confirm; aria-label is ${JSON.stringify(label)}`)
  }
  await shot(page, block, '03-copied.png')

  // 04 — the whole window at rest. Back to the diagram view, and WAIT OUT the
  // 1500ms confirmation: a frame shot inside that window shows the transient
  // check rather than the resting state.
  await toggle.click()
  await enlarge.waitFor({ state: 'visible', timeout: 5000 })
  await page.waitForTimeout(1800)
  await block.hover()
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, '04-full-window.png') })
  console.log('captured 04-full-window.png')
  await harness.close()
}

// ── Pass B: the failed render ───────────────────────────────────────────────
{
  const harness = await open('chat-mermaid-5902-bad', 'Mermaid render failure',
    'This one does not parse:', BROKEN_SOURCE)
  const { page } = harness
  const notice = page.getByTestId('mermaid-render-error')
  await notice.waitFor({ state: 'visible', timeout: 20000 })
  const copy = page.getByTestId('mermaid-copy-source')
  if (!(await copy.isVisible())) throw new Error('05: copy is not offered on a failed render')
  if (await page.getByTestId('mermaid-source-toggle').count() !== 0) {
    throw new Error('05: a source toggle is offered though nothing rendered')
  }
  const n = await copy.locator('..').locator('button').count()
  if (n !== 1) throw new Error(`05: expected copy alone on a failed render, found ${n} buttons`)
  const block = page.locator('figure').locator('..').first()
  await block.hover()
  await shot(page, block, '05-failed-render.png')
  await harness.close()
}

// ── Pass C: the round trip, recorded ────────────────────────────────────────
{
  const harness = await open('chat-mermaid-5902-vid', 'Mermaid diagram controls',
    'The orchestrator fans a booking out to payment before confirming:', MERMAID_SOURCE,
    { recordVideo: { dir: OUT, size: { width: 1280, height: 900 } } })
  const { page } = harness
  const toggle = page.getByTestId('mermaid-source-toggle')
  await toggle.waitFor({ state: 'visible', timeout: 20000 })
  const block = page.locator('figure').locator('..').first()
  await block.evaluate(el => el.scrollIntoView({ block: 'center' }))
  await block.hover()
  await page.waitForTimeout(900)
  for (let i = 0; i < 2; i += 1) {
    await toggle.click()
    await page.getByTestId('mermaid-source').waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(1100)
    await toggle.click()
    await page.getByTestId('mermaid-enlarge').waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(1100)
  }
  const video = page.video()
  if (!video) throw new Error('C: no video was recorded')
  const raw = await video.path()
  await harness.close()
  renameSync(raw, join(OUT, 'toggle-round-trip.webm'))
  console.log('captured toggle-round-trip.webm')
}

// ── Pass D: a REFUSED clipboard write ───────────────────────────────────────
// The state a still frame could not previously show, and the one the review
// asked for. Forced rather than simulated: both paths `copyCode` can take are
// disabled in the live page -- `navigator.clipboard` removed (as on any
// non-secure origin) and `execCommand` made to report false (as when the
// textarea fallback is refused) -- so the frame is of the real failure branch,
// not of a state poked into the component.
{
  const harness = await open('chat-mermaid-5902-refuse', 'Mermaid diagram controls',
    'The orchestrator fans a booking out to payment before confirming:', MERMAID_SOURCE)
  const { page } = harness
  const toggle = page.getByTestId('mermaid-source-toggle')
  await toggle.waitFor({ state: 'visible', timeout: 20000 })
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    document.execCommand = () => false
  })
  await toggle.click()
  const copy = page.getByTestId('mermaid-copy-source')
  await copy.waitFor({ state: 'visible', timeout: 5000 })
  await copy.click()

  const notice = page.getByTestId('mermaid-copy-error')
  await notice.waitFor({ state: 'visible', timeout: 5000 })
  const text = (await notice.textContent()) ?? ''
  if (!/fail/i.test(text)) throw new Error(`06: notice does not report a failure: ${text}`)
  // The failure must NOT be announced as a success anywhere.
  const label = await copy.getAttribute('aria-label')
  if (/copied/i.test(label ?? '')) throw new Error(`06: a refused write still claims ${label}`)
  // ONE error surface: the button keeps its neutral name beside the notice.
  if (/fail/i.test(label ?? '')) throw new Error(`06: the failure is restated on the button: ${label}`)
  // The notice is a SEPARATED REGION, so the row is still under its cap.
  const inRow = await toggle.locator('..').getByTestId('mermaid-copy-error').count()
  if (inRow !== 0) throw new Error('06: the notice is inside the capped action row')
  const rowCount = await toggle.locator('..').locator('button').count()
  if (rowCount > 2) throw new Error(`06: action row holds ${rowCount} buttons, cap is 2`)
  // And it OUTLIVES the confirmation timer: an error that erased itself could
  // not be read. Shot after that window, so the frame proves the persistence.
  await page.waitForTimeout(2200)
  if (!(await notice.isVisible())) throw new Error('06: the failure erased itself on a timer')
  const block = page.locator('figure').locator('..').first()
  await block.hover()
  await shot(page, block, '06-copy-failed.png')
  await harness.close()
}

console.log('done ->', OUT)
