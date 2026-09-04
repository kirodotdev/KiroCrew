/**
 * The delete affordance must not exist unless the backend confirms it moves a
 * note to `.trash`.
 *
 * A new UI bundle can run against an older Notes backend whose `DELETE` unlinks
 * the file outright. The confirmation this button opens tells the user the note
 * is restorable from `.trash`, so offering it there would break that promise and
 * an uncommitted note would be gone for good. `MdNotebookPage` passes `onDelete`
 * only when health positively reports the `trash` capability, and the row omits
 * the button when it is absent — this pins the row half of that contract, which
 * is the half a future refactor could quietly make required again.
 */
import { describe, it, expect } from 'vitest'
import { render, within } from '@testing-library/react'

import { NoteRow } from '../apps/md-notebook/NoteRow'
import type { Note, NoteActions } from '../apps/md-notebook/types'

const note: Note = {
  path: 'One.md',
  title: 'One',
  modifiedAt: Date.now(),
  syncStatus: 'synced',
}

function actions(overrides: Partial<NoteActions> = {}): NoteActions {
  return {
    isPinned: () => false,
    onTogglePin: () => {},
    onDuplicate: () => {},
    onMove: () => {},
    renamingPath: null,
    deletingPath: null,
    onRenameStart: () => {},
    onRenameEnd: () => {},
    onRename: () => {},
    ...overrides,
  }
}

describe('md-notebook/NoteRow delete affordance', () => {
  it('offers delete when the backend can trash', () => {
    const { container } = render(
      <NoteRow note={note} active={false} onOpen={() => {}} actions={actions({ onDelete: () => {} })} />,
    )
    expect(within(container).queryByRole('button', { name: /delete/i })).not.toBeNull()
  })

  it('omits delete entirely when it cannot', () => {
    // Not merely disabled: a disabled control still advertises an action the
    // backend cannot honour. The row must render no delete button at all.
    const { container } = render(
      <NoteRow note={note} active={false} onOpen={() => {}} actions={actions()} />,
    )
    expect(within(container).queryByRole('button', { name: /delete/i })).toBeNull()
    // The other row actions are unaffected — only delete depends on `trash`.
    expect(within(container).queryByRole('button', { name: /rename/i })).not.toBeNull()
  })
})
