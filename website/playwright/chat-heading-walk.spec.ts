import { test, expect, type Page, type APIRequestContext } from '@playwright/test'

/**
 * A screen reader's heading key reaches every turn of a virtualized transcript.
 *
 * Only the rows around the viewport are mounted, so the next speaker heading
 * past the window edge is not in the DOM. A screen reader that lands its
 * cursor on a heading scrolls it into view (in Chromium the same scroll as
 * `element.scrollIntoView()`); that scroll must move the transcript scroller,
 * so the virtualizer's overscan mounts the following turns and their headings
 * become the next H / Shift+H target.
 *
 * Asserted: stepping heading by heading with `scrollIntoView()`, from the live
 * end up to the first row and from the top down to the last, lands on every
 * turn's heading, each landed heading stays exposed (a user row scrolled to the
 * top hands over to the pinned banner, which hides the row), and the
 * conversation never pans sideways.
 */

const SCROLLER = '.chat-container'
const HEADING = 'h2.sr-only'
const VIEWPORT = { width: 1280, height: 800 }
// 60 rows: far longer than the mounted window, and under the first page of
// history, so display row 0 is the first message.
const TURNS = 30
const SETTLED_QUIET_READS = 3 // identical reads at 100ms

const WORDS = 'the quick brown fox jumps over the lazy dog while reading a long transcript row'

interface Landing {
  index: number
  role: string
}

interface MountedWindow {
  first: number
  last: number
  scrollTop: number
  scrollLeft: number
}

async function importTranscript(request: APIRequestContext): Promise<string> {
  const messages: { role: string; content: string; ts: string }[] = []
  for (let i = 0; i < TURNS; i++) {
    const paragraphs = 2 + ((i * 7) % 5)
    const reply = Array.from({ length: paragraphs }, (_, p) =>
      `Turn ${i} paragraph ${p}. ${Array(1 + ((i + p) % 4)).fill(WORDS).join(' ')}`).join('\n\n')
    messages.push({ role: 'user', content: `Question ${i}: what does turn ${i} say?`, ts: new Date(Date.UTC(2026, 0, 1, 0, 0, 2 * i)).toISOString() })
    messages.push({ role: 'assistant', content: reply, ts: new Date(Date.UTC(2026, 0, 1, 0, 0, 2 * i + 1)).toISOString() })
  }
  const imported = await request.post('/api/chat/slots/import', {
    data: { bundle_version: 1, title: 'Heading walk', origin: 'playwright', agent: '', messages },
  })
  expect(imported.status(), `session import failed: ${await imported.text()}`).toBeLessThan(300)
  return ((await imported.json()) as { key: string }).key
}

async function readWindow(page: Page): Promise<MountedWindow> {
  return page.locator(SCROLLER).evaluate((sc) => {
    const rows = [...sc.querySelectorAll<HTMLElement>('[data-display-index]')].map(el => Number(el.dataset.displayIndex))
    return { first: Math.min(...rows), last: Math.max(...rows), scrollTop: Math.round(sc.scrollTop), scrollLeft: sc.scrollLeft }
  })
}

/** Poll until the mounted range and the scroll offset hold for a quiet window:
 *  the landing scroll, the window shift and the new rows' measurement land
 *  over several frames. */
async function settledWindow(page: Page): Promise<MountedWindow> {
  let last = ''
  let quiet = 0
  let reading: MountedWindow | null = null
  await expect.poll(async () => {
    reading = await readWindow(page)
    const key = JSON.stringify(reading)
    quiet = key === last ? quiet + 1 : 0
    last = key
    return quiet >= SETTLED_QUIET_READS
  }, { timeout: 15_000, intervals: [100] }).toBe(true)
  return reading!
}

/** Land on the mounted heading next to `from` (the first or last mounted one
 *  when `from` is null) the way a screen reader does, by scrolling it into
 *  view. Returns null when no heading lies that way in the DOM.
 *  Only headings a screen reader can reach count: the measure farm renders
 *  copies of unmeasured rows, headings included, into an `aria-hidden` box
 *  after the last row while it measures a batch. */
async function landNext(page: Page, from: Landing | null, dir: 'up' | 'down'): Promise<Landing | null> {
  return page.locator(SCROLLER).evaluate((sc, { from, dir, heading }) => {
    const reachable = [...sc.querySelectorAll<HTMLElement>(heading)].filter(h => !h.closest('[aria-hidden="true"]'))
    const all = reachable.map(h => ({
      h,
      index: Number(h.closest<HTMLElement>('[data-display-index]')?.dataset.displayIndex ?? -1),
      role: h.closest<HTMLElement>('[data-role]')?.dataset.role ?? '',
    }))
    let at: number
    if (from === null) at = dir === 'up' ? all.length - 1 : 0
    else {
      const i = all.findIndex(x => x.index === from.index && x.role === from.role)
      if (i < 0) return null
      at = dir === 'up' ? i - 1 : i + 1
    }
    const next = all[at]
    if (!next) return null
    next.h.scrollIntoView()
    return { index: next.index, role: next.role }
  }, { from, dir, heading: HEADING })
}

async function walk(page: Page, dir: 'up' | 'down'): Promise<Set<string>> {
  const landed = new Set<string>()
  let at: Landing | null = null
  for (let step = 0; step <= TURNS * 2; step++) {
    const next = await landNext(page, at, dir)
    if (!next) break
    expect(next.index, 'every reachable speaker heading sits in a transcript row').toBeGreaterThanOrEqual(0)
    const settled = await settledWindow(page)
    expect(settled.scrollLeft, 'the landing scroll never pans the transcript sideways').toBe(0)
    const heading = page.locator(`${SCROLLER} [data-display-index="${next.index}"] [data-role="${next.role}"] ${HEADING}`)
    await expect(heading, 'the landed heading stays in the accessibility tree').toBeVisible()
    landed.add(`${next.index}:${next.role}`)
    at = next
  }
  return landed
}

function rowsOf(landed: Set<string>): number[] {
  return [...landed].map(k => Number(k.split(':')[0]))
}

function everyTurn(): string[] {
  return Array.from({ length: TURNS * 2 }, (_, i) => `${i}:${i % 2 === 0 ? 'user' : 'assistant'}`)
}

test.describe('Chat transcript heading navigation', () => {
  let key = ''

  test.afterEach(async ({ request }) => {
    if (key) await request.delete(`/api/chat/slots/${encodeURIComponent(key)}`)
    key = ''
  })

  test('landing on each speaker heading mounts the next turn, in both directions', async ({ page, request }) => {
    test.setTimeout(180_000)
    key = await importTranscript(request)
    await page.setViewportSize(VIEWPORT)
    await page.goto(`/chat/heading-walk?sid=${encodeURIComponent(key)}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator(SCROLLER)).toBeVisible({ timeout: 15_000 })
    await expect(page.locator(`[data-display-index="${TURNS * 2 - 1}"]`)).toBeAttached({ timeout: 15_000 })
    const start = await settledWindow(page)
    expect(start.first, 'the transcript is longer than the mounted window').toBeGreaterThan(0)

    // Shift+H from the live end up to the first turn.
    const up = await walk(page, 'up')
    expect(Math.min(...rowsOf(up)), 'Shift+H reaches the first turn').toBe(0)
    expect([...up].sort()).toEqual(everyTurn().sort())

    // H from the top down to the last turn.
    // A row measuring in at the top can move one scroll write: re-park until it holds.
    await expect.poll(async () => {
      await page.locator(SCROLLER).evaluate((sc) => { sc.scrollTop = 0 })
      return (await settledWindow(page)).first
    }, { timeout: 30_000 }).toBe(0)
    const top = await readWindow(page)
    expect(top.last, 'the top window leaves the last turn unmounted').toBeLessThan(TURNS * 2 - 1)
    const down = await walk(page, 'down')
    expect(Math.max(...rowsOf(down)), 'H reaches the last turn').toBe(TURNS * 2 - 1)
    expect([...down].sort()).toEqual(everyTurn().sort())
  })
})
