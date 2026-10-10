// @vitest-environment jsdom
/**
 * The side panel's view of a crewmate's STAGED dashboard: it says the page is not
 * applied, it renders the staged read (never the live one), it offers no way to
 * apply from here, and it keeps an "open in browser" fallback on the URL.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
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
    renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    expect(await screen.findByTestId('dashboard-preview-badge', undefined, LAZY)).toHaveTextContent('Preview, not applied')
  })

  it('reads the STAGED page for the member the roster names', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    await waitFor(() => expect(read).toHaveBeenCalledWith('atlas', 'Atlas', 'en', true), LAZY)
    expect(read.mock.calls.every(call => call[3] === true)).toBe(true)
    expect(await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)).toBeInTheDocument()
  })

  it('applies nothing and offers no Apply control', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    expect(await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /apply/i })).toBeNull()
    const posted = fetchSpy.mock.calls.map(([url]) => String(url))
    expect(posted.some(url => url.includes('/dashboard/apply'))).toBe(false)
  })

  it('reports a failed roster refresh even over a cached crewmate name', async () => {
    const members = roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    const { queryClient } = renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)
    members.mockRejectedValue(new Error('gateway restarting'))
    await queryClient.refetchQueries({ queryKey: ['kirocrew-agents', 'members-roster'] }).catch(() => {})
    expect(await screen.findByTestId('dashboard-preview-roster-error', undefined, LAZY)).toBeInTheDocument()
  })

  it('offers no browser link, because the preview URL is JSON, not a page', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    await screen.findByTestId('crew-dashboard-frame', undefined, LAZY)
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('says the roster read failed instead of claiming no crewmate', async () => {
    vi.spyOn(api, 'members').mockRejectedValue(new Error('gateway restarting'))
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    expect(await screen.findByTestId('dashboard-preview-roster-error', undefined, LAZY)).toBeInTheDocument()
    expect(screen.queryByTestId('dashboard-preview-no-member')).toBeNull()
    expect(read).not.toHaveBeenCalled()
    // No agent hand-off: it navigates away from unsaved side-panel edits.
    expect(screen.queryByRole('button', { name: /agent/i })).toBeNull()
  })

  it('says plainly that an applied or expired preview is gone, with no Retry', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockRejectedValue(
      new ApiError(404, 'nothing is staged to preview', JSON.stringify({ error: 'nothing is staged to preview', code: 'no_preview' })),
    )
    renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    expect(await screen.findByTestId('crew-dashboard-preview-gone', undefined, LAZY)).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dashboard-error')).toBeNull()
    expect(screen.queryByTestId('crew-dashboard-error-retry')).toBeNull()
    // "Not applied" over "it was applied" would contradict itself.
    await waitFor(() => expect(screen.queryByTestId('dashboard-preview-badge')).toBeNull())
  })

  it('wraps its header so the hint keeps a full line in a narrow panel', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }])
    vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    const badge = await screen.findByTestId('dashboard-preview-badge', undefined, LAZY)
    expect(badge.parentElement).toHaveClass('flex-wrap')
    expect(screen.getByText(/Not live yet/)).toHaveClass('basis-full')
  })

  it('withholds the page when no single roster name owns the slug', async () => {
    roster([{ name: 'Atlas', slug: 'atlas' }, { name: 'ATLAS', slug: 'atlas' }])
    const read = vi.spyOn(api, 'memberDashboard').mockResolvedValue(staged())
    renderWithProviders(<DashboardPreviewPanel slug="atlas" />)
    expect(await screen.findByTestId('dashboard-preview-no-member', undefined, LAZY)).toBeInTheDocument()
    expect(read).not.toHaveBeenCalled()
    expect(screen.getByTestId('dashboard-preview-badge')).toBeInTheDocument()
  })
})
