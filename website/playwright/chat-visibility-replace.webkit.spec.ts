import { test, expect, type Page, type APIRequestContext } from '@playwright/test'

/**
 * WebKit-engine coverage for the chat transcript's visibility snapshot /
 * re-placement path (useVirtualChat: hide snapshot on visible->hidden,
 * disturbance-gated re-placement on hidden->visible).
 *
 * Runs ONLY under the opt-in `webkit-mobile` project (PLAYWRIGHT_RUN_WEBKIT=1,
 * see playwright.config.ts): the `chromium` project ignores `*.webkit.spec.ts`,
 * so the default CI run never collects this file. The fix targets mobile
 * WebKit, and the jsdom cases in src/test/useVirtualChat.visibilityReplace.test.tsx
 * cannot observe a real layout engine; this file runs the same contract against
 * Playwright's WebKit with iPhone emulation and a real gateway.
 *
 * What is OBSERVED here versus what is INJECTED -- keep the distinction honest:
 *
 * - `visibilitychange` with `document.hidden` flipping is produced by defining
 *   own-property getters on `document` and dispatching the event. That is the
 *   same event sequence the engine emits on a tab background/return; Playwright
 *   exposes no way to background a headless WebKit page for real.
 * - A headless WebKit page does NOT collapse its layout on that synthetic hide
 *   (recorded per test as `clientHeightWhileHidden`). The zero-height box a
 *   backgrounded mobile tab gets is therefore INJECTED with inline styles on the
 *   scroller, and the file records what WebKit does to scrollTop / scroll events
 *   when the box collapses and when it comes back. Those clamp observations are
 *   real engine behaviour; the collapse itself is not.
 * - Content arriving while hidden is a real turn from the stub ACP backend,
 *   delivered over the live WebSocket to the still-mounted scroller. The
 *   disconnect-then-heal refetch a backgrounded mobile socket goes through is
 *   NOT exercised here.
 *
 * Every observation is attached to the test (`webkit-observations.json`) and
 * printed as `[webkit-visibility]` lines so a run's evidence can be quoted.
 */

const SCROLLER = '.chat-container'
const ROW = '[data-display-index]'
// Rows imported per test. Only the most recent PANE_HYDRATE_LIMIT are
// rendered; the count is asserted against the slot's own total after a turn.
const SEEDED_MESSAGES = 120
// The engine cannot resolve a written scrollTop past its own clamp, and a
// re-placed anchor lands at device-pixel granularity (DPR 3 on the emulated
// device), so a 2 CSS px band is the honest equality for "same position".
const PX_TOLERANCE = 2

interface Geom {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
  hidden: boolean
  visibilityState: string
  /** Mounted (rendered) row span, so a blank tail can be told from a scrolled one. */
  mounted: { first: number; last: number; count: number }
  /** Text of the newest mounted row (highest display index). */
  lastRowText: string
}

interface TopRow {
  index: number
  top: number
}

/** Scroller geometry at the instant an event fired (no row scan: cheap enough for a listener). */
type EventGeom = Pick<Geom, 'scrollTop' | 'scrollHeight' | 'clientHeight' | 'hidden' | 'visibilityState'>

interface RecordedEvent {
  type: string
  t: number
  geom: EventGeom
}

declare global {
  interface Window {
    __kcVisEvents?: RecordedEvent[]
    /** The scroller's inline style before `collapseScroller`, for an exact restore. */
    __kcScrollerCss?: string
  }
}

function geom(page: Page): Promise<Geom> {
  return page.evaluate(({ sel, row }) => {
    const el = document.querySelector<HTMLElement>(sel)!
    const idx = Array.from(el.querySelectorAll<HTMLElement>(row)).map((r) => Number(r.dataset.displayIndex))
    const lastIdx = idx.length ? Math.max(...idx) : -1
    const lastRow = lastIdx >= 0 ? el.querySelector<HTMLElement>(`[data-display-index="${lastIdx}"]`) : null
    const lastRowText = (lastRow?.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 120)
    return {
      scrollTop: el.scrollTop,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      hidden: document.hidden,
      visibilityState: document.visibilityState,
      mounted: { first: idx.length ? Math.min(...idx) : -1, last: lastIdx, count: idx.length },
      lastRowText,
    }
  }, { sel: SCROLLER, row: ROW })
}

function distanceFromBottom(g: Geom): number {
  return g.scrollHeight - g.scrollTop - g.clientHeight
}

// Topmost mounted row whose bottom edge is still below the scroller's top --
// the same rule captureTopAnchorFrom applies -- and its offset from that top.
function topRow(page: Page): Promise<TopRow | null> {
  return page.evaluate(({ sel, row }) => {
    const el = document.querySelector<HTMLElement>(sel)!
    const sTop = el.getBoundingClientRect().top
    let best: { index: number; top: number } | null = null
    for (const r of Array.from(el.querySelectorAll<HTMLElement>(row))) {
      const rc = r.getBoundingClientRect()
      if (rc.bottom <= sTop + 0.5) continue
      const index = Number(r.dataset.displayIndex)
      if (best === null || index < best.index) best = { index, top: rc.top - sTop }
    }
    return best
  }, { sel: SCROLLER, row: ROW })
}

function rowOffset(page: Page, index: number): Promise<number | null> {
  return page.evaluate(({ sel, index }) => {
    const el = document.querySelector<HTMLElement>(sel)!
    const r = el.querySelector<HTMLElement>(`[data-display-index="${index}"]`)
    if (!r) return null
    return r.getBoundingClientRect().top - el.getBoundingClientRect().top
  }, { sel: SCROLLER, index })
}

// Records every visibilitychange / scroll / resize with the geometry at that
// instant, so the report can show the ORDER the engine produced, not a guess.
async function installRecorder(page: Page): Promise<void> {
  await page.evaluate((sel) => {
    const el = document.querySelector<HTMLElement>(sel)!
    const events: RecordedEvent[] = []
    window.__kcVisEvents = events
    const snap = (type: string) => {
      events.push({
        type,
        t: Math.round(performance.now()),
        geom: {
          scrollTop: el.scrollTop,
          scrollHeight: el.scrollHeight,
          clientHeight: el.clientHeight,
          hidden: document.hidden,
          visibilityState: document.visibilityState,
        },
      })
    }
    // Capture-phase so the recorder's row precedes the hook's own bubble
    // listener for the same event; both read the same geometry.
    document.addEventListener('visibilitychange', () => snap('visibilitychange'), true)
    el.addEventListener('scroll', () => snap('scroll'), { passive: true })
    window.addEventListener('resize', () => snap('resize'))
  }, SCROLLER)
}

function drainEvents(page: Page): Promise<RecordedEvent[]> {
  return page.evaluate(() => {
    const ev = window.__kcVisEvents ?? []
    window.__kcVisEvents = []
    return ev
  })
}

// The engine's own hidden/visible signal, minus the engine's decision to send
// it: own-property getters shadow the prototype accessors, then the event the
// hook listens for is dispatched on the document exactly where the engine
// dispatches it.
async function goHidden(page: Page): Promise<void> {
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

async function goVisible(page: Page): Promise<void> {
  await page.evaluate(() => {
    // Deleting the own properties re-exposes the prototype getters, which
    // report the engine's real (visible) state.
    delete (document as unknown as Record<string, unknown>).hidden
    delete (document as unknown as Record<string, unknown>).visibilityState
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

// INJECTED zero-height box: what a backgrounded mobile tab's scroller reads
// as. Inline `!important` beats the shell's flex/overflow contract for the
// duration of the hidden interval only. The scroller's own inline style
// (the shell writes `flex: 1` and the host's padding inline) is stashed
// first and put back verbatim by `restoreScroller`, so the restore is a real
// round-trip: dropping those properties instead would leave the box ~24px
// taller than before and confound the "same position" comparison.
async function collapseScroller(page: Page): Promise<void> {
  await page.evaluate((sel) => {
    const el = document.querySelector<HTMLElement>(sel)!
    window.__kcScrollerCss = el.style.cssText
    el.style.setProperty('height', '0px', 'important')
    el.style.setProperty('min-height', '0px', 'important')
    el.style.setProperty('flex', '0 0 0px', 'important')
    el.style.setProperty('padding-top', '0px', 'important')
    el.style.setProperty('padding-bottom', '0px', 'important')
  }, SCROLLER)
}

async function restoreScroller(page: Page): Promise<void> {
  await page.evaluate((sel) => {
    const el = document.querySelector<HTMLElement>(sel)!
    el.style.cssText = window.__kcScrollerCss ?? ''
  }, SCROLLER)
}

// Waits until the follower has settled at the live end and the geometry has
// stopped moving (hydration, measurement, late-rendering timestamps and the
// slot-entry pin have all landed): three consecutive identical reads.
async function settleAtBottom(page: Page): Promise<Geom> {
  let last: Geom | null = null
  let stableReads = 0
  await expect
    .poll(async () => {
      const g = await geom(page)
      const same = last !== null && last.scrollTop === g.scrollTop && last.scrollHeight === g.scrollHeight
      stableReads = same ? stableReads + 1 : 0
      last = g
      return stableReads >= 2 && distanceFromBottom(g) <= PX_TOLERANCE && g.scrollHeight > g.clientHeight * 2
    }, { timeout: 20_000, intervals: [250] })
    .toBe(true)
  return last!
}

// A reader scrolled up by hand: the write lands outside the hook's own
// last-write record, so its scroll handler reads it as a user scroll and
// releases follow, then the debounced anchor save (200ms) runs. The result
// is POLLED rather than assumed: a pin racing the write (a row re-measuring
// in the same frame) can snap the box back once, and only a position that
// HOLDS proves the release.
async function releaseReader(page: Page, fraction: number): Promise<Geom> {
  const write = () =>
    page.evaluate(({ sel, fraction }) => {
      const el = document.querySelector<HTMLElement>(sel)!
      el.scrollTop = Math.round((el.scrollHeight - el.clientHeight) * fraction)
      return el.scrollTop
    }, { sel: SCROLLER, fraction })
  let target = await write()
  let held = 0
  await expect
    .poll(async () => {
      const g = await geom(page)
      if (g.scrollTop !== target) {
        held = 0
        target = await write()
        return false
      }
      held += 1
      return held >= 3
    }, { timeout: 10_000, intervals: [200] })
    .toBe(true)
  const g = await geom(page)
  expect(distanceFromBottom(g), 'reader must be released (well off the live end)').toBeGreaterThan(200)
  return g
}

// Seeds a transcript long enough to scroll on the emulated phone and opens it.
// The most-recent-50 bounded hydrate (PANE_HYDRATE_LIMIT) is what gets rendered,
// and every reader position in this file stays far from the top so the paging
// bar is never triggered and row display indices stay stable.
async function openSeededChat(page: Page, request: APIRequestContext, label: string): Promise<string> {
  const messages = Array.from({ length: SEEDED_MESSAGES }, (_, i) => ({
    role: i % 2 === 0 ? 'user' : 'assistant',
    content: `${label} row ${String(i).padStart(3, '0')}\n\nline two of row ${i}\n\nline three of row ${i}`,
    ts: new Date(Date.UTC(2026, 0, 1, 0, 0, 0, i)).toISOString(),
  }))
  const imported = await request.post('/api/chat/slots/import', {
    data: { bundle_version: 1, title: `Visibility replace ${label}`, origin: 'playwright webkit', agent: '', messages },
  })
  expect(imported.status(), `session import failed: ${await imported.text()}`).toBeLessThan(300)
  const { key } = (await imported.json()) as { key: string }
  await page.goto(`/chat/visibility-replace?sid=${encodeURIComponent(key)}`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByPlaceholder(/message/i)).toBeVisible({ timeout: 15_000 })
  await expect(page.locator(SCROLLER)).toBeVisible()
  await expect(page.locator(ROW).first()).toBeVisible({ timeout: 15_000 })
  return key
}

// A real turn from the stub ACP backend, appended to the slot while the page
// is hidden. The SSE body resolves once the turn is finalized ("[DONE]").
async function appendTurnWhileHidden(request: APIRequestContext, key: string, marker: string): Promise<void> {
  const res = await request.post('/api/chat', { data: { message: marker, slot: key } })
  expect(res.ok(), `turn failed: ${await res.text()}`).toBeTruthy()
  expect(await res.text()).toContain('[DONE]')
}

async function report(page: Page, name: string, data: Record<string, unknown>): Promise<void> {
  const events = await drainEvents(page)
  const payload = { ...data, events }
  console.log(`[webkit-visibility] ${name} ${JSON.stringify(payload)}`)
  await test.info().attach('webkit-observations.json', {
    body: JSON.stringify(payload, null, 2),
    contentType: 'application/json',
  })
}

// Polls the scroller until the geometry holds for a full quiet window, so a
// post-return re-placement that lands over two commits is read once it has
// landed. The window is deliberately longer than the hook's own settle
// (ANCHOR_RESTORE_SETTLE_MS, 600ms): after a reader release the rows above the
// fold finish measuring in ~500ms steps for a few seconds, each compensated by
// an integer scrollTop write that walks the top row by a fraction of a pixel,
// and a window shorter than one step (three reads at 200ms) fits between two
// steps and reads a baseline that is still converging, so the tail of that
// convergence shows up as ~2px of "return" drift.
const SETTLED_QUIET_READS = 6 // ~1.2s of identical reads at 200ms

async function settled(page: Page): Promise<Geom> {
  let last: Geom | null = null
  let stableReads = 0
  await expect
    .poll(async () => {
      const g = await geom(page)
      const same =
        last !== null &&
        last.scrollTop === g.scrollTop &&
        last.scrollHeight === g.scrollHeight &&
        last.mounted.last === g.mounted.last
      stableReads = same ? stableReads + 1 : 0
      last = g
      return stableReads >= SETTLED_QUIET_READS
    }, { timeout: 12_000, intervals: [200] })
    .toBe(true)
  return last!
}

const APPENDED_REPLY = 'pong from the fake ACP backend'

test.describe('Chat transcript re-placement on a mobile tab return (WebKit)', { tag: '@needs-agent' }, () => {
  let key = ''

  test.afterEach(async ({ request }) => {
    if (key) await request.delete(`/api/chat/slots/${encodeURIComponent(key)}`)
    key = ''
  })

  test('undisturbed return: a follower is left exactly where it was', async ({ page, request }) => {
    key = await openSeededChat(page, request, 'follower-quiet')
    const before = await settleAtBottom(page)
    await installRecorder(page)

    await goHidden(page)
    const whileHidden = await geom(page)
    await goVisible(page)
    await page.waitForTimeout(500)
    const after = await settled(page)

    await report(page, 'follower-undisturbed', {
      before, whileHidden, after, clientHeightWhileHidden: whileHidden.clientHeight,
    })
    expect(whileHidden.hidden).toBe(true)
    expect(after.hidden).toBe(false)
    // No re-placement happened: a follower's position is the live end, so the
    // only scrollTop movement an undisturbed return may show is the pin
    // tracking rows that finish measuring after the return, and that is
    // bounded by the transcript's growth. The at-bottom check below is the
    // position assertion.
    const growth = Math.max(0, after.scrollHeight - before.scrollHeight)
    expect(Math.abs(after.scrollTop - before.scrollTop)).toBeLessThanOrEqual(PX_TOLERANCE + growth)
    expect(distanceFromBottom(after)).toBeLessThanOrEqual(PX_TOLERANCE)
  })

  test('undisturbed return: a released reader is left exactly where it was', async ({ page, request }) => {
    key = await openSeededChat(page, request, 'reader-quiet')
    await settleAtBottom(page)
    await releaseReader(page, 0.5)
    // Rows newly mounted around the release position finish measuring over the
    // next commits; a released reader's hook compensates those above-viewport
    // height changes by design. Read the baseline only once that has settled,
    // so the comparison below is about the visibility round-trip alone.
    const before = await settled(page)
    const row = (await topRow(page))!
    await installRecorder(page)

    await goHidden(page)
    const whileHidden = await geom(page)
    await goVisible(page)
    await page.waitForTimeout(500)
    const after = await settled(page)
    const rowAfter = await rowOffset(page, row.index)

    await report(page, 'reader-undisturbed', {
      before, whileHidden, after, hideTimeTopRow: row, rowOffsetAfter: rowAfter,
      clientHeightWhileHidden: whileHidden.clientHeight,
    })
    expect(rowAfter).not.toBeNull()
    expect(Math.abs(rowAfter! - row.top)).toBeLessThanOrEqual(PX_TOLERANCE)
    // No re-placement happened: the only scrollTop movement an undisturbed
    // return may show is the hook's own compensation for a row above the reader
    // that finished measuring, and that is bounded by the transcript's growth.
    const growth = Math.max(0, after.scrollHeight - before.scrollHeight)
    expect(Math.abs(after.scrollTop - before.scrollTop)).toBeLessThanOrEqual(PX_TOLERANCE + growth)
    expect(distanceFromBottom(after)).toBeGreaterThan(200)
  })

  test('follower: rows grow under an injected zero-height box while hidden; return lands at the live end', async ({ page, request }) => {
    key = await openSeededChat(page, request, 'follower-collapse')
    const before = await settleAtBottom(page)
    await installRecorder(page)

    await goHidden(page)
    const hiddenIntact = await geom(page)
    await collapseScroller(page)
    const collapsed = await geom(page)
    await appendTurnWhileHidden(request, key, 'appended while hidden (follower)')
    // Let the WebSocket frames land and the hook run its (guarded) pins.
    await page.waitForTimeout(800)
    const collapsedAfterGrowth = await geom(page)
    await restoreScroller(page)
    const restoredBeforeReturn = await geom(page)
    await goVisible(page)
    await expect.poll(() => geom(page).then(distanceFromBottom), { timeout: 5_000 }).toBeLessThanOrEqual(PX_TOLERANCE)
    const after = await settled(page)

    await report(page, 'follower-collapse', {
      before, hiddenIntact, collapsed, collapsedAfterGrowth, restoredBeforeReturn, after,
      clientHeightWhileHidden: hiddenIntact.clientHeight,
    })
    // The premise the fix is built on, checked against the real engine: a box
    // laid out at zero height reads its whole transcript as "distance from
    // bottom".
    expect(collapsed.clientHeight).toBe(0)
    // The injected collapse is a round-trip: the box is back at its hide-time
    // size, so the return is measured against the same viewport.
    expect(restoredBeforeReturn.clientHeight).toBe(before.clientHeight)
    // Growth is asserted on the ROWS, not on scrollHeight: with virtualized
    // rows the total is estimated heights plus measured ones, so mounting a
    // different window can shrink it even as two rows were appended.
    expect(after.mounted.last).toBe(before.mounted.last + 2)
    expect(distanceFromBottom(after)).toBeLessThanOrEqual(PX_TOLERANCE)
    // The live end IS the appended reply: the newest row is mounted and shown,
    // not a spacer standing in for it.
    expect(after.lastRowText).toContain(APPENDED_REPLY)
  })

  test('released reader: rows grow under an injected zero-height box while hidden; return lands at the hide-time row', async ({ page, request }) => {
    key = await openSeededChat(page, request, 'reader-collapse')
    await settleAtBottom(page)
    await releaseReader(page, 0.5)
    const before = await settled(page)
    const row = (await topRow(page))!
    await installRecorder(page)

    await goHidden(page)
    const hiddenIntact = await geom(page)
    await collapseScroller(page)
    const collapsed = await geom(page)
    await appendTurnWhileHidden(request, key, 'appended while hidden (reader)')
    await page.waitForTimeout(800)
    const collapsedAfterGrowth = await geom(page)
    await restoreScroller(page)
    const restoredBeforeReturn = await geom(page)
    await goVisible(page)
    await expect
      .poll(async () => {
        const off = await rowOffset(page, row.index)
        return off === null ? Number.POSITIVE_INFINITY : Math.abs(off - row.top)
      }, { timeout: 5_000 })
      .toBeLessThanOrEqual(PX_TOLERANCE)
    const after = await settled(page)
    const rowAfter = await rowOffset(page, row.index)

    await report(page, 'reader-collapse', {
      before, hiddenIntact, collapsed, collapsedAfterGrowth, restoredBeforeReturn, after,
      hideTimeTopRow: row, rowOffsetAfter: rowAfter, clientHeightWhileHidden: hiddenIntact.clientHeight,
    })
    expect(collapsed.clientHeight).toBe(0)
    expect(restoredBeforeReturn.clientHeight).toBe(before.clientHeight)
    // The reader's window is mid-transcript, so the appended rows are not
    // mounted (that is the point); the growth is proven by the slot itself.
    const detail = (await (await request.get(`/api/chat/slots/${encodeURIComponent(key)}?limit=1`)).json()) as { total?: number }
    expect(detail.total).toBe(SEEDED_MESSAGES + 2)
    expect(rowAfter).not.toBeNull()
    expect(Math.abs(rowAfter! - row.top)).toBeLessThanOrEqual(PX_TOLERANCE)
    // Not dragged to the live end by the growth.
    expect(distanceFromBottom(after)).toBeGreaterThan(200)
  })

  test('follower: engine relayout (viewport height change) plus growth while hidden; return lands at the live end', async ({ page, request }) => {
    key = await openSeededChat(page, request, 'follower-relayout')
    const before = await settleAtBottom(page)
    const viewport = page.viewportSize()!
    await installRecorder(page)

    await goHidden(page)
    // A REAL relayout by the engine, not an injected style: the box shrinks,
    // and the engine decides what happens to scrollTop.
    await page.setViewportSize({ width: viewport.width, height: Math.round(viewport.height / 2) })
    const shrunk = await geom(page)
    await appendTurnWhileHidden(request, key, 'appended while hidden (relayout)')
    await page.waitForTimeout(800)
    const shrunkAfterGrowth = await geom(page)
    await page.setViewportSize(viewport)
    const restoredBeforeReturn = await geom(page)
    await goVisible(page)
    await expect.poll(() => geom(page).then(distanceFromBottom), { timeout: 5_000 }).toBeLessThanOrEqual(PX_TOLERANCE)
    const after = await settled(page)

    await report(page, 'follower-relayout', {
      before, shrunk, shrunkAfterGrowth, restoredBeforeReturn, after,
    })
    expect(shrunk.clientHeight).toBeLessThan(before.clientHeight)
    expect(after.clientHeight).toBe(before.clientHeight)
    // Growth is asserted on the ROWS, not on scrollHeight: with virtualized
    // rows the total is estimated heights plus measured ones, so mounting a
    // different window can shrink it even as two rows were appended.
    expect(after.mounted.last).toBe(before.mounted.last + 2)
    expect(distanceFromBottom(after)).toBeLessThanOrEqual(PX_TOLERANCE)
    expect(after.lastRowText).toContain(APPENDED_REPLY)
  })
})
