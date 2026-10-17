import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import FileChangeChips, { type FileChangeEntry } from '../components/FileChangeChips'
import enManual from '../i18n/locales/en.manual.json'

/* A write tool reported `failed` and nothing recorded the file before it, so
 * the gateway keeps the path with no diff and `omitted_reason:
 * 'unverified_write'` (`file_changes.py::_failed_write_entries`). The card must
 * say exactly that: not the turn-budget sentence (the turn was not too large)
 * and not "no changes" (the file may have changed). */

const UNVERIFIED_SENTENCE = enManual.components.fileChangeChips.diff_unavailable_unverified_write
const TURN_BUDGET_SENTENCE = "This turn's changes were too large to keep every file's diff (over 400,000 characters total)."

const unverified = (path: string): FileChangeEntry => ({
  path, before: '', after: '', truncated: true, content_omitted: true, omitted_reason: 'unverified_write',
})
const budgetDemoted = (path: string): FileChangeEntry => ({
  path, before: '', after: '', truncated: true, content_omitted: true, turn_budget_chars: 400_000,
})

describe('FileChangeChips: a failed write whose earlier file is unknown', () => {
  it('says the diff is unavailable because the write failed, and nothing else', () => {
    const { container } = render(<FileChangeChips fileChanges={[unverified('/repo/a.py')]} />)
    expect(container.querySelector('[data-fcc-turn-notice]')).toHaveTextContent(UNVERIFIED_SENTENCE)
    expect(screen.getByText('File only')).toHaveAttribute('title', UNVERIFIED_SENTENCE)
    expect(screen.queryByText(/too large/)).not.toBeInTheDocument()
    expect(screen.queryByText('No changes')).not.toBeInTheDocument()
  })

  it('renders the failure through the shared error surface, with the agent hand-off', () => {
    render(<FileChangeChips fileChanges={[unverified('/repo/a.py')]} />)
    const alert = screen.getByTestId('fcc-unverified-write-notice')
    expect(alert).toHaveAttribute('role', 'alert')
    expect(alert).toHaveTextContent(UNVERIFIED_SENTENCE)
    expect(within(alert).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('states the failed write and the turn budget as two reasons, each once', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[unverified('/repo/a.py'), budgetDemoted('/repo/b.py')]} />,
    )
    const notices = [...container.querySelectorAll('[data-fcc-turn-notice]')]
    expect(notices).toHaveLength(2)
    expect(notices[0]).toHaveTextContent(UNVERIFIED_SENTENCE)
    expect(notices[1]).toHaveTextContent(TURN_BUDGET_SENTENCE)
    // The budget sentence is not an error and keeps its plain notice.
    expect(notices[1].querySelector('[role="alert"]')).toBeNull()
  })

  it('keeps the failed-write error when the turn ages, in both styles', () => {
    // transcript_merge strips only entries that still hold a copy, so an aged
    // mixed turn carries both reasons; the error and its hand-off stay.
    const aged = { path: '/repo/b.py', before: '', after: '', truncated: true, content_omitted: true, omitted_reason: 'older_turn' }
    for (const style of ['expanded', 'minimal'] as const) {
      const { container, unmount } = render(
        <FileChangeChips fileChanges={[unverified('/repo/a.py'), aged]} style={style} />,
      )
      const notices = [...container.querySelectorAll('[data-fcc-turn-notice]')]
      expect(notices).toHaveLength(2)
      expect(notices[0]).toHaveTextContent(UNVERIFIED_SENTENCE)
      expect(within(notices[0] as HTMLElement).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
      expect(notices[1]).toHaveTextContent(enManual.components.fileChangeChips.diff_unavailable_older_turn)
      unmount()
    }
  })

  it('says the reason once on the minimal pill row', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[unverified('/repo/a.py'), unverified('/repo/b.py')]} style="minimal" />,
    )
    const notices = container.querySelectorAll('[data-fcc-turn-notice]')
    expect(notices).toHaveLength(1)
    expect(notices[0]).toHaveTextContent(UNVERIFIED_SENTENCE)
  })

  it('marks the failed row itself, and only that row, in both styles', () => {
    const changes = [unverified('/repo/a.py'), budgetDemoted('/repo/b.py')]
    const { container, unmount } = render(<FileChangeChips fileChanges={changes} />)
    let marked = container.querySelectorAll('[data-fcc-unverified-write]')
    expect(marked).toHaveLength(1)
    expect(screen.getByTestId('fcc-row-/repo/a.py')).toContainElement(marked[0] as HTMLElement)
    expect(marked[0]).toHaveAttribute('title', UNVERIFIED_SENTENCE)
    unmount()
    const minimal = render(<FileChangeChips fileChanges={changes} style="minimal" />)
    marked = minimal.container.querySelectorAll('[data-fcc-unverified-write]')
    expect(marked).toHaveLength(1)
    expect(screen.getByRole('button', { name: '/repo/a.py' })).toContainElement(marked[0] as HTMLElement)
  })

  it('puts the open hint on its own line, apart from the hand-off link', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[unverified('/repo/a.py')]} style="minimal" onFileOpen={() => {}} />,
    )
    const notice = container.querySelector('[data-fcc-turn-notice]') as HTMLElement
    const alert = within(notice).getByRole('alert')
    expect(alert).not.toHaveTextContent('Click a file name to open it.')
    expect(notice).toHaveTextContent('Click a file name to open it.')
  })

  it('never implies the file is unchanged', () => {
    expect(UNVERIFIED_SENTENCE).not.toMatch(/unchanged|no change/i)
    expect(UNVERIFIED_SENTENCE).toMatch(/failed/)
  })
})
