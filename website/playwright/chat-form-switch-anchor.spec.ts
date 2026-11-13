import { test, expect, type Page, type APIRequestContext } from '@playwright/test'

/**
 * The transcript keeps the reader's place when the window resizes across the
 * dashboard's phone breakpoint (useIsMobile, 768px), in both directions.
 *
 * Crossing it swaps the shell between its two-pane and phone forms: the
 * sessions sidebar leaves the row, so the chat column changes width by
 * hundreds of px over the frames the layout takes to settle, every mounted row
 * re-wraps, and the width-scoped height cache swaps to a new scope. The rows
 * here are varied-height prose, so the re-wrap moves each by a different
 * amount, and the reader parks mid-transcript with follow released.
 *
 * Asserted: after each resize settles, the row the reader was looking at is
 * still mounted and sits where it sat before the resize. Same-form resizes are
 * covered too, since the window recompute they share must not move either.
 */

const SCROLLER = '.chat-container'
const ROW = '[data-display-index]'
const VIEWPORT_HEIGHT = 800
// Assistant rows of 2-10 paragraphs, so a narrow column re-wraps each one by a
// different amount and the rows above the reader reprice unevenly.
const TURNS = 40
// Sub-pixel row heights (the cache keeps quarter pixels) and the spacer's
// whole-pixel rounding leave a pixel or two after a settled reprice.
const PX_TOLERANCE = 3
const SETTLED_QUIET_READS = 5 // ~1s of identical reads at 200ms

const WORDS = 'the quick brown fox jumps over the lazy dog while reading a long transcript row'

interface Reading {
  index: number
  top: number
  scrollTop: number
  scrollHeight: number
}

async function importTranscript(request: APIRequestContext): Promise<string> {
  const messages: { role: string; content: string; ts: string }[] = []
  for (let i = 0; i < TURNS; i++) {
    const paragraphs = 2 + ((i * 7) % 9)
    const reply = Array.from({ length: paragraphs }, (_, p) =>
      `Row ${i} paragraph ${p}. ${Array(1 + ((i + p) % 4)).fill(WORDS).join(' ')}`).join('\n\n')
    messages.push({ role: 'user', content: `Question ${i}: how does row ${i} wrap at a narrow width?`, ts: new Date(Date.UTC(2026, 0, 1, 0, 0, 2 * i)).toISOString() })
    messages.push({ role: 'assistant', content: reply, ts: new Date(Date.UTC(2026, 0, 1, 0, 0, 2 * i + 1)).toISOString() })
  }
  const imported = await request.post('/api/chat/slots/import', {
    data: { bundle_version: 1, title: 'Form switch anchor', origin: 'playwright', agent: '', messages },
  })
  expect(imported.status(), `session import failed: ${await imported.text()}`).toBeLessThan(300)
  return ((await imported.json()) as { key: string }).key
}

/** The first row whose top is inside the viewport, and where that top sits.
 *  Not the row straddling the viewport's top edge: a straddler that re-wraps
 *  keeps its BOTTOM where it was (the virtualizer's rule for the reader's own
 *  row), so its top legitimately moves, and the row below it is the one that
 *  holds still. */
async function readerRow(page: Page, pin?: number): Promise<Reading | null> {
  return page.evaluate(({ scroller, row, pin }) => {
    const sc = document.querySelector<HTMLElement>(scroller)
    if (!sc) return null
    const top = sc.getBoundingClientRect().top
    const geom = { scrollTop: Math.round(sc.scrollTop), scrollHeight: sc.scrollHeight }
    if (pin !== undefined) {
      const el = sc.querySelector<HTMLElement>(`[data-display-index="${pin}"]`)
      return el ? { index: pin, top: Math.round(el.getBoundingClientRect().top - top), ...geom } : null
    }
    for (const el of sc.querySelectorAll<HTMLElement>(row)) {
      const r = el.getBoundingClientRect()
      if (r.height > 0 && r.top >= top && r.top < top + sc.clientHeight) {
        return { index: Number(el.dataset.displayIndex), top: Math.round(r.top - top), ...geom }
      }
    }
    return null
  }, { scroller: SCROLLER, row: ROW, pin })
}

/** Poll until the pinned row's reading holds for a quiet window: the resize
 *  lands over several frames (the sidebar's exit, the 200ms width settle, the
 *  scope swap and its reseed), and a single read can catch it mid-way. */
async function settledRow(page: Page, index: number): Promise<Reading | null> {
  let last: string | null = null
  let quiet = 0
  let reading: Reading | null = null
  await expect.poll(async () => {
    reading = await readerRow(page, index)
    const key = JSON.stringify(reading)
    quiet = key === last ? quiet + 1 : 0
    last = key
    return quiet >= SETTLED_QUIET_READS
  }, { timeout: 15_000, intervals: [200] }).toBe(true)
  return reading
}

const SEQUENCES: number[][] = [
  // Desktop to phone and back: the defect.
  [951, 382, 951],
  [1400, 382, 1400],
  [740, 382, 740],
  // Phone to desktop and back.
  [382, 951, 382],
  [382, 1400, 382],
  // Within one form.
  [382, 740, 382],
  [951, 1400, 951],
]

test.describe('Chat transcript keeps the reading position across a form switch', () => {
  let key = ''

  test.afterEach(async ({ request }) => {
    if (key) await request.delete(`/api/chat/slots/${encodeURIComponent(key)}`)
    key = ''
  })

  for (const widths of SEQUENCES) {
    test(`holds the reader's row through ${widths.join(' -> ')}`, async ({ page, request }) => {
      key = await importTranscript(request)
      await page.setViewportSize({ width: widths[0], height: VIEWPORT_HEIGHT })
      await page.goto(`/chat/form-switch-anchor?sid=${encodeURIComponent(key)}`, { waitUntil: 'domcontentloaded' })
      await expect(page.locator(SCROLLER)).toBeVisible({ timeout: 15_000 })
      await expect(page.locator(ROW).first()).toBeVisible({ timeout: 15_000 })

      // Park mid-transcript with a real wheel, which releases follow the way a
      // reader does, and let the rows above measure in.
      await page.locator(SCROLLER).evaluate((el) => { el.scrollTop = el.scrollHeight * 0.4 })
      const box = await page.locator(SCROLLER).boundingBox()
      expect(box).not.toBeNull()
      await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
      await page.mouse.wheel(0, -60)
      // On the phone a reply can be taller than the viewport: wheel on until
      // some row's top is on screen to pin.
      let parked: Reading | null = null
      await expect.poll(async () => {
        parked = await readerRow(page)
        if (!parked) await page.mouse.wheel(0, 200)
        return parked !== null
      }, { timeout: 10_000 }).toBe(true)
      const parkedAt = await settledRow(page, parked!.index)
      expect(parkedAt, 'the parked row is mounted').not.toBeNull()
      expect(parkedAt!.scrollHeight - parkedAt!.scrollTop, 'the reader is well off the live end').toBeGreaterThan(VIEWPORT_HEIGHT * 2)

      // Each resize is judged against what the reader sees when it starts: on
      // the way back, a row the first resize brought fully on screen is held
      // by its own top, so the round trip need not return to the first frame.
      let from = widths[0]
      for (const width of widths.slice(1)) {
        const seen = await readerRow(page)
        expect(seen, `a row's top is on screen at ${from}px`).not.toBeNull()
        const before = await settledRow(page, seen!.index)
        await page.setViewportSize({ width, height: VIEWPORT_HEIGHT })
        const after = await settledRow(page, before!.index)
        expect(after, `row ${before!.index} is still mounted at ${width}px`).not.toBeNull()
        expect(
          Math.abs(after!.top - before!.top),
          `${from} -> ${width}px: row ${before!.index} sits at ${after!.top}px, was ${before!.top}px`,
        ).toBeLessThanOrEqual(PX_TOLERANCE)
        from = width
      }
    })
  }
})
