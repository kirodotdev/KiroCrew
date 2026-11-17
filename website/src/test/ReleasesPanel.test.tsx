// Settings > About: the release notes section (running release, then a capped
// list of earlier ones).
//
// Contract under test:
// - the release the running build belongs to is the one shown open, so a
//   prerelease user does not land on someone else's older version (the defect
//   this section replaced: a 0.2.0-nightly build showed [0.1.2] as the newest
//   entry, headlined "First public release")
// - a version with no changelog section still opens and explains the absence,
//   rather than being an inert row the reader cannot tell from a broken one
// - the in-progress release is badged and offers a commit range
// - the list-is-from-your-build caveat shows only when it can actually bite
// - earlier releases are capped at five rows, grow five at a time, and open in
//   place; `?release=<version>` opens and reveals one
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { MemoryRouter, useNavigate } from 'react-router-dom'
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

/** A stable build with twelve earlier releases: more than two pages of rows. */
const MANY = {
  current_version: '0.13.0',
  stale: false,
  releases: Array.from({ length: 13 }, (_, i) => ({
    version: `0.${13 - i}.0`,
    date: `2026-10-${String(13 - i).padStart(2, '0')}`,
    body: `Notes for 0.${13 - i}.0.`,
    is_current: i === 0,
    in_progress: false,
  })),
}

function mount(payload: unknown, url = '/settings/about') {
  vi.spyOn(api, 'releases').mockResolvedValue(payload as never)
  return renderPanel(url)
}

/** The gateway answered non-2xx, so `api.releases()` rejects and nothing is cached. */
function mountFailing() {
  vi.spyOn(api, 'releases').mockRejectedValue(new Error('500 Internal Server Error'))
  return renderPanel('/settings/about')
}

function renderPanel(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          <ReleasesPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

const current = () => screen.getByTestId('releases-current')
const rows = () => within(screen.getByRole('list', { name: 'Versions' })).getAllByRole('button')

describe('ReleasesPanel', () => {
  beforeEach(() => vi.restoreAllMocks())
  afterEach(() => cleanup())

  it('heads the section with the running build’s release, not the newest with notes', async () => {
    mount(PRERELEASE)
    // 0.2.0 is the in-progress release and must win even though 0.1.2 is the
    // only one carrying prose.
    await waitFor(() => expect(within(current()).getByRole('heading')).toHaveTextContent('What\'s new in 0.2.0'))
    expect(within(current()).getByRole('heading')).not.toHaveTextContent('0.1.2')
  })

  it('states which prerelease you are on, since the heading itself says only 0.2.0', async () => {
    mount(PRERELEASE)
    await waitFor(() => expect(within(current()).getByText(/You are on the prerelease 0\.2\.0-nightly\.20260806t065257/)).toBeInTheDocument())
  })

  it('compares an unreleased version against the branch, not a tag that does not exist', async () => {
    mount(PRERELEASE)
    await waitFor(() => expect(current()).toBeInTheDocument())
    const link = within(current()).getByRole('link', { name: /View commits/ })
    // `v0.2.0` is not tagged while 0.2.0 is in progress, so `compare/v0.1.2...v0.2.0` 404s.
    expect(link).toHaveAttribute('href', expect.stringContaining('/compare/v0.1.2...main'))
  })

  it('compares a RELEASED notes-less version against its own tag', async () => {
    mount(STABLE_NO_NOTES)
    await waitFor(() => expect(current()).toBeInTheDocument())
    expect(within(current()).getByText(/shipped without release notes/)).toBeInTheDocument()
    expect(within(current()).getByRole('link', { name: /View commits/ }))
      .toHaveAttribute('href', expect.stringContaining('/compare/v0.1.2...v0.1.3'))
  })

  it('links the running release’s commits even when it has notes', async () => {
    mount(MANY)
    await waitFor(() => expect(within(current()).getByText('Notes for 0.13.0.')).toBeInTheDocument())
    expect(within(current()).getByRole('link', { name: /View commits/ }))
      .toHaveAttribute('href', expect.stringContaining('/compare/v0.12.0...v0.13.0'))
  })

  it('opens an earlier release in place, and closes it again', async () => {
    mount(STABLE_NO_NOTES)
    await waitFor(() => expect(rows()).toHaveLength(1))
    const row = rows()[0]
    expect(row).toHaveTextContent('0.1.2')
    expect(row).toHaveTextContent('2026-07-30')
    expect(row).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(/First public release/)).toBeNull()

    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-expanded', 'true')
    const panel = document.getElementById(row.getAttribute('aria-controls')!)!
    expect(within(panel).getByText(/First public release/)).toBeInTheDocument()
    // A row with prose does NOT also offer the empty-state fallback.
    expect(within(panel).queryByRole('link')).toBeNull()

    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText(/First public release/)).toBeNull()
  })

  it('preserves angle-bracket placeholders in an opened release’s code spans', async () => {
    mount({ ...STABLE_NO_NOTES, releases: [STABLE_NO_NOTES.releases[0], { ...STABLE_NO_NOTES.releases[1], body: '`kirocrew app update <name> --source <dir>`' }] })
    await waitFor(() => expect(rows()).toHaveLength(1))
    fireEvent.click(rows()[0])
    expect(screen.getByText('kirocrew app update <name> --source <dir>', { selector: 'code' })).toBeInTheDocument()
  })

  it('sanitizes raw script elements in an opened release after parsing markdown', async () => {
    const { container } = mount({ ...STABLE_NO_NOTES, releases: [STABLE_NO_NOTES.releases[0], { ...STABLE_NO_NOTES.releases[1], body: 'Safe notes.\n\n<script>alert("unsafe")</script>' }] })
    await waitFor(() => expect(rows()).toHaveLength(1))
    fireEvent.click(rows()[0])
    expect(screen.getByText('Safe notes.')).toBeInTheDocument()
    expect(screen.getByText('[unsupported: script]')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
  })

  it('caps earlier releases at five rows and shows five more per click', async () => {
    mount(MANY)
    await waitFor(() => expect(rows()).toHaveLength(5))
    // The running release is the section above, never a row of its own.
    expect(rows().map(r => r.textContent)).not.toContainEqual(expect.stringContaining('0.13.0'))
    expect(screen.getByText('12 releases')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Show 5 more' }))
    expect(rows()).toHaveLength(10)
    // The last page names what is actually left.
    fireEvent.click(screen.getByRole('button', { name: 'Show 2 more' }))
    expect(rows()).toHaveLength(12)
    expect(screen.queryByTestId('releases-show-more')).toBeNull()
  })

  it('moves focus to the first revealed row when the last Show more goes away', async () => {
    mount(MANY)
    await waitFor(() => expect(rows()).toHaveLength(5))
    fireEvent.click(screen.getByRole('button', { name: 'Show 5 more' }))
    expect(document.activeElement).toBe(rows()[5])
    fireEvent.click(screen.getByRole('button', { name: 'Show 2 more' }))
    expect(screen.queryByTestId('releases-show-more')).toBeNull()
    expect(document.activeElement).toBe(rows()[10])
  })

  it('points aria-controls at the notes only while a row is open', async () => {
    mount(STABLE_NO_NOTES)
    await waitFor(() => expect(rows()).toHaveLength(1))
    const row = rows()[0]
    expect(row).not.toHaveAttribute('aria-controls')
    fireEvent.click(row)
    expect(document.getElementById(row.getAttribute('aria-controls')!)).not.toBeNull()
  })

  it('scrolls to and focuses the release notes section for the old bare Releases address', async () => {
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    try {
      mount(MANY, '/settings/about?release=notes')
      const section = await screen.findByTestId('release-notes-section')
      await waitFor(() => expect(document.activeElement).toBe(section))
      expect(scrolled).toContain(section)
      // Nothing opens and no notice shows: it is a section link, not a version.
      expect(rows().every(r => r.getAttribute('aria-expanded') === 'false')).toBe(true)
      expect(screen.queryByTestId('releases-link-missing')).toBeNull()
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('tags the running release as installed and lists newer versions apart from earlier ones', async () => {
    mount({
      current_version: '0.12.0',
      stale: false,
      releases: [
        { version: '0.12.2', date: '2026-11-12', body: 'Two ahead.', is_current: false, in_progress: false },
        { version: '0.12.1', date: '2026-11-09', body: 'One ahead.', is_current: false, in_progress: false },
        { version: '0.12.0', date: '2026-11-05', body: 'Mine.', is_current: true, in_progress: false },
        { version: '0.11.0', date: '2026-10-24', body: 'Older.', is_current: false, in_progress: false },
      ],
    })
    const newer = await screen.findByTestId('releases-newer')
    expect(within(newer).getByRole('heading')).toHaveTextContent('Newer than yours')
    expect(within(newer).getAllByRole('button').map(b => b.textContent)).toEqual([
      expect.stringContaining('0.12.2'), expect.stringContaining('0.12.1'),
    ])
    expect(within(current()).getByTestId('releases-installed')).toHaveTextContent('Installed')
    expect(rows().map(r => r.textContent)).toEqual([expect.stringContaining('0.11.0')])
    expect(screen.getByText('1 release')).toBeInTheDocument()
  })

  it('shows the running release\u2019s summary and folds the rest into Read all notes', async () => {
    mount({
      ...STABLE_NO_NOTES,
      releases: [{ ...STABLE_NO_NOTES.releases[0], body: 'The summary.\n\n### Section\n\n- **A change**: detail' }, STABLE_NO_NOTES.releases[1]],
    })
    await waitFor(() => expect(within(current()).getByText('The summary.')).toBeInTheDocument())
    const more = within(current()).getByTestId('release-read-all')
    expect(more).not.toHaveAttribute('open')
    expect(within(more).getByText('Read all notes')).toBeInTheDocument()
    // The summary is not repeated inside the disclosure.
    expect(within(more).queryByText('The summary.')).toBeNull()
  })

  it('offers no Show more when fewer than five earlier releases exist', async () => {
    mount(STABLE_NO_NOTES)
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(screen.getByText('1 release')).toBeInTheDocument()
    expect(screen.queryByTestId('releases-show-more')).toBeNull()
  })

  it('opens and reveals a linked release beyond the first page', async () => {
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    try {
      mount(MANY, '/settings/about?release=0.4.0')
      // 0.4.0 is the ninth earlier release, so the list grows to the page holding it.
      await waitFor(() => expect(rows()).toHaveLength(10))
      const row = rows().find(r => r.textContent?.includes('0.4.0'))!
      expect(row).toHaveAttribute('aria-expanded', 'true')
      expect(screen.getByText('Notes for 0.4.0.')).toBeInTheDocument()
      await waitFor(() => expect(scrolled).toContain(document.getElementById('release-0.4.0')))
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('reads a prerelease link as its release', async () => {
    mount(MANY, '/settings/about?release=v0.12.0-rc.2')
    await waitFor(() => expect(screen.getByText('Notes for 0.12.0.')).toBeInTheDocument())
  })

  it('says so when a linked version is not in the list', async () => {
    mount(MANY, '/settings/about?release=9.9.9')
    await waitFor(() => expect(screen.getByTestId('releases-link-missing'))
      .toHaveTextContent("Version 9.9.9 is not in this build's release list."))
    // Nothing else moves: still the first page, all collapsed.
    expect(rows()).toHaveLength(5)
    expect(rows().every(r => r.getAttribute('aria-expanded') === 'false')).toBe(true)
  })

  it('clears a removed release link without resetting the mounted panel and handles later links', async () => {
    function Navigation() {
      const navigate = useNavigate()
      return (
        <>
          <button onClick={() => navigate('/settings/about')}>About</button>
          <button onClick={() => navigate('/settings/about?release=0.4.0')}>Link to 0.4.0</button>
        </>
      )
    }
    vi.spyOn(api, 'releases').mockResolvedValue(MANY as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={['/settings/about?release=9.9.9']}>
            <Navigation />
            <ReleasesPanel />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    expect(await screen.findByTestId('releases-link-missing'))
      .toHaveTextContent("Version 9.9.9 is not in this build's release list.")
    const panel = current()
    fireEvent.click(screen.getByRole('button', { name: 'Show 5 more' }))
    const row = rows().find(r => r.textContent?.includes('0.4.0'))!
    fireEvent.click(row)

    fireEvent.click(screen.getByRole('button', { name: 'About', exact: true }))
    await waitFor(() => expect(screen.queryByTestId('releases-link-missing')).toBeNull())
    expect(current()).toBe(panel)
    expect(rows()).toHaveLength(10)
    expect(row).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(row)
    fireEvent.click(screen.getByRole('button', { name: 'Link to 0.4.0' }))
    await waitFor(() => expect(row).toHaveAttribute('aria-expanded', 'true'))
    expect(screen.getByText('Notes for 0.4.0.')).toBeInTheDocument()

    // Removing the parameter must also allow the same link to be handled again.
    fireEvent.click(screen.getByRole('button', { name: 'About', exact: true }))
    fireEvent.click(row)
    fireEvent.click(screen.getByRole('button', { name: 'Link to 0.4.0' }))
    await waitFor(() => expect(row).toHaveAttribute('aria-expanded', 'true'))
    expect(current()).toBe(panel)
  })

  it('explains a linked version that is missing even when no earlier release exists', async () => {
    const only = { current_version: '0.1.3', stale: false, releases: [STABLE_NO_NOTES.releases[0]] }
    mount(only, '/settings/about?release=9.9.9')
    await waitFor(() => expect(within(current()).getByTestId('releases-link-missing'))
      .toHaveTextContent("Version 9.9.9 is not in this build's release list."))
    expect(screen.queryByTestId('releases-earlier')).toBeNull()
  })

  it('warns the list came from this build only when a prerelease is running', async () => {
    const { unmount } = mount(PRERELEASE)
    await waitFor(() => expect(screen.getByText(/may not include newer entries/)).toBeInTheDocument())
    unmount()
    cleanup()

    mount(STABLE_NO_NOTES)
    await waitFor(() => expect(current()).toBeInTheDocument())
    expect(screen.queryByText(/may not include newer entries/)).toBeNull()
  })

  it('reports unavailability instead of rendering an empty shell', async () => {
    mount({ current_version: '', stale: false, releases: [] })
    await waitFor(() => expect(screen.getByText(/No release notes are available/)).toBeInTheDocument())
  })

  it('titles the list plainly when the running version matches no release', async () => {
    mount({ current_version: '', stale: false, releases: [STABLE_NO_NOTES.releases[1]] })
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(screen.queryByTestId('releases-current')).toBeNull()
    expect(screen.getByRole('heading', { name: /^Releases/ })).toBeInTheDocument()
  })

  it('badges an unreleased version with an outline chip, never a solid accent fill', async () => {
    mount(PRERELEASE)
    const badge = await screen.findByText('Unreleased')
    expect(badge.className).toContain('border-accent/40')
    // Catches every fill, including `bg-accent/10` and `bg-accent-subtle`.
    expect(badge.className).not.toContain('bg-accent')
    expect(badge.className).not.toContain('text-accent-fg')
  })

  it('separates a failed fetch from an archive with nothing in it', async () => {
    // Both states arrive as zero rows, and "not available in this build" sends
    // the reader hunting a build problem when the gateway simply 500'd.
    mountFailing()
    await waitFor(() => expect(screen.getByText(/Could not load the release notes/)).toBeInTheDocument())
    expect(screen.queryByText(/No release notes are available/)).toBeNull()
  })
})
