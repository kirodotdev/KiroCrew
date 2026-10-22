// @vitest-environment jsdom
/**
 * The side panel's view of a crewmate's STAGED dashboard: it says the page is not
 * applied, it renders the staged read until the person's Keep or Discard (drawn
 * outside the frame), and once the staged page is gone it shows the live page.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { announceDashboardPreviewOpened, dashboardPreviewRef } from '../utils/dashboardPreview'
import { api, type DashboardManifest, type MemberRosterRow } from '../api/client'
import { ApiError } from '../api/apiError'
import { renderWithProviders } from './helpers'
import DashboardPreviewPanel from '../pages/chat/DashboardPreviewPanel'

vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? '/sandbox-doc/0' : null,
    pending: false,
    failed: false,
    stalled: false,
    retry: vi.fn(),
  }),
}))

/** The body sits behind React.lazy: the wait covers the dynamic import, then the
 *  roster read, then the dashboard read. A loaded runner can take past the 1s default. */
const LAZY = { timeout: 5000 }

const MANIFEST = { id: 'project-report', version: 1, title: 'Project report', fields: [] } as unknown as DashboardManifest

function staged() {
  return {
    instance_version: 3,
    template: { id: 'project-report', version: 1 },
    html: '<!doctype html><p>stored</p>',
    rendered_html: '<!doctype html><title>staged</title><p>staged</p>',
    manifest: MANIFEST,
    state: 'live' as const,
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

function roster(rows: Array<Pick<MemberRosterRow, 'name' | 'slug'>>) {
  return vi.spyOn(api, 'members').mockResolvedValue({ members: rows as MemberRosterRow[] })
}

describe('DashboardPreviewPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('labels the page as a preview that is not applied', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    expect(await screen.findByTestId('dashboard-preview-badge', undefined, LAZY)).toHaveTextContent('Preview, not applied')
  })

  it('reads a root session\'s STAGED page by its slot, with no roster read', async () => {
    const members = roster([])
    const read = vi.spyOn(api, 'sessionDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'session', slot: 'chat-7' }} />)
    await waitFor(() => expect(read).toHaveBeenCalledWith('chat-7', 'en', true), LAZY)
    expect(await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)).toBeInTheDocument()
    expect(screen.getByTestId('dashboard-preview-badge')).toHaveTextContent('Preview, not applied')
    expect(members).not.toHaveBeenCalled()
  })

  it('reads the STAGED page for the member the roster names', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    await waitFor(() => expect(read).toHaveBeenCalledWith('atlas', 'Atlas', 'en', true), LAZY)
    expect(read.mock.calls.every(call => call[3] === true)).toBe(true)
    expect(await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)).toBeInTheDocument()
  })

  it('a page link names the mate by exact name, never by a guessed slug', async () => {
    roster([{ name: 'BI Buddy', slug: 'bi-buddy' }, { name: 'bi buddy 2', slug: 'bi-buddy-2' }])
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member-name', member: 'BI Buddy' }} />)
    await waitFor(() => expect(read).toHaveBeenCalledWith('bi-buddy', 'BI Buddy', 'en', true), LAZY)
  })

  it('a page link naming no mate shows no page and says so', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member-name', member: 'Ghost' }} />)
    expect(await screen.findByTestId('dashboard-preview-no-member', undefined, LAZY)).toBeInTheDocument()
    expect(read).not.toHaveBeenCalled()
  })

  it('applies nothing on its own: Keep and Discard sit outside the frame until pressed', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue({ ...staged(), preview: true, staged_ms: 42 })
    const apply = vi.spyOn(api, 'memberDashboardPreviewApply')
    const discard = vi.spyOn(api, 'memberDashboardPreviewDiscard')
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    const frame = await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)
    const keep = screen.getByTestId('dashboard-preview-keep')
    expect(frame.contains(keep)).toBe(false)
    expect(frame.contains(screen.getByTestId('dashboard-preview-discard'))).toBe(false)
    expect(apply).not.toHaveBeenCalled()
    expect(discard).not.toHaveBeenCalled()
  })

  it('Keep sends the staged page it was drawn beside, then shows the live page', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    let kept = false
    const read = vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (preview && kept) throw new ApiError(404, 'gone', JSON.stringify({ error: 'gone', code: 'no_preview' }))
      return preview ? { ...staged(), preview: true, staged_ms: 42 } : staged()
    })
    const apply = vi.spyOn(api, 'memberDashboardPreviewApply').mockImplementation(async () => {
      kept = true
      return { ok: true, instance_version: 4, template_id: 'project-report' }
    })
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    const keep = await screen.findByTestId('dashboard-preview-keep', undefined, LAZY)
    await waitFor(async () => { await drawFrame(); expect(keep).not.toBeDisabled() }, LAZY)
    fireEvent.click(keep)
    await waitFor(() => expect(apply).toHaveBeenCalledWith('atlas', 'Atlas', 42))
    expect(await screen.findByTestId('dashboard-preview-outcome', undefined, LAZY)).toHaveAttribute('data-outcome', 'kept')
    await waitFor(() => expect(read.mock.calls.some(call => !call[3])).toBe(true), LAZY)
    expect(screen.queryByTestId('crew-dashboard-preview-gone')).toBeNull()
    expect(screen.queryByTestId('dashboard-preview-badge')).toBeNull()
  })

  it("a session's staged page has no Keep: that one is answered in chat", async () => {
    vi.spyOn(api, 'sessionDashboard').mockResolvedValue({ ...staged(), preview: true, staged_ms: 42 })
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'session', slot: 'chat-7' }} />)
    expect(await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)).toBeInTheDocument()
    expect(screen.queryByTestId('dashboard-preview-actions')).toBeNull()
  })

  it('reports a failed roster refresh even over a cached mate name', async () => {
    const members = roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    const { queryClient } = renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)
    members.mockRejectedValue(new Error('gateway restarting'))
    await queryClient.refetchQueries({ queryKey: ['kirocrew-agents', 'members-roster'] }).catch(() => {})
    expect(await screen.findByTestId('dashboard-preview-roster-error', undefined, LAZY)).toBeInTheDocument()
  })

  it('offers no browser link, because the preview URL is JSON, not a page', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('says the roster read failed instead of claiming no mate', async () => {
    vi.spyOn(api, 'members').mockRejectedValue(new Error('gateway restarting'))
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    expect(await screen.findByTestId('dashboard-preview-roster-error', undefined, LAZY)).toBeInTheDocument()
    expect(screen.queryByTestId('dashboard-preview-no-member')).toBeNull()
    expect(read).not.toHaveBeenCalled()
    // No agent hand-off: it navigates away from unsaved side-panel edits.
    expect(screen.queryByRole('button', { name: /agent/i })).toBeNull()
  })

  it('hands an applied or expired preview over to the live page, with one line and no Retry', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    const read = vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (preview) {
        throw new ApiError(404, 'nothing is staged to preview', JSON.stringify({ error: 'nothing is staged to preview', code: 'no_preview' }))
      }
      return staged()
    })
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    expect(await screen.findByTestId('dashboard-preview-outcome', undefined, LAZY)).toHaveAttribute('data-outcome', 'closed')
    await waitFor(() => expect(read.mock.calls.some(call => !call[3])).toBe(true), LAZY)
    expect(await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-preview-gone')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-error-retry')).toBeNull()
    // "Not applied" over "it was applied" would contradict itself.
    expect(screen.queryByTestId('dashboard-preview-badge')).toBeNull()
  })

  it('a later preview link for the same mate puts the open panel back on the staged page', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    let staging = false
    vi.spyOn(api, 'memberDashboard').mockImplementation(async (_s, _m, _l, preview) => {
      if (preview && !staging) throw new ApiError(404, 'gone', JSON.stringify({ error: 'gone', code: 'no_preview' }))
      return preview ? { ...staged(), preview: true, staged_ms: 7 } : staged()
    })
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    await screen.findByTestId('dashboard-preview-outcome', undefined, LAZY)
    staging = true
    act(() => announceDashboardPreviewOpened(dashboardPreviewRef({ kind: 'member', slug: 'atlas' })))
    expect(await screen.findByTestId('dashboard-preview-badge', undefined, LAZY)).toBeInTheDocument()
    expect(screen.queryByTestId('dashboard-preview-outcome')).toBeNull()
    expect(await screen.findByTestId('dashboard-preview-keep', undefined, LAZY)).toBeInTheDocument()
  })

  it('wraps its header so the hint keeps a full line in a narrow panel', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    const badge = await screen.findByTestId('dashboard-preview-badge', undefined, LAZY)
    expect(badge.parentElement).toHaveClass('flex-wrap')
    expect(screen.getByText(/Not live yet/)).toHaveClass('basis-full')
  })

  it('withholds the page when no single roster name owns the slug', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }, { name: 'ATLAS', slug: 'atlas' }])
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel target={{ kind: 'member', slug: 'atlas' }} />)
    expect(await screen.findByTestId('dashboard-preview-no-member', undefined, LAZY)).toBeInTheDocument()
    expect(read).not.toHaveBeenCalled()
    expect(screen.getByTestId('dashboard-preview-badge')).toBeInTheDocument()
  })
})
