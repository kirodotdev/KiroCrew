/**
 * `NoteRow.tsx` end to end: the row's own affordances, the folder row, and the
 * three pure ordering helpers the panel's keyboard navigation depends on.
 *
 * The row's interesting behaviour is all conditional — inline rename (seed,
 * commit, abandon), the drag-to-file handlers on both a note and a folder, and
 * the badge precedence that turns the sync slot into a delete indicator. The
 * helpers are asserted directly because `flattenVisibleNotes` must agree with
 * what `renderTree` actually renders; a drift there silently breaks
 * "next note down".
 *
 * Controls are addressed by position inside `.mdnb-row-actions` rather than by
 * their labels, so the assertions never pin user-visible copy.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'

import {
  NoteRow,
  flattenVisibleNotes,
  orderNotes,
  renderTree,
} from '../apps/md-notebook/NoteRow'
import type { Note, NoteActions, TreeNode } from '../apps/md-notebook/types'

function note(path: string, over: Partial<Note> = {}): Note {
  return { path, title: path.split('/').pop() ?? path, modifiedAt: 1_700_000_000_000, syncStatus: 'synced', ...over }
}

function actions(over: Partial<NoteActions> = {}): NoteActions {
  return {
    isPinned: () => false,
    onTogglePin: vi.fn(),
    onDuplicate: vi.fn(),
    onMove: vi.fn(),
    renamingPath: null,
    deletingPath: null,
    onRenameStart: vi.fn(),
    onRenameEnd: vi.fn(),
    onRename: vi.fn(),
    ...over,
  }
}

/** A dataTransfer stand-in that records what the row wrote and reads back. */
function transfer(payload = '') {
  const store: Record<string, string> = { 'text/plain': payload }
  return {
    setData: vi.fn((k: string, v: string) => { store[k] = v }),
    getData: (k: string) => store[k] ?? '',
    effectAllowed: '',
    dropEffect: '',
  }
}

/** The hover action bar's buttons, in DOM order: pin, duplicate, rename, delete. */
function actionButtons(root: HTMLElement) {
  return Array.from(root.querySelectorAll('.mdnb-row-actions button')) as HTMLButtonElement[]
}

const tree = (): TreeNode => ({
  folders: new Map<string, TreeNode>([
    ['beta', { folders: new Map(), notes: [note('beta/b1.md'), note('beta/b2.md')] }],
    ['alpha', { folders: new Map(), notes: [note('alpha/a1.md')] }],
  ]),
  notes: [note('root2.md'), note('root1.md')],
})

describe('md-notebook/NoteRow — row affordances', () => {
  it('opens the note on click and runs pin, duplicate and rename-start', () => {
    const onOpen = vi.fn()
    const a = actions({ onDelete: vi.fn() })
    const { container } = render(
      <NoteRow note={note('One.md')} active onOpen={onOpen} actions={a} />,
    )
    fireEvent.click(container.querySelector('.mdnb-row') as HTMLElement)
    expect(onOpen).toHaveBeenCalledWith('One.md')

    const [pin, dup, ren, del] = actionButtons(container)
    fireEvent.click(pin)
    fireEvent.click(dup)
    fireEvent.click(ren)
    fireEvent.click(del)
    expect(a.onTogglePin).toHaveBeenCalledWith('One.md')
    expect(a.onDuplicate).toHaveBeenCalledWith('One.md')
    expect(a.onRenameStart).toHaveBeenCalledWith('One.md')
    expect(a.onDelete).toHaveBeenCalledWith('One.md', 'One.md')
    // A row-action click must not also open the note.
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('shows the pinned marker and no actions at all without an actions bundle', () => {
    const { container: pinned } = render(
      <NoteRow note={note('One.md')} active={false} onOpen={vi.fn()} actions={actions({ isPinned: () => true })} />,
    )
    expect(actionButtons(pinned).length).toBe(3) // no onDelete supplied
    const { container: bare } = render(
      <NoteRow note={note('One.md')} active={false} onOpen={vi.fn()} />,
    )
    expect(bare.querySelector('.mdnb-row-actions')).toBeNull()
  })

  it('surfaces the parent folder only in flat-list view', () => {
    const { container: flat } = render(
      <NoteRow note={note('Deep/Nested/One.md')} active={false} onOpen={vi.fn()} showFolder actions={actions()} />,
    )
    expect(flat.querySelector('[title="Deep/Nested/One.md"]')?.textContent).toBe('Nested')
    const { container: tree_ } = render(
      <NoteRow note={note('Deep/Nested/One.md')} active={false} onOpen={vi.fn()} actions={actions()} />,
    )
    expect(tree_.querySelector('[title="Deep/Nested/One.md"]')).toBeNull()
  })

  it('gives the delete indicator precedence over the pending sync badge', () => {
    const { container } = render(
      <NoteRow
        note={note('One.md', { syncStatus: 'pending' })}
        active={false}
        onOpen={vi.fn()}
        actions={actions({ deletingPath: 'One.md' })}
      />,
    )
    // Dimmed, undraggable, unclickable, and the action bar is gone.
    const row = container.querySelector('.mdnb-row') as HTMLElement
    expect(row.style.opacity).toBe('0.5')
    expect(row.getAttribute('draggable')).toBe('false')
    expect(container.querySelector('.mdnb-row-actions')).toBeNull()
  })

  it('hides the pending badge on a vault with no remote', () => {
    const withBadge = render(
      <NoteRow note={note('One.md', { syncStatus: 'pending' })} active={false} onOpen={vi.fn()} actions={actions()} />,
    )
    const withoutBadge = render(
      <NoteRow
        note={note('Two.md', { syncStatus: 'pending' })}
        active={false}
        onOpen={vi.fn()}
        showSyncBadge={false}
        actions={actions()}
      />,
    )
    const spans = (r: ReturnType<typeof render>) =>
      Array.from(r.container.querySelectorAll('span')).filter((s) => s.style.border === '1px solid')
    expect(spans(withBadge).length).toBe(1)
    expect(spans(withoutBadge).length).toBe(0)
  })
})

describe('md-notebook/NoteRow — inline rename', () => {
  function renderRenaming(over: Partial<NoteActions> = {}) {
    const a = actions({ renamingPath: 'One.md', ...over })
    const utils = render(
      <NoteRow note={note('One.md', { title: 'Original' })} active={false} onOpen={vi.fn()} actions={a} />,
    )
    const input = utils.container.querySelector('input') as HTMLInputElement
    return { a, input, ...utils }
  }

  it('seeds the field from the title and focuses it', () => {
    const { input } = renderRenaming()
    expect(input.value).toBe('Original')
    expect(document.activeElement).toBe(input)
  })

  it('commits a changed name on Enter', () => {
    const { a, input } = renderRenaming()
    fireEvent.change(input, { target: { value: '  Renamed  ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(a.onRenameEnd).toHaveBeenCalled()
    expect(a.onRename).toHaveBeenCalledWith('One.md', 'Renamed')
  })

  it('ends rename mode without a write when the name is unchanged or empty', () => {
    const { a, input } = renderRenaming()
    fireEvent.blur(input)
    expect(a.onRenameEnd).toHaveBeenCalledTimes(1)
    expect(a.onRename).not.toHaveBeenCalled()

    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.blur(input)
    expect(a.onRename).not.toHaveBeenCalled()
  })

  it('abandons the edit on Escape and swallows other keys', () => {
    const { a, input } = renderRenaming()
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(a.onRenameEnd).toHaveBeenCalledTimes(1)
    expect(a.onRename).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'x' })
    expect(a.onRenameEnd).toHaveBeenCalledTimes(1)
  })

  it('does not open the note when the field is clicked, and suppresses dragging', () => {
    const onOpen = vi.fn()
    const a = actions({ renamingPath: 'One.md' })
    const { container } = render(
      <NoteRow note={note('One.md')} active={false} onOpen={onOpen} actions={a} />,
    )
    const input = container.querySelector('input') as HTMLInputElement
    fireEvent.click(input)
    const row = container.querySelector('.mdnb-row') as HTMLElement
    fireEvent.click(row)
    expect(onOpen).not.toHaveBeenCalled()
    expect(row.getAttribute('draggable')).toBe('false')
    // The action bar is hidden while the row is being renamed.
    expect(container.querySelector('.mdnb-row-actions')).toBeNull()
  })
})

describe('md-notebook/NoteRow — drag to file', () => {
  it('writes its own path on drag start and files a dropped note into its folder', () => {
    const a = actions()
    const { container } = render(
      <NoteRow note={note('Deep/One.md')} active={false} onOpen={vi.fn()} actions={a} />,
    )
    const row = container.querySelector('.mdnb-row') as HTMLElement

    const start = transfer()
    fireEvent.dragStart(row, { dataTransfer: start })
    expect(start.setData).toHaveBeenCalledWith('text/plain', 'Deep/One.md')

    fireEvent.dragOver(row, { dataTransfer: transfer() })

    fireEvent.drop(row, { dataTransfer: transfer('Other.md') })
    expect(a.onMove).toHaveBeenCalledWith('Other.md', 'Deep')
  })

  it('ignores a drop of the row onto itself, and files to the root from a top-level row', () => {
    const a = actions()
    const { container } = render(
      <NoteRow note={note('One.md')} active={false} onOpen={vi.fn()} actions={a} />,
    )
    const row = container.querySelector('.mdnb-row') as HTMLElement
    fireEvent.drop(row, { dataTransfer: transfer('One.md') })
    expect(a.onMove).not.toHaveBeenCalled()
    fireEvent.drop(row, { dataTransfer: transfer('Other.md') })
    expect(a.onMove).toHaveBeenCalledWith('Other.md', '')
  })

  it('never starts a drag while renaming', () => {
    const { container } = render(
      <NoteRow
        note={note('One.md')}
        active={false}
        onOpen={vi.fn()}
        actions={actions({ renamingPath: 'One.md' })}
      />,
    )
    const row = container.querySelector('.mdnb-row') as HTMLElement
    const dt = transfer()
    fireEvent.dragStart(row, { dataTransfer: dt })
    expect(dt.setData).not.toHaveBeenCalled()
  })

  it('drops through silently with no actions bundle', () => {
    const { container } = render(<NoteRow note={note('One.md')} active={false} onOpen={vi.fn()} />)
    const row = container.querySelector('.mdnb-row') as HTMLElement
    // No handler to call and nothing to move — must not throw.
    expect(() => {
      fireEvent.dragOver(row, { dataTransfer: transfer() })
      fireEvent.drop(row, { dataTransfer: transfer('Other.md') })
    }).not.toThrow()
  })
})

describe('md-notebook/NoteRow — tree', () => {
  function renderFolders(collapsed: Set<string>, over: Partial<NoteActions> = {}) {
    const toggle = vi.fn()
    const a = actions(over)
    const utils = render(
      <div>{renderTree(tree(), 0, '', {
        activePath: 'root1.md',
        onOpen: vi.fn(),
        collapsed,
        toggle,
        cmp: (x, y) => x.title.localeCompare(y.title),
        actions: a,
      })}</div>,
    )
    return { toggle, a, ...utils }
  }

  it('renders folders alphabetically with their recursive note counts', () => {
    const { container } = renderFolders(new Set())
    const rows = Array.from(container.querySelectorAll('.mdnb-row'))
    const labels = rows.map((r) => r.getAttribute('aria-label'))
    // alpha (1 note) before beta (2 notes), then this level's own notes sorted.
    expect(labels).toEqual(['alpha', 'a1.md', 'beta', 'b1.md', 'b2.md', 'root1.md', 'root2.md'])
    const counts = rows
      .map((r) => r.querySelector('span[style*="auto"]')?.textContent)
      .filter(Boolean)
    expect(counts).toEqual(['1', '2'])
  })

  it('renders nothing under a collapsed folder and toggles on click', () => {
    const { container, toggle } = renderFolders(new Set(['beta']))
    const labels = Array.from(container.querySelectorAll('.mdnb-row')).map((r) =>
      r.getAttribute('aria-label'),
    )
    expect(labels).toEqual(['alpha', 'a1.md', 'beta', 'root1.md', 'root2.md'])
    fireEvent.click(container.querySelectorAll('.mdnb-row')[2] as HTMLElement)
    expect(toggle).toHaveBeenCalledWith('beta')
  })

  it('files a dropped note into the folder row, and clears the highlight on leave', () => {
    const { container, a } = renderFolders(new Set())
    const folder = container.querySelectorAll('.mdnb-row')[0] as HTMLElement
    fireEvent.dragOver(folder, { dataTransfer: transfer() })
    expect(folder.style.outline).not.toBe('')
    fireEvent.dragLeave(folder)
    expect(folder.style.outline).toBe('')
    fireEvent.dragOver(folder, { dataTransfer: transfer() })
    fireEvent.drop(folder, { dataTransfer: transfer('root1.md') })
    expect(a.onMove).toHaveBeenCalledWith('root1.md', 'alpha')
    expect(folder.style.outline).toBe('')
  })

  it('ignores an empty drop payload on a folder row', () => {
    const { container, a } = renderFolders(new Set())
    fireEvent.drop(container.querySelectorAll('.mdnb-row')[0] as HTMLElement, {
      dataTransfer: transfer(''),
    })
    expect(a.onMove).not.toHaveBeenCalled()
  })
})

describe('md-notebook/NoteRow — ordering helpers', () => {
  const byTitle = (a: Note, b: Note) => a.title.localeCompare(b.title)

  it('hoists pinned notes inside their own group, keeping the sort within each', () => {
    const notes = [note('c.md'), note('a.md'), note('b.md')]
    const ordered = orderNotes(notes, byTitle, (p) => p === 'c.md')
    expect(ordered.map((n) => n.path)).toEqual(['c.md', 'a.md', 'b.md'])
    // Pure: the input array is untouched.
    expect(notes.map((n) => n.path)).toEqual(['c.md', 'a.md', 'b.md'])
  })

  it('flattens exactly what the folders view renders, in the same order', () => {
    const paths = flattenVisibleNotes(tree(), byTitle, () => false, new Set())
    expect(paths).toEqual(['alpha/a1.md', 'beta/b1.md', 'beta/b2.md', 'root1.md', 'root2.md'])
  })

  it('contributes nothing from a collapsed folder', () => {
    const paths = flattenVisibleNotes(tree(), byTitle, () => false, new Set(['beta']))
    expect(paths).toEqual(['alpha/a1.md', 'root1.md', 'root2.md'])
  })

  it('applies the pin order inside a folder', () => {
    const paths = flattenVisibleNotes(tree(), byTitle, (p) => p === 'beta/b2.md', new Set())
    expect(paths).toEqual(['alpha/a1.md', 'beta/b2.md', 'beta/b1.md', 'root1.md', 'root2.md'])
  })
})
