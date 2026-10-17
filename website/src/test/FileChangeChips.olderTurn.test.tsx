import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import FileChangeChips, { type FileChangeEntry } from '../components/FileChangeChips'
import enManual from '../i18n/locales/en.manual.json'

/* A chat keeps file-change copies only for its newest turns that changed files
 * (`session.file_changes_keep_turns`). An older turn's entries come
 * back from the transcript the way `transcript_merge.py` writes them: the path
 * and the other fields stay, before/after are empty, `content_omitted` holds and
 * `omitted_reason` is `older_turn`. Its card must still list every file, say why
 * there is no diff, and open the file; a recent turn's card is unchanged. */

const OLDER_TURN_SENTENCE = enManual.components.fileChangeChips.diff_unavailable_older_turn
const TURN_BUDGET_SENTENCE = "This turn's changes were too large to keep every file's diff (over 400,000 characters total)."

const olderTurn = (path: string): FileChangeEntry => ({
  path,
  before: '',
  after: '',
  truncated: true,
  content_omitted: true,
  omitted_reason: 'older_turn',
})
const budgetDemoted = (path: string): FileChangeEntry => ({
  path, before: '', after: '', truncated: true, content_omitted: true, turn_budget_chars: 400_000,
})

describe('FileChangeChips: a turn older than the chat keeps copies for', () => {
  it('lists every file as file-only and says once why the diff is gone', () => {
    const { container } = render(<FileChangeChips fileChanges={[olderTurn('/repo/a.py'), olderTurn('/repo/b.py')]} />)

    expect(screen.getByText('2 files changed')).toBeInTheDocument()
    expect(screen.getByTestId('fcc-row-/repo/a.py')).toBeInTheDocument()
    expect(screen.getByTestId('fcc-row-/repo/b.py')).toBeInTheDocument()
    expect(screen.getAllByText('File only')).toHaveLength(2)
    expect(screen.getAllByText(OLDER_TURN_SENTENCE)).toHaveLength(1)
    expect(container.querySelector('[data-fcc-turn-notice]')).toHaveTextContent(OLDER_TURN_SENTENCE)
    // Not the turn-budget reason: this turn was not too large.
    expect(screen.queryByText(/too large/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Show or hide the diff for /repo/a.py')).not.toBeInTheDocument()
  })

  it('restates the reason on each row tag', () => {
    render(<FileChangeChips fileChanges={[olderTurn('/repo/a.py')]} />)
    expect(screen.getByText('File only')).toHaveAttribute('title', OLDER_TURN_SENTENCE)
  })

  it('still opens the file from its row', () => {
    const onFileOpen = vi.fn()
    render(<FileChangeChips fileChanges={[olderTurn('/repo/a.py')]} onFileOpen={onFileOpen} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open /repo/a.py in side panel' }))
    expect(onFileOpen).toHaveBeenCalledWith('/repo/a.py')
  })

  it('gives the older-turn reason once for a card that also had a budget-demoted row', () => {
    // That row lost its content to the turn budget long ago; the older-turn
    // sentence is true of it too, so the card states one reason, not two.
    render(<FileChangeChips fileChanges={[budgetDemoted('/repo/b.py'), olderTurn('/repo/a.py')]} />)
    expect(screen.getAllByText(OLDER_TURN_SENTENCE)).toHaveLength(1)
    expect(screen.queryByText(TURN_BUDGET_SENTENCE)).not.toBeInTheDocument()
  })

  it('keeps the minimal pill row: filenames, the marker and the reason once, each pill opening its file', () => {
    const onFileOpen = vi.fn()
    const onOpenDiff = vi.fn()
    const { container } = render(
      <FileChangeChips
        fileChanges={[olderTurn('/repo/a.py'), olderTurn('/repo/b.py')]}
        style="minimal"
        onFileOpen={onFileOpen}
        onOpenDiff={onOpenDiff}
      />,
    )
    expect(screen.getAllByText('File only')).toHaveLength(2)
    const notices = container.querySelectorAll('[data-fcc-turn-notice]')
    expect(notices).toHaveLength(1)
    expect(notices[0]).toHaveTextContent(`${OLDER_TURN_SENTENCE} Click a file name to open it.`)
    fireEvent.click(screen.getByRole('button', { name: '/repo/b.py' }))
    expect(onFileOpen).toHaveBeenCalledWith('/repo/b.py')
    expect(onOpenDiff).not.toHaveBeenCalled()
  })
})

describe('FileChangeChips: a recent turn the bound left whole', () => {
  it('renders its diff stats and no notice', () => {
    const { container } = render(<FileChangeChips fileChanges={[{ path: '/repo/a.py', before: 'a', after: 'a\nb' }]} />)
    expect(screen.getByText('1 addition')).toBeInTheDocument()
    expect(container.querySelector('[data-fcc-turn-notice]')).toBeNull()
    expect(screen.queryByText(OLDER_TURN_SENTENCE)).not.toBeInTheDocument()
  })

  it('keeps the turn-budget reason for a recent turn the budget demoted', () => {
    render(<FileChangeChips fileChanges={[{ path: '/repo/a.py', before: 'a', after: 'b' }, budgetDemoted('/repo/b.py')]} />)
    expect(screen.getByText(TURN_BUDGET_SENTENCE)).toBeInTheDocument()
    expect(screen.queryByText(OLDER_TURN_SENTENCE)).not.toBeInTheDocument()
  })
})
