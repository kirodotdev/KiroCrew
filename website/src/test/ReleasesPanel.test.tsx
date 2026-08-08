// Settings > Releases: the changelog archive.
//
// Contract under test:
// - the release the running build belongs to is selected by DEFAULT, so a
//   prerelease user does not land on someone else's older version (the defect
//   this panel replaces: a 0.2.0-nightly build showed [0.1.2] as the newest
//   entry, headlined "First public release")
// - a version with no changelog section stays SELECTABLE and explains the
//   absence, rather than being an inert row the reader cannot tell from a
//   broken one
// - the in-progress row is badged and offers a commit range instead of prose
// - the list-is-from-your-build caveat shows only when it can actually bite
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import ReleasesPanel from '../pages/settings/ReleasesPanel'
import { api } from '../api/client'

const NOTES = 'First public release.\n\n### Chat from wherever\n\n- One agent, ten ways in\n'

/** What /api/releases returns for a nightly build of an unreleased 0.2.0. */
const PRERELEASE = {
  current_version: '0.2.0-nightly.20260806t065257',
  stale: true,
  releases: [
    { version: '0.2.0', date: '', body: '', is_current: true, in_progress: true },
    { version: '0.1.2', date: '2026-07-30', body: NOTES, is_current: false, in_progress: false },
  ],
}

/** A released build whose version never got a changelog section (v0.1.3 shipped this way). */
const STABLE_NO_NOTES = {
  current_version: '0.1.3',
  stale: false,
  releases: [
    { version: '0.1.3', date: '', body: '', is_current: true, in_progress: false },
    { version: '0.1.2', date: '2026-07-30', body: NOTES, is_current: false, in_progress: false },
  ],
}

function mount(payload: unknown) {
  vi.spyOn(api, 'releases').mockResolvedValue(payload as never)
  return renderPanel()
}

/** The gateway answered non-2xx, so `api.releases()` rejects and nothing is cached. */
function mountFailing() {
  vi.spyOn(api, 'releases').mockRejectedValue(new Error('500 Internal Server Error'))
  return renderPanel()
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ReleasesPanel />
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ReleasesPanel', () => {
  beforeEach(() => vi.restoreAllMocks())
  afterEach(() => cleanup())

  it('selects the running build’s release by default, not the newest with notes', async () => {
    mount(PRERELEASE)
    // 0.2.0 is the in-progress row and must win the default selection even
    // though 0.1.2 is the only row carrying prose.
    await waitFor(() => expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('0.2.0'))
    expect(screen.getByRole('heading', { level: 3 })).not.toHaveTextContent('0.1.2')
  })

  it('states which prerelease you are on, since the row itself says only 0.2.0', async () => {
    mount(PRERELEASE)
    await waitFor(() =>
      expect(screen.getAllByText(/0\.2\.0-nightly\.20260806t065257/).length).toBeGreaterThan(0),
    )
  })

  it('compares an unreleased version against the branch, not a tag that does not exist', async () => {
    mount(PRERELEASE)
    const link = await screen.findByRole('link')
    // `v0.2.0` is not tagged while 0.2.0 is in progress, so `compare/v0.1.2...v0.2.0`
    // 404s -- on the one row every prerelease reader lands on.
    expect(link).toHaveAttribute('href', expect.stringContaining('/compare/v0.1.2...main'))
  })

  it('compares a RELEASED notes-less version against its own tag', async () => {
    mount(STABLE_NO_NOTES)
    const link = await screen.findByRole('link')
    expect(link).toHaveAttribute('href', expect.stringContaining('/compare/v0.1.2...v0.1.3'))
  })

  it('keeps a notes-less row selectable so the absence can be explained', async () => {
    mount(STABLE_NO_NOTES)
    await waitFor(() => expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('0.1.3'))
    // Selectable in both directions: away to a row WITH notes and back again.
    fireEvent.click(screen.getByText('0.1.2'))
    await waitFor(() => expect(screen.getByText(/First public release/)).toBeInTheDocument())
    fireEvent.click(screen.getByText('0.1.3'))
    await waitFor(() => expect(screen.getByRole('link')).toBeInTheDocument())
  })

  it('renders the section body when the selected version has one', async () => {
    mount(STABLE_NO_NOTES)
    await waitFor(() => expect(screen.getByText('0.1.2')).toBeInTheDocument())
    fireEvent.click(screen.getByText('0.1.2'))
    await waitFor(() => expect(screen.getByText(/First public release/)).toBeInTheDocument())
    // A row with prose must NOT also offer the empty-state fallback.
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('warns the list came from this build only when a prerelease is running', async () => {
    const { unmount } = mount(PRERELEASE)
    await waitFor(() => expect(screen.getByText(/may not include newer entries/)).toBeInTheDocument())
    unmount()
    cleanup()

    mount(STABLE_NO_NOTES)
    await waitFor(() => expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('0.1.3'))
    expect(screen.queryByText(/may not include newer entries/)).toBeNull()
  })

  it('reports unavailability instead of rendering an empty shell', async () => {
    mount({ current_version: '', stale: false, releases: [] })
    await waitFor(() => expect(screen.getByText(/No release notes are available/)).toBeInTheDocument())
  })

  it('separates a failed fetch from an archive with nothing in it', async () => {
    // Both states arrive as zero rows, and "not available in this build" sends
    // the reader hunting a build problem when the gateway simply 500'd.
    mountFailing()
    await waitFor(() => expect(screen.getByText(/Could not load the release notes/)).toBeInTheDocument())
    expect(screen.queryByText(/No release notes are available/)).toBeNull()
  })
})
