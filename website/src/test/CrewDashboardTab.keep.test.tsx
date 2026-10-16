// @vitest-environment jsdom
/**
 * The crewmate's Dashboard tab on a preview link (`?dashboard=preview`): the
 * person's Keep and Discard, and the hand-over to the live page once the staged
 * page is gone -- by this tab's Keep or by the agent's `dashboard_apply`.
 *
 * The router is real and the URL is read back, because the bug this pins was a
 * URL that kept asking for the preview after the page had been kept.
 */
import { Suspense } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation, useSearchParams } from 'react-router-dom'
import { api, type DashboardManifest } from '../api/client'
import { ApiError } from '../api/apiError'
import { ThemeProvider } from '../hooks/useTheme'
import { createTestStore } from './helpers'
import CrewDashboardTab from '../pages/members/CrewDashboardTab'
import { isDashboardPreview, withoutDashboardPreview } from '../pages/members/dashboardPreview'

/** The mint, held: `hold` keeps the last url on screen and reports a pending mint,
 *  which is what the real hook does while a newer document is being minted. */
const mint = vi.hoisted(() => ({ hold: false, last: null as string | null }))

vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => {
    if (mint.hold) return { url: mint.last, pending: true, failed: false, stalled: false, retry: vi.fn() }
    const url = srcdoc
      ? '/sandbox-doc/' + (srcdoc.includes('kept') ? 'kept' : srcdoc.includes('restaged') ? 'restaged' : 'staged')
      : null
    mint.last = url
    return { url, pending: false, failed: false, stalled: false, retry: vi.fn() }
  },
}))

/** The router's navigation, held. React Router 7 commits a navigation inside
 *  `startTransition`, so the tab's own note (an ordinary update) can be on screen
 *  while the URL still asks for the preview. `hold` keeps that navigation pending,
 *  which is what a loaded runner does to it, until the test releases it. */
const nav = vi.hoisted(() => ({ hold: null as null | { promise: Promise<void>; release: () => void } }))

function holdNavigation() {
  let release = () => {}
  const promise = new Promise<void>((resolve) => { release = () => { nav.hold = null; resolve() } })
  nav.hold = { promise, release }
  return () => release()
}

/** Suspends the render that leaves the preview while the navigation is held. */
function NavigationGate() {
  const { search } = useLocation()
  if (nav.hold && !search.includes('dashboard=preview')) throw nav.hold.promise
  return null
}

const LAZY = { timeout: 5000 }
const MANIFEST = { id: 'project-report', version: 1, title: 'Project report', fields: [] } as unknown as DashboardManifest
const STAGED_MS = 1_760_000_000_000

function body(page: 'staged' | 'restaged' | 'kept', extra: Record<string, unknown> = {}) {
  return {
    instance_version: page === 'kept' ? 4 : 3,
    template: { id: 'project-report', version: 1 },
    html: '<!doctype html><p>stored</p>',
    rendered_html: `<!doctype html><title>${page}</title><p>${page}</p>`,
    manifest: MANIFEST,
    state: 'live' as const,
    ...extra,
  }
}

/** The frame DRAWS the page: its `load`, then the page's ready beacon. Keep and
 *  Discard stay disabled until both, so a test that clicks them draws first. */
async function drawFrame() {
  await waitFor(() => expect(document.querySelector('[data-testid="crew-dashboard-iframe"]')).not.toBeNull(), { timeout: 5000 })
  const frame = document.querySelector('[data-testid="crew-dashboard-iframe"]') as HTMLIFrameElement
  fireEvent.load(frame)
  window.dispatchEvent(new MessageEvent('message', { data: { type: 'kirocrew-dashboard:ready' }, source: frame.contentWindow }))
}

const NO_PREVIEW = () => new ApiError(404, 'nothing is staged to preview', JSON.stringify({ error: 'nothing is staged to preview', code: 'no_preview' }))

/** The tab as MembersPage drives it: `preview` from the URL, `onLeavePreview` dropping it. */
function Host() {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  return (
    <>
      <span data-testid="url">{location.search}</span>
      <NavigationGate />
      {/* A later preview link, as MembersPage's opener writes it. */}
      <button type="button" data-testid="reopen" onClick={() => setParams({ member: 'Atlas', dashboard: 'preview' }, { replace: true })}>reopen</button>
      <CrewDashboardTab
        slug="atlas"
        member="Atlas"
        displayName="Atlas"
        preview={isDashboardPreview(params)}
        onLeavePreview={() => setParams(withoutDashboardPreview, { replace: true })}
      />
    </>
  )
}

function renderHost() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/members?member=Atlas&dashboard=preview']}>
            <Suspense fallback={null}>
              <Host />
            </Suspense>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return queryClient
}

function frameSrc(): string | null {
  return screen.getByTestId('crew-dashboard-frame').querySelector('iframe')?.getAttribute('src') ?? null
}

describe('CrewDashboardTab on a preview link', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    mint.hold = false
    mint.last = null
    nav.hold = null
  })

  it('Keep names the page the frame has MOUNTED, never a newer read still being minted', async () => {
    let restaged = false
    vi.spyOn(api, 'memberDashboard').mockImplementation(async () => (restaged
      ? body('restaged', { preview: true, staged_ms: STAGED_MS + 1 })
      : body('staged', { preview: true, staged_ms: STAGED_MS })))
    const apply = vi.spyOn(api, 'memberDashboardPreviewApply').mockResolvedValue({ ok: true, instance_version: 4, template_id: 'project-report' })
    const queryClient = renderHost()
    const keep = await screen.findByTestId('dashboard-preview-keep', undefined, LAZY)
    await waitFor(async () => { await drawFrame(); expect(keep).not.toBeDisabled() }, LAZY)
    // The crewmate stages again; the frame still shows A while B is minted.
    mint.hold = true
    restaged = true
    await queryClient.invalidateQueries({ queryKey: ['member-dashboard', 'atlas'] })
    await waitFor(() => expect(keep).toBeDisabled(), LAZY)
    expect(screen.getByTestId('dashboard-preview-discard')).toBeDisabled()
    fireEvent.click(keep)
    expect(apply).not.toHaveBeenCalled()
    // B lands in the frame: now Keep names B.
    mint.hold = false
    await queryClient.invalidateQueries({ queryKey: ['member-dashboard', 'atlas'] })
    await waitFor(() => expect(frameSrc()).toBe('/sandbox-doc/restaged'), LAZY)
    await waitFor(async () => { await drawFrame(); expect(keep).not.toBeDisabled() }, LAZY)
    fireEvent.click(keep)
    await waitFor(() => expect(apply).toHaveBeenCalledWith('atlas', 'Atlas', STAGED_MS + 1))
  })

  it('Keep and Discard do nothing until the frame has loaded the page and it sent its ready beacon', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(body('staged', { preview: true, staged_ms: STAGED_MS }))
    const apply = vi.spyOn(api, 'memberDashboardPreviewApply')
    const discard = vi.spyOn(api, 'memberDashboardPreviewDiscard')
    renderHost()
    const keep = await screen.findByTestId('dashboard-preview-keep', undefined, LAZY)
    await waitFor(() => expect(frameSrc()).toBe('/sandbox-doc/staged'), LAZY)
    // The url is on the frame, but its document has not loaded or beaconed yet.
    fireEvent.click(keep)
    fireEvent.click(screen.getByTestId('dashboard-preview-discard'))
    expect(keep).toBeDisabled()
    // Loaded but not yet ready: still nothing.
    fireEvent.load(screen.getByTestId('crew-dashboard-iframe'))
    fireEvent.click(keep)
    expect(apply).not.toHaveBeenCalled()
    expect(discard).not.toHaveBeenCalled()
    await waitFor(async () => { await drawFrame(); expect(keep).not.toBeDisabled() }, LAZY)
  })

  it('a later preview link shows the newly staged page, not the cached "nothing staged"', async () => {
    let stagedAgain = false
    vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (!preview) return body('kept')
      if (!stagedAgain) throw NO_PREVIEW()
      return body('restaged', { preview: true, staged_ms: STAGED_MS + 1 })
    })
    renderHost()
    // The agent applied the last page: the tab handed over to the live page.
    await waitFor(() => expect(screen.getByTestId('url').textContent).toBe('?member=Atlas'), LAZY)
    // It stages another, and the person opens the new link.
    stagedAgain = true
    fireEvent.click(screen.getByTestId('reopen'))
    expect(await screen.findByTestId('dashboard-preview-keep', undefined, LAZY)).toBeInTheDocument()
    await waitFor(() => expect(frameSrc()).toBe('/sandbox-doc/restaged'), LAZY)
    expect(screen.getByTestId('url').textContent).toContain('dashboard=preview')
    expect(screen.queryByTestId('dashboard-preview-outcome')).toBeNull()
  })

  it('a preview that ends while open (applied by the mate or expired) says so, with no undo line', async () => {
    let ended = false
    vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (preview) {
        if (ended) throw NO_PREVIEW()
        return body('staged', { preview: true, staged_ms: STAGED_MS })
      }
      return body('kept')
    })
    const queryClient = renderHost()
    await waitFor(async () => { await drawFrame(); expect(screen.getByTestId('dashboard-preview-keep')).not.toBeDisabled() }, LAZY)
    ended = true
    await queryClient.invalidateQueries({ queryKey: ['member-dashboard', 'atlas'] })
    const note = await screen.findByTestId('dashboard-preview-outcome', undefined, LAZY)
    expect(note).toHaveTextContent('Preview ended. This is the live page now.')
    expect(note).not.toHaveTextContent('To undo')
  })

  it('Keep applies the staged page, leaves preview at once and mounts the live page', async () => {
    let kept = false
    const read = vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (preview) {
        if (kept) throw NO_PREVIEW()
        return body('staged', { preview: true, staged_ms: STAGED_MS, structure_locked: false })
      }
      return body(kept ? 'kept' : 'staged')
    })
    const apply = vi.spyOn(api, 'memberDashboardPreviewApply').mockImplementation(async () => {
      kept = true
      return { ok: true, instance_version: 4, template_id: 'project-report' }
    })
    renderHost()
    const keep = await screen.findByTestId('dashboard-preview-keep', undefined, LAZY)
    await waitFor(async () => { await drawFrame(); expect(keep).not.toBeDisabled() }, LAZY)
    // Outside the frame: the buttons are the host's, not the page's.
    expect(screen.getByTestId('crew-dashboard-frame').contains(keep)).toBe(false)
    fireEvent.click(keep)
    await waitFor(() => expect(apply).toHaveBeenCalledWith('atlas', 'Atlas', STAGED_MS))
    await waitFor(() => expect(screen.getByTestId('url').textContent).toBe('?member=Atlas'))
    const keptNote = await screen.findByTestId('dashboard-preview-outcome', undefined, LAZY)
    expect(keptNote).toHaveAttribute('data-outcome', 'kept')
    expect(keptNote).toHaveTextContent('Kept. This is the live page now. To undo, ask Atlas to restore the previous dashboard.')
    // The live read ran after the keep, and its page is the one on screen.
    await waitFor(() => expect(read.mock.calls.some(([, , , preview]) => !preview)).toBe(true))
    await waitFor(() => expect(frameSrc()).toBe('/sandbox-doc/kept'), LAZY)
    expect(screen.queryByTestId('crew-dashboard-preview-gone')).toBeNull()
    expect(screen.queryByTestId('dashboard-preview-keep')).toBeNull()
    expect(screen.queryByTestId('dashboard-preview-badge')).toBeNull()
  })

  it("hands over to the live page when the agent's dashboard_apply took the staged page", async () => {
    // The agent applied it: the preview read now answers 404 `no_preview`.
    const read = vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (preview) throw NO_PREVIEW()
      return body('kept')
    })
    renderHost()
    await waitFor(() => expect(screen.getByTestId('url').textContent).toBe('?member=Atlas'), LAZY)
    expect(await screen.findByTestId('dashboard-preview-outcome', undefined, LAZY)).toHaveAttribute('data-outcome', 'closed')
    await waitFor(() => expect(read.mock.calls.some(([, , , preview]) => !preview)).toBe(true))
    await waitFor(() => expect(frameSrc()).toBe('/sandbox-doc/kept'), LAZY)
    expect(screen.queryByTestId('crew-dashboard-preview-gone')).toBeNull()
  })

  it('stays on the preview and says locked when the gateway refuses with dashboard_locked', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(body('staged', { preview: true, staged_ms: STAGED_MS, structure_locked: false }))
    vi.spyOn(api, 'memberDashboardPreviewApply').mockRejectedValue(
      new ApiError(423, 'locked', JSON.stringify({ error: 'locked', code: 'dashboard_locked' })),
    )
    renderHost()
    const keep = await screen.findByTestId('dashboard-preview-keep', undefined, LAZY)
    await waitFor(async () => { await drawFrame(); expect(keep).not.toBeDisabled() }, LAZY)
    fireEvent.click(keep)
    const refused = await screen.findByTestId('dashboard-preview-problem')
    expect(refused).toHaveTextContent('Layout locked — unlock on Profile')
    // The server's refusal is an error, through the shared error surface.
    expect(refused.closest('[role="alert"]')).not.toBeNull()
    expect(screen.getByTestId('url').textContent).toContain('dashboard=preview')
    expect(screen.queryByTestId('dashboard-preview-outcome')).toBeNull()
  })

  it('a Keep that fails stays on the preview and says so through the shared error surface', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(body('staged', { preview: true, staged_ms: STAGED_MS }))
    vi.spyOn(api, 'memberDashboardPreviewApply').mockRejectedValue(
      new ApiError(500, 'boom', JSON.stringify({ error: 'boom', code: 'dashboard_failed' })),
    )
    renderHost()
    const keep = await screen.findByTestId('dashboard-preview-keep', undefined, LAZY)
    await waitFor(async () => { await drawFrame(); expect(keep).not.toBeDisabled() }, LAZY)
    fireEvent.click(keep)
    const problem = await screen.findByTestId('dashboard-preview-problem')
    expect(problem).toHaveTextContent("Couldn't keep this preview")
    expect(problem.closest('[role="alert"]')).not.toBeNull()
    expect(screen.getByTestId('url').textContent).toContain('dashboard=preview')
  })

  it('Discard drops the staged page and goes back to the live one', async () => {
    let dropped = false
    vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (preview) {
        if (dropped) throw NO_PREVIEW()
        return body('staged', { preview: true, staged_ms: STAGED_MS })
      }
      return body('kept')
    })
    const discard = vi.spyOn(api, 'memberDashboardPreviewDiscard').mockImplementation(async () => {
      dropped = true
      return { ok: true }
    })
    renderHost()
    const discardButton = await screen.findByTestId('dashboard-preview-discard', undefined, LAZY)
    await waitFor(async () => { await drawFrame(); expect(discardButton).not.toBeDisabled() }, LAZY)
    fireEvent.click(discardButton)
    await waitFor(() => expect(discard).toHaveBeenCalledWith('atlas', 'Atlas', STAGED_MS))
    expect(await screen.findByTestId('dashboard-preview-outcome', undefined, LAZY)).toHaveAttribute('data-outcome', 'discarded')
    await waitFor(() => expect(screen.getByTestId('url').textContent).toBe('?member=Atlas'))
  })

  it('Discard leaves the preview URL when the navigation commits after the note', async () => {
    let dropped = false
    vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (preview) {
        if (dropped) throw NO_PREVIEW()
        return body('staged', { preview: true, staged_ms: STAGED_MS })
      }
      return body('kept')
    })
    vi.spyOn(api, 'memberDashboardPreviewDiscard').mockImplementation(async () => {
      dropped = true
      return { ok: true }
    })
    renderHost()
    const discardButton = await screen.findByTestId('dashboard-preview-discard', undefined, LAZY)
    await waitFor(async () => { await drawFrame(); expect(discardButton).not.toBeDisabled() }, LAZY)
    const release = holdNavigation()
    fireEvent.click(discardButton)
    // The note is an ordinary update and lands first; the URL is the router's
    // transition and is still the preview link, so reading it now proves nothing.
    expect(await screen.findByTestId('dashboard-preview-outcome', undefined, LAZY)).toHaveAttribute('data-outcome', 'discarded')
    expect(screen.getByTestId('url').textContent).toBe('?member=Atlas&dashboard=preview')
    release()
    await waitFor(() => expect(screen.getByTestId('url').textContent).toBe('?member=Atlas'))
    expect(screen.getByTestId('dashboard-preview-outcome')).toHaveAttribute('data-outcome', 'discarded')
    expect(screen.queryByTestId('dashboard-preview-discard')).toBeNull()
  })
})

describe('CrewDashboardTab on the live page', () => {
  it('draws no Keep or Discard', async () => {
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(body('kept'))
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Provider store={createTestStore()}>
          <ThemeProvider>
            <MemoryRouter>
              <CrewDashboardTab slug="atlas" member="Atlas" displayName="Atlas" />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    expect(await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)).toBeInTheDocument()
    expect(screen.queryByTestId('dashboard-preview-actions')).toBeNull()
  })
})
