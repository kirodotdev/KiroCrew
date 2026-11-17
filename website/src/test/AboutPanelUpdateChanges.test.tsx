// About > Updates: "What you'd get", the changes an available update brings.
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render as rtlRender, screen, cleanup, within, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { AboutPanelUpdateChanges } from '../pages/settings/AboutPanelUpdateChanges'
import { archiveNotes, changeEntries, notesSince } from '../pages/settings/updateChanges'

/** The component reads the shared ['releases'] cache; `archive` seeds it. */
function render(ui: ReactElement, archive: unknown = { releases: [] }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['releases'], archive)
  const wrap = (node: ReactElement) => <QueryClientProvider client={qc}>{node}</QueryClientProvider>
  const utils = rtlRender(wrap(ui))
  return { ...utils, rerender: (next: ReactElement) => utils.rerender(wrap(next)) }
}

/** Shaped like a real CHANGELOG.md section: bullets with bold leads, then bold-led fix paragraphs. */
const SECTION = [
  'Bots are on for everyone, and the browser can be watched live.',
  '',
  '### Bots for everyone',
  '',
  '- **Bots without a preview switch**: Mates are now called Bots.',
  '- **A desk of its own**: each Bot keeps its chats in one folder,',
  '  and works in its own git worktree.',
  '',
  '### Lighter and faster',
  '',
  'Long chats stream with less work.',
  '',
  '### Notable fixes',
  '',
  '**Chat and sessions.** A queued message waits its turn.',
  '',
  '**Windows and Linux.** The gateway no longer freezes.',
].join('\n')

describe('changeEntries', () => {
  it('names each bullet and bold-led paragraph, and a section with neither by its heading', () => {
    expect(changeEntries(SECTION)).toEqual([
      'Bots without a preview switch',
      'A desk of its own',
      'Lighter and faster',
      'Chat and sessions: A queued message waits its turn.',
      'Windows and Linux: The gateway no longer freezes.',
    ])
  })

  it('includes only the first sentence of a bold-led paragraph, including wrapped text', () => {
    expect(changeEntries([
      '**Chat and sessions.** Chats that a planned restart cut off',
      'resume. A second sentence stays in the full notes.',
      '',
      '**Windows:** The gateway no longer freezes.',
      '',
      '- **Live view**: The browser is visible. More detail.',
    ].join('\n'))).toEqual([
      'Chat and sessions: Chats that a planned restart cut off resume.',
      'Windows: The gateway no longer freezes.',
      'Live view',
    ])
  })

  it('reads the lines a git checkout check adds, version headings included', () => {
    const added = '## [0.13.0] - 2026-11-20\n\n- Plain bullet with a [link](https://example.com) and `code`\n\n## [0.12.2] - 2026-11-12\n\n- **Fix**: one'
    expect(changeEntries(added)).toEqual(['Plain bullet with a link and code', 'Fix'])
  })

  it('finds nothing in prose alone or in an empty string', () => {
    expect(changeEntries('')).toEqual([])
    expect(changeEntries('Just a summary paragraph.\nThat wraps.')).toEqual([])
  })
})

describe('notesSince', () => {
  const SECTIONS = '## [0.13.0] - 2026-11-20\n\n- a\n\n## [0.12.1] - 2026-11-09\n\n- b\n\n## [0.12.0] - 2026-11-05\n\n- c'

  it('keeps the sections newer than the running build and calls them a range', () => {
    expect(notesSince(SECTIONS, '0.12.0', false)).toEqual({
      notes: '## [0.13.0] - 2026-11-20\n\n- a\n\n## [0.12.1] - 2026-11-09\n\n- b',
      sinceBuild: true,
    })
  })

  it('keeps the running release\u2019s own section for a prerelease of it', () => {
    expect(notesSince(SECTIONS, '0.13.0-insider.2', false).notes).toBe('## [0.13.0] - 2026-11-20\n\n- a')
    expect(notesSince(SECTIONS, '0.13.0', false).notes).toBe('')
  })

  it('omits already-installed changes for a promoted stable build with a prerelease stamp', () => {
    expect(notesSince(SECTIONS, '0.12.0-insider.2', true)).toEqual(notesSince(SECTIONS, '0.12.0', false))
    expect(notesSince(SECTIONS, '0.13.0-insider.2', true).notes).toBe('')
  })

  it.each(['0.12.0rc2', '0.12.0.dev20261112'])('uses shared release ordering for %s', version => {
    expect(notesSince(SECTIONS, version, false).notes).toBe(SECTIONS)
  })

  it('folds build numbers and local metadata onto the running release', () => {
    expect(notesSince(SECTIONS, '0.12.0.2+local', false)).toEqual(notesSince(SECTIONS, '0.12.0', false))
  })

  it('ends a selected section at an unversioned level-2 heading', () => {
    expect(notesSince('## [0.13.0]\n\n- new\n\n## Unreleased\n\n- unknown', '0.12.0', false).notes)
      .toBe('## [0.13.0]\n\n- new')
  })

  it('shows the notes whole, not as a range, when the running version names no release', () => {
    expect(notesSince(SECTIONS, 'unknown', false)).toEqual({ notes: SECTIONS, sinceBuild: false })
  })

  it('passes headingless notes through as one release', () => {
    expect(notesSince('- only', '0.1.0', false)).toEqual({ notes: '- only', sinceBuild: false })
  })
})

const ARCHIVE = {
  releases: [
    { version: '0.13.1', date: '2026-11-25', body: '- **Even newer**: z', is_current: false },
    { version: '0.13.0', date: '2026-11-20', body: '- **From the archive**: a', is_current: false },
    { version: '0.12.1', date: '2026-11-09', body: '- **Installed**: b', is_current: true },
  ],
}

describe('archiveNotes', () => {
  it('takes the sections newer than the running release, up to the update', () => {
    expect(archiveNotes(ARCHIVE.releases, '0.13.0')).toBe('## [0.13.0] - 2026-11-20\n\n- **From the archive**: a')
  })

  it('has nothing when the list stops at the running build', () => {
    expect(archiveNotes([ARCHIVE.releases[2]], '0.13.0')).toBe('')
  })
})

describe('AboutPanelUpdateChanges', () => {
  afterEach(() => cleanup())

  it('counts every change and names the first five', () => {
    const notes = Array.from({ length: 7 }, (_, i) => `- **Change ${i + 1}**: detail`).join('\n')
    render(<AboutPanelUpdateChanges notes={notes} version="0.13.0" sinceBuild />)
    const section = screen.getByTestId('update-changes')
    expect(section).toHaveTextContent("What you'd get (7 changes since your build)")
    expect(within(screen.getByTestId('update-changes-list')).getAllByRole('listitem').map(li => li.textContent))
      .toEqual(['Change 1', 'Change 2', 'Change 3', 'Change 4', 'Change 5'])
    expect(screen.getByTestId('update-changes-more')).toHaveTextContent('2 more changes are in the full notes.')
    // The full notes stay one click away.
    expect(screen.getByText('Read the full notes')).toBeInTheDocument()
  })

  it('preserves angle-bracket placeholders in the full notes’ code spans', () => {
    render(<AboutPanelUpdateChanges notes="- **Update**: `kirocrew app update <name> --source <dir>`" version="0.13.0" />)
    const summary = screen.getByText('Read the full notes')
    fireEvent.click(summary)
    const details = summary.closest('details')!
    expect(details).toHaveAttribute('open')
    expect(within(details).getByText('kirocrew app update <name> --source <dir>', { selector: 'code' })).toBeInTheDocument()
  })

  it('sanitizes raw script elements in the full notes after parsing markdown', () => {
    const { container } = render(<AboutPanelUpdateChanges notes={'- **Update**: safe notes\n\n<script>alert("unsafe")</script>'} version="0.13.0" />)
    const summary = screen.getByText('Read the full notes')
    fireEvent.click(summary)
    const details = summary.closest('details')!
    expect(details).toHaveAttribute('open')
    expect(within(details).getByText('[unsupported: script]')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
  })

  it('uses the singular for one change and adds no "more" line', () => {
    render(<AboutPanelUpdateChanges notes="- **Only one**: x" version="0.13.0" sinceBuild />)
    expect(screen.getByTestId('update-changes')).toHaveTextContent("What you'd get (1 change since your build)")
    expect(screen.queryByTestId('update-changes-more')).toBeNull()
  })

  it('names the target version, not a range, when the notes are one release section', () => {
    // The desktop feed carries only the target version's own section, so a
    // "since your build" count would leave out the releases in between.
    render(<AboutPanelUpdateChanges notes={'- **A**: x\n- **B**: y'} version="0.13.0" />)
    expect(screen.getByTestId('update-changes')).toHaveTextContent("What's in 0.13.0 (2 changes)")
    expect(screen.getByTestId('update-changes')).not.toHaveTextContent('since your build')
  })

  it('still shows notes with nothing to count, under a title with no count', () => {
    render(<AboutPanelUpdateChanges notes="A small patch that fixes one crash." version="0.13.0" sinceBuild />)
    const section = screen.getByTestId('update-changes')
    expect(screen.getByText("What you'd get")).toBeInTheDocument()
    expect(screen.queryByTestId('update-changes-list')).toBeNull()
    expect(screen.queryByTestId('update-changes-empty')).toBeNull()
    fireEvent.click(screen.getByText('Read the full notes'))
    expect(within(section).getByText('A small patch that fixes one crash.')).toBeInTheDocument()
  })

  it('names its section by the visible title', () => {
    render(<AboutPanelUpdateChanges notes="- **A**: x" version="0.13.0" sinceBuild />)
    expect(screen.getByRole('region', { name: "What you'd get (1 change since your build)" })).toBeInTheDocument()
    expect(screen.getByTestId('update-changes')).not.toHaveAttribute('aria-label')
  })

  it('says a checked source carried no entries', () => {
    render(<AboutPanelUpdateChanges notes="" version="0.13.0" sinceBuild checked />)
    expect(screen.getByTestId('update-changes-none')).toHaveTextContent('No changelog entries between your build and this update.')
    expect(screen.queryByTestId('update-changes-empty')).toBeNull()
  })

  it('falls back to this install\u2019s release list when the update carries no notes', () => {
    render(<AboutPanelUpdateChanges notes="" version="0.13.0" onLoad={() => {}} />, ARCHIVE)
    expect(screen.getByTestId('update-changes')).toHaveTextContent("What you'd get (1 change since your build)")
    expect(within(screen.getByTestId('update-changes-list')).getByText('From the archive')).toBeInTheDocument()
    expect(screen.queryByTestId('update-changes-load')).toBeNull()
    expect(screen.queryByRole('link', { name: /GitHub/ })).toBeNull()
  })

  it('cuts a desktop fallback above its running version, not the older gateway release', () => {
    const archive = { releases: [
      ...ARCHIVE.releases.slice(0, 2),
      { version: '0.12.2', date: '2026-11-12', body: '- **Already on desktop**: c', is_current: false },
      ARCHIVE.releases[2],
    ] }
    render(<AboutPanelUpdateChanges notes="" version="0.13.1" runningVersion="0.13.0" bytesAreStable={false} />, archive)
    expect(screen.getByTestId('update-changes')).toHaveTextContent("What you'd get (1 change since your build)")
    expect(within(screen.getByTestId('update-changes-list')).getAllByRole('listitem').map(li => li.textContent))
      .toEqual(['Even newer'])
    fireEvent.click(screen.getByText('Read the full notes'))
    const details = screen.getByText('Read the full notes').closest('details')!
    expect(details).toHaveTextContent('[0.13.1]')
    expect(details).not.toHaveTextContent('[0.13.0]')
    expect(details).not.toHaveTextContent('[0.12.2]')
    expect(details).not.toHaveTextContent('[0.12.1]')
  })

  it('lists and counts both releases newer than the desktop when the gateway is ahead', () => {
    const archive = { releases: [
      { version: '0.12.2', date: '2026-11-12', body: '- **Newer release**: a', is_current: false },
      { version: '0.12.1', date: '2026-11-09', body: '- **Gateway release**: b', is_current: true },
      { version: '0.12.0', date: '2026-11-05', body: '- **Desktop release**: c', is_current: false },
    ] }
    render(<AboutPanelUpdateChanges notes="" version="0.13.0-nightly.20261120t010101" runningVersion="0.12.0" />, archive)
    expect(screen.getByTestId('update-changes')).toHaveTextContent("What you'd get (2 changes since your build)")
    expect(within(screen.getByTestId('update-changes-list')).getAllByRole('listitem').map(li => li.textContent))
      .toEqual(['Newer release', 'Gateway release'])
  })

  it.each([false, true])('uses desktop stable-byte membership (%s) when cutting archive notes', bytesAreStable => {
    render(<AboutPanelUpdateChanges notes="" version="0.13.1" runningVersion="0.13.0-insider.2" bytesAreStable={bytesAreStable} />, ARCHIVE)
    expect(within(screen.getByTestId('update-changes-list')).getAllByRole('listitem').map(li => li.textContent))
      .toEqual(bytesAreStable ? ['Even newer'] : ['Even newer', 'From the archive'])
  })

  it('does not call the desktop fallback a range when its running version is unknown', () => {
    render(<AboutPanelUpdateChanges notes="" version="0.13.0" runningVersion="unknown" bytesAreStable={false} />, ARCHIVE)
    expect(screen.getByTestId('update-changes')).toHaveTextContent("What's in 0.13.0 (2 changes)")
    expect(screen.getByTestId('update-changes')).not.toHaveTextContent('since your build')
  })

  it('says the notes are not shown, and links the release, when the update carries none', () => {
    render(<AboutPanelUpdateChanges notes="" version="0.13.0" />)
    expect(screen.queryByTestId('update-changes')).toBeNull()
    expect(screen.getByTestId('update-changes-empty')).toHaveTextContent("This update's notes aren't shown here.")
    expect(screen.getByRole('link', { name: /Read them on GitHub/ }))
      .toHaveAttribute('href', 'https://github.com/kirodotdev/KiroCrew/releases/tag/v0.13.0')
  })

  it('links the release list for a version with no tag of its own', () => {
    render(<AboutPanelUpdateChanges notes="" version="0.13.0-nightly.20261120t010101" />)
    expect(screen.getByRole('link', { name: /Read them on GitHub/ }))
      .toHaveAttribute('href', 'https://github.com/kirodotdev/KiroCrew/releases')
  })

  it('offers to load the notes when a check would bring them, instead of saying there are none', () => {
    const onLoad = vi.fn()
    const { rerender } = render(<AboutPanelUpdateChanges notes="" version="0.13.0" onLoad={onLoad} />)
    expect(screen.queryByTestId('update-changes-empty')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: "Show what's in this update" }))
    expect(onLoad).toHaveBeenCalledTimes(1)
    rerender(<AboutPanelUpdateChanges notes="" version="0.13.0" onLoad={onLoad} loading />)
    expect(screen.getByTestId('update-changes-load')).toBeDisabled()
    // The check answered with notes: the list replaces the offer.
    rerender(<AboutPanelUpdateChanges notes="- **Faster**: x" version="0.13.0" sinceBuild />)
    expect(screen.queryByTestId('update-changes-load')).toBeNull()
    expect(screen.getByTestId('update-changes')).toHaveTextContent("What you'd get (1 change since your build)")
  })
})
