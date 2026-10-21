// @vitest-environment jsdom
/**
 * A RE-MINT MUST NEVER SHOW AN EMPTY FRAME.
 *
 * A read that the open document cannot take as a refill gets a new document, and the
 * new document has its own URL. Pointing the visible frame at it navigates that frame,
 * and until the new document paints the frame shows its own background -- a black flash
 * in the dark theme, on every agent field write and on every fallback read.
 *
 * So the new document loads in a frame off screen first, and the visible frame is
 * replaced only when it has reported that it came up. These tests drive the component
 * with a mint whose URL follows the document, so a different page is a different URL.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { api, type DashboardManifest } from '../../api/client'
import { renderWithProviders } from '../../test/helpers'
import CrewDynamicDashboard, { DATA_MESSAGE_TYPE, READY_MESSAGE_TYPE, READY_TIMEOUT_MS } from './CrewDynamicDashboard'

vi.mock('../../hooks/websocket/dashboardFields', () => ({
  subscribeDashboardFields: () => () => {},
}))

vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? `/sandbox-doc/${srcdoc.length}` : null,
    pending: false,
    failed: false,
    stalled: false,
    retry: () => {},
  }),
}))

const MANIFEST = { id: 'atlas-tree', version: 1, title: 'Tree', fields: [] } as unknown as DashboardManifest
const TARGET = { kind: 'member', slug: 'atlas', member: 'Atlas' } as const
const DASHBOARD_KEY = ['member-dashboard', 'atlas']

/** A composed page with a data island, so a refill has values to post. The same
 *  version every time: the re-mint under test is a same-page one. */
function page(body: string, over: Record<string, unknown> = {}) {
  const island = `<script type="application/json" id="kirocrew-dashboard-data">${JSON.stringify({
    fields: { boards: body.length },
    agentic: [],
    seq: 0,
    stale: false,
    missing: [],
  })}</script>`
  return {
    instance_version: 1,
    template: { id: 'atlas-tree', version: 1 },
    html: '<!doctype html><p>stored</p>',
    rendered_html: `<!doctype html>${island}<p>${body}</p>`,
    manifest: MANIFEST,
    state: 'live' as const,
    ...over,
  }
}

const mount = () => renderWithProviders(<CrewDynamicDashboard target={TARGET} displayName="Atlas" />)

/** Stand a recording window under a frame, the way the sibling tests do: jsdom would
 *  otherwise give every iframe a real, unrecordable one. */
function stubWindow(frame: HTMLElement) {
  const posted: Array<{ type?: string }> = []
  const stub = { postMessage: (message: { type?: string }) => posted.push(message) }
  Object.defineProperty(frame, 'contentWindow', { configurable: true, value: stub })
  return { stub, posted }
}

/** The page's ready beacon, as a message from `source`. */
function beacon(source: unknown, refill: boolean) {
  const event = new MessageEvent('message', { data: { type: READY_MESSAGE_TYPE, refill } })
  Object.defineProperty(event, 'source', { value: source })
  window.dispatchEvent(event)
}

const visible = () => screen.getByTestId('crew-dashboard-iframe')
const offscreen = () => screen.queryByTestId('crew-dashboard-swap')

describe('a re-minted document', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('loads off screen while the open frame keeps showing the old document', async () => {
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page('first'))
      .mockResolvedValue(page('second, longer'))
    const { queryClient } = mount()
    const first = await screen.findByTestId('crew-dashboard-iframe')
    const oldSrc = first.getAttribute('src')

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    const next = await screen.findByTestId('crew-dashboard-swap')
    expect(next.getAttribute('src')).not.toBe(oldSrc)
    // THE POINT: the frame a reader is looking at was not navigated.
    expect(visible()).toBe(first)
    expect(visible().getAttribute('src')).toBe(oldSrc)
  })

  it('replaces the open frame once the new document has said it came up', async () => {
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page('first'))
      .mockResolvedValue(page('second, longer'))
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    const next = await screen.findByTestId('crew-dashboard-swap')
    const newSrc = next.getAttribute('src')
    const { stub } = stubWindow(next)

    // The ready beacon is a one-shot message: the component only acts on it when its
    // staging effect has attached the listener and stageRef points at this frame, and
    // a beacon dispatched before that settles is dropped with nothing to re-send it,
    // so promotion never fires and the off-screen frame stays. Re-send inside waitFor
    // until promotion takes -- a ready report is idempotent, like the staged-preview
    // test below already relies on.
    await waitFor(() => {
      act(() => beacon(stub, false))
      expect(offscreen()).toBeNull()
    })
    // The SAME element moved on screen: a second element at the same url would load
    // the document again, which is the blank frame this exists to avoid.
    expect(visible()).toBe(next)
    expect(visible().getAttribute('src')).toBe(newSrc)
  })

  it('ignores a beacon that does not come from the off-screen frame', async () => {
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page('first'))
      .mockResolvedValue(page('second, longer'))
    const { queryClient } = mount()
    const first = await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    await screen.findByTestId('crew-dashboard-swap')
    const { stub } = stubWindow(first)

    act(() => beacon(stub, false))
    act(() => beacon({}, false))

    expect(offscreen()).not.toBeNull()
  })

  it('replaces the open frame anyway when the new document never reports', async () => {
    // A page whose first script throws sends no beacon. It used to be shown as it
    // was; keeping the old document forever would show stale numbers with no band.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page('first'))
      .mockResolvedValue(page('second, longer'))
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    const next = await screen.findByTestId('crew-dashboard-swap')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS + 100)
    })

    expect(offscreen()).toBeNull()
    expect(visible()).toBe(next)
  })

  it('counts the wait from the first off-screen document, however often reads rebuild it', async () => {
    // Every read that lands while a document is loading rebuilds it under a new url.
    // If each one restarted the wait, writes arriving more often than the wait would
    // keep the old document on screen, with its old numbers, for as long as they last.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page('first'))
      .mockResolvedValueOnce(page('second, longer'))
      .mockResolvedValue(page('third, longer still'))
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    const second = await screen.findByTestId('crew-dashboard-swap')
    const secondSrc = second.getAttribute('src')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS - 2000)
    })
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    // The newer read replaced the document that was loading.
    await waitFor(() => expect(screen.getByTestId('crew-dashboard-swap').getAttribute('src')).not.toBe(secondSrc))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500)
    })

    expect(offscreen()).toBeNull()
  })

  it('replaces the open frame on load when the page runs no script', async () => {
    // A page without script cannot send a beacon; its load event is all there is.
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page('first', { scripts: false }))
      .mockResolvedValue(page('second, longer', { scripts: false }))
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    const next = await screen.findByTestId('crew-dashboard-swap')

    act(() => {
      next.dispatchEvent(new Event('load'))
    })

    await waitFor(() => expect(offscreen()).toBeNull())
    expect(visible()).toBe(next)
  })

  it('keeps the refill ability its beacon reported, so the next read is not a re-mint', async () => {
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page('first'))
      .mockResolvedValueOnce(page('second, longer'))
      .mockResolvedValue(page('third, longer still'))
    const { queryClient } = mount()
    await screen.findByTestId('crew-dashboard-iframe')
    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    const next = await screen.findByTestId('crew-dashboard-swap')
    const { stub, posted } = stubWindow(next)

    // One-shot beacon: re-send inside waitFor until promotion lands, so a beacon that
    // arrives before the staging listener is attached is not simply lost (see the
    // "replaces the open frame" test). A ready report is idempotent; `before` is
    // snapshotted only after promotion, so any re-sent posts are counted into the
    // baseline rather than the +1 the next read must add.
    await waitFor(() => {
      act(() => beacon(stub, true))
      expect(offscreen()).toBeNull()
    })
    // The values the document missed while it loaded are posted into it as it comes up.
    expect(posted.map((m) => m.type)).toContain(DATA_MESSAGE_TYPE)
    const before = posted.length

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })

    await waitFor(() => expect(posted.length).toBe(before + 1))
    expect(offscreen()).toBeNull()
    expect(visible()).toBe(next)
  })

  it('takes a first document straight onto the screen', async () => {
    // Nothing is open yet, so there is nothing to protect and nothing to wait for.
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(page('first'))
    mount()
    await screen.findByTestId('crew-dashboard-iframe')
    expect(offscreen()).toBeNull()
  })
})

/**
 * A KEEP NAMES A PAGE SOMEONE SAW COME UP.
 *
 * On a staged preview the Keep button is offered for the page that is mounted and drawn.
 * A document shown because the wait ran out never said it came up -- its script may have
 * thrown -- so being on screen is not being drawn.
 */
describe('a staged preview that is re-minted', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('is not offered for Keep when it was shown only because the wait ran out', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.spyOn(api, 'memberDashboard')
      .mockResolvedValueOnce(page('first', { staged_ms: 7 }))
      .mockResolvedValue(page('second, longer', { staged_ms: 7 }))
    const onPreviewLoaded = vi.fn()
    const { queryClient } = renderWithProviders(
      <CrewDynamicDashboard target={TARGET} displayName="Atlas" preview onPreviewLoaded={onPreviewLoaded} />,
    )
    const first = await screen.findByTestId('crew-dashboard-iframe')
    const firstWindow = stubWindow(first).stub
    // The first document loads and reports ready: that one IS offered.
    // Repeated until it lands: the frame's drawn state is reset by an effect that may
    // not have run yet when the first report arrives, and a report is idempotent.
    await waitFor(() => {
      act(() => {
        first.dispatchEvent(new Event('load'))
        beacon(firstWindow, false)
      })
      expect(onPreviewLoaded).toHaveBeenLastCalledWith(7)
    })

    await queryClient.invalidateQueries({ queryKey: DASHBOARD_KEY })
    const next = await screen.findByTestId('crew-dashboard-swap')
    const nextWindow = stubWindow(next).stub
    // It loads, but its script never reports.
    act(() => {
      next.dispatchEvent(new Event('load'))
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS + 100)
    })
    expect(offscreen()).toBeNull()
    await waitFor(() => expect(onPreviewLoaded).toHaveBeenLastCalledWith(0))

    // Reporting late still counts: the page did come up.
    await waitFor(() => {
      act(() => beacon(nextWindow, false))
      expect(onPreviewLoaded).toHaveBeenLastCalledWith(7)
    })
  })
})
