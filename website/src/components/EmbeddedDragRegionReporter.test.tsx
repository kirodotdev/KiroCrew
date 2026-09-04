import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import EmbeddedDragRegionReporter from './EmbeddedDragRegionReporter'
import { setHostModel, type HostModel } from '../store/instancesSlice'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => true) }))
import { isEmbeddedPane } from '../lib/embedded'

function model(over: Partial<HostModel> = {}): HostModel {
  return {
    tabs: [],
    activeId: null,
    self: null,
    macInset: false,
    electron: true,
    pinnedCrews: [],
    stableOrder: false,
    ...over,
  }
}

function storeWith(host: HostModel | null) {
  return createTestStore({
    instances: { warm: {}, activeId: null, mru: [], unread: {}, ready: {}, host },
  })
}

describe('EmbeddedDragRegionReporter', () => {
  let parentPost: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    parentPost = vi.fn()
    // The reporter bails unless it has a real (cross-frame) parent; give it one
    // with a postMessage spy so we can observe the relay.
    Object.defineProperty(window, 'parent', {
      value: { postMessage: parentPost },
      configurable: true,
    })
    // A laid-out header the reporter can measure (computeHeaderDragGaps yields a
    // full-width gap here since happy-dom reports zero-size control rects). Built
    // with the DOM API rather than `.innerHTML` (frontend-security AUTOSDE rule).
    const header = document.createElement('header')
    header.className = 'topbar-glass'
    const btn = document.createElement('button')
    btn.textContent = 'x'
    header.appendChild(btn)
    document.body.replaceChildren(header)
  })

  afterEach(() => {
    delete (window as unknown as { parent?: unknown }).parent
    document.body.replaceChildren()
    // restoreAllMocks (not clearAllMocks) so the requestAnimationFrame spy the
    // "stays silent" test installs is UNINSTALLED, not merely cleared — otherwise
    // the spy lingers on the global for later tests. beforeEach re-asserts the
    // isEmbeddedPane return value, so restoring it here is safe.
    vi.restoreAllMocks()
  })

  const gapsPosts = () =>
    parentPost.mock.calls.filter(c => (c[0] as { type?: string })?.type === 'mc-drag-gaps')

  it('relays mc-drag-gaps to the parent when the host is an Electron window', async () => {
    renderWithProviders(<EmbeddedDragRegionReporter />, { store: storeWith(model()) })
    await waitFor(() => expect(gapsPosts().length).toBeGreaterThan(0))
    expect(gapsPosts()[0][0]).toMatchObject({ type: 'mc-drag-gaps', v: 1 })
    expect(Array.isArray((gapsPosts()[0][0] as { gaps: unknown }).gaps)).toBe(true)
  })

  it('stays silent when the host is not an Electron window', async () => {
    // Assert the SHAPE, not a duration: a broken electron gate would schedule the
    // post via requestAnimationFrame, so spy on it and require it was never
    // called. A bare "0 posts" check could false-green on a post that is merely
    // scheduled-but-not-yet-fired; asserting nothing was scheduled cannot.
    const rafSpy = vi.spyOn(window, 'requestAnimationFrame')
    renderWithProviders(<EmbeddedDragRegionReporter />, { store: storeWith(model({ electron: false })) })
    expect(rafSpy).not.toHaveBeenCalled()
    expect(gapsPosts().length).toBe(0)
  })

  it('re-asserts the gaps when the host relays a fresh model, even with unchanged geometry', async () => {
    // Regression guard for the drop race: the geometry-change dedup must NOT
    // suppress the re-post triggered by a new host model (tab switch / pane
    // re-engagement). Without the re-assert, a host that missed the initial post
    // never receives the gaps and the remote pane stays undraggable.
    const store = storeWith(model())
    renderWithProviders(<EmbeddedDragRegionReporter />, { store })
    await waitFor(() => expect(gapsPosts().length).toBeGreaterThan(0))
    const before = gapsPosts().length

    // Same geometry, new model object — the host re-engaged this pane.
    act(() => {
      store.dispatch(setHostModel(model({ activeId: 'cd-9' })))
    })

    await waitFor(() => expect(gapsPosts().length).toBeGreaterThan(before))
  })
})
