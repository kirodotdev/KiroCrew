// About > Updates: "What you'd get", the changes an available update brings.
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup, within, fireEvent } from '@testing-library/react'
import { AboutPanelUpdateChanges } from '../pages/settings/AboutPanelUpdateChanges'
import { changeEntries } from '../pages/settings/updateChanges'

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
      'Chat and sessions',
      'Windows and Linux',
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
