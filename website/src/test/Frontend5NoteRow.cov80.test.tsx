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
import { render, fireEvent, within } from '@testing-library/react'

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
    validateName: vi.fn(() => null),
    onNewNote: vi.fn(),
    newFolderParent: null,
    onNewFolderStart: vi.fn(),
    onNewFolderEnd: vi.fn(),
    newFolderDraft: '',
    onNewFolderDraft: vi.fn(),
    onNewFolder: vi.fn(),
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

/** The hover action bar's buttons, in DOM order: pin, duplicate, rename, delete on a note; new note, new subfolder on a folder. */
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

describe('md-notebook/NoteRow — folder row actions and the new-folder field', () => {
  function renderFolders(over: Partial<NoteActions> = {}) {
    const a = actions(over)
    const toggle = vi.fn()
    const utils = render(
      <div>{renderTree(tree(), 0, '', {
        activePath: null,
        onOpen: vi.fn(),
        collapsed: new Set(),
        toggle,
        cmp: (x, y) => x.title.localeCompare(y.title),
        actions: a,
      })}</div>,
    )
    return { a, toggle, ...utils }
  }

  it('opens a create menu from one trigger; an item acts and closes it, without toggling the folder', () => {
    const { container, a, toggle } = renderFolders()
    const alpha = container.querySelectorAll('.mdnb-row')[0] as HTMLElement
    // ONE visible action on the bar (the row's click is already the toggle).
    const [trigger, ...others] = actionButtons(alpha)
    expect(others).toEqual([])
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    const bar = alpha.querySelector('.mdnb-folder-actions') as HTMLElement
    expect(bar.style.opacity).toBe('')

    fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    // Held visible while open: the menu hangs below the row, outside its hover.
    expect(bar.style.opacity).toBe('1')
    fireEvent.click(within(alpha).getByRole('button', { name: 'New note in this folder' }))
    expect(a.onNewNote).toHaveBeenCalledWith('alpha')
    expect(within(alpha).queryByRole('button', { name: 'New subfolder' })).toBeNull()
    expect(bar.style.opacity).toBe('')

    fireEvent.click(trigger)
    fireEvent.click(within(alpha).getByRole('button', { name: 'New subfolder' }))
    expect(a.onNewFolderStart).toHaveBeenCalledWith('alpha')
    expect(within(alpha).queryByRole('button', { name: 'New subfolder' })).toBeNull()
    // stopPropagation: no click reached the row's own toggle handler.
    expect(toggle).not.toHaveBeenCalled()
  })

  it('closes the create menu on Escape and on a second press of the trigger, acting on nothing', () => {
    const { container, a } = renderFolders()
    const alpha = container.querySelectorAll('.mdnb-row')[0] as HTMLElement
    const [trigger] = actionButtons(alpha)
    fireEvent.click(trigger)
    fireEvent.keyDown(within(alpha).getByRole('button', { name: 'New subfolder' }), { key: 'Escape' })
    expect(within(alpha).queryByRole('button', { name: 'New subfolder' })).toBeNull()
    fireEvent.click(trigger)
    fireEvent.click(trigger)
    expect(within(alpha).queryByRole('button', { name: 'New subfolder' })).toBeNull()
    expect(a.onNewNote).not.toHaveBeenCalled()
    expect(a.onNewFolderStart).not.toHaveBeenCalled()
  })

  it('closes the create menu on a pointer press outside its host, and on Escape from the trigger', () => {
    const { container, a } = renderFolders()
    const alpha = container.querySelectorAll('.mdnb-row')[0] as HTMLElement
    const [trigger] = actionButtons(alpha)
    fireEvent.click(trigger)
    const item = () => within(alpha).queryByRole('button', { name: 'New subfolder' })
    // A press inside the host (the menu itself, the trigger) leaves it open: the
    // trigger's own click is its toggle, and closing here too would reopen it.
    fireEvent.pointerDown(item()!)
    expect(item()).not.toBeNull()
    fireEvent.pointerDown(trigger)
    expect(item()).not.toBeNull()
    // A press anywhere else — the page around the tree — closes it, acting on nothing.
    fireEvent.pointerDown(document.body)
    expect(item()).toBeNull()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')

    // Right after opening, focus is still on the trigger: Escape there closes.
    fireEvent.click(trigger)
    expect(item()).not.toBeNull()
    fireEvent.keyDown(trigger, { key: 'Escape' })
    expect(item()).toBeNull()
    expect(a.onNewNote).not.toHaveBeenCalled()
    expect(a.onNewFolderStart).not.toHaveBeenCalled()
  })

  it('seeds the field from the mirrored draft and mirrors every keystroke back', () => {
    const { container, a } = renderFolders({ newFolderParent: 'beta', newFolderDraft: 'Proj' })
    const input = container.querySelector('input') as HTMLInputElement
    // The tree unmounts during a search; the draft comes back from the page.
    expect(input.value).toBe('Proj')
    fireEvent.change(input, { target: { value: 'Projects' } })
    expect(a.onNewFolderDraft).toHaveBeenCalledWith('Projects')
    expect(input.value).toBe('Projects')
  })

  it('previews the cleaned name in the hint when the cleaner would change it', () => {
    const { container } = renderFolders({ newFolderParent: 'beta' })
    const input = container.querySelector('input') as HTMLInputElement
    const hint = () =>
      container.querySelector(`#${CSS.escape(input.getAttribute('aria-describedby')!)}`)?.textContent
    expect(hint()).toBe('Starts with an empty note')
    // Path-style nesting and forbidden characters: the name that WILL be created.
    fireEvent.change(input, { target: { value: '2026/Q1' } })
    expect(hint()).toBe('Will be created as “2026Q1”')
    fireEvent.change(input, { target: { value: 'Q&A: 2026' } })
    expect(hint()).toBe('Will be created as “Q&A 2026”')
    // A name the cleaner leaves alone goes back to the plain disclosure.
    fireEvent.change(input, { target: { value: 'Plans' } })
    expect(hint()).toBe('Starts with an empty note')
    // Nothing usable left: no preview of an empty name, the refusal speaks instead.
    fireEvent.change(input, { target: { value: '?*|' } })
    expect(hint()).toBe('Starts with an empty note')
  })

  it('keeps a refused folder name in the field, marked invalid with the reason, until it is edited', () => {
    const { container, a } = renderFolders({
      newFolderParent: 'beta',
      validateName: vi.fn((raw: string) => (raw === 'CON' ? 'Windows reserves this name' : null)),
    })
    const input = container.querySelector('input') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'CON' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // Not committed, not closed: the draft is still there to fix.
    expect(a.onNewFolder).not.toHaveBeenCalled()
    expect(a.onNewFolderEnd).not.toHaveBeenCalled()
    expect(input.value).toBe('CON')
    expect(input.getAttribute('aria-invalid')).toBe('true')
    const hint = container.querySelector(`#${CSS.escape(input.getAttribute('aria-describedby')!)}`)
    expect(hint?.textContent).toBe('Windows reserves this name')
    // Blur while refused keeps it too (the reason would otherwise vanish with the draft).
    fireEvent.blur(input)
    expect(a.onNewFolderEnd).not.toHaveBeenCalled()
    // Editing clears the hint; a valid name then commits once.
    fireEvent.change(input, { target: { value: 'Console' } })
    expect(input.getAttribute('aria-invalid')).toBeNull()
    expect(container.textContent).not.toContain('reserves')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(a.onNewFolder).toHaveBeenCalledWith('beta', 'Console')
    expect(a.onNewFolderEnd).toHaveBeenCalledTimes(1)
  })

  it('renders the name field under its parent only, first among that level, focused', () => {
    const { container } = renderFolders({ newFolderParent: 'beta' })
    const rows = Array.from(container.querySelectorAll('.mdnb-row'))
    const labels = rows.map((r) => r.getAttribute('aria-label'))
    // beta's children: the field (no aria-label on the row, the input has it),
    // then beta's notes. Nothing under alpha, nothing at the root.
    expect(labels).toEqual(['alpha', 'a1.md', 'beta', null, 'b1.md', 'b2.md', 'root1.md', 'root2.md'])
    const input = rows[3].querySelector('input') as HTMLInputElement
    expect(document.activeElement).toBe(input)
  })

  it('never renders the root field itself — that is the page\'s placement', () => {
    const { container } = renderFolders({ newFolderParent: '' })
    expect(container.querySelector('.mdnb-row input')).toBeNull()
  })

  it('commits a typed name on Enter, once, and ends the field first', () => {
    const calls: string[] = []
    const { container } = renderFolders({
      newFolderParent: 'beta',
      onNewFolderEnd: vi.fn(() => calls.push('end')),
      onNewFolder: vi.fn(() => calls.push('create')),
    })
    const input = container.querySelector('.mdnb-row input') as HTMLInputElement
    fireEvent.change(input, { target: { value: '  Projects ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // The blur the unmount produces must not commit a second time.
    fireEvent.blur(input)
    expect(calls).toEqual(['end', 'create'])
  })

  it('passes the trimmed name and its parent to onNewFolder', () => {
    const { container, a } = renderFolders({ newFolderParent: 'beta' })
    const input = container.querySelector('.mdnb-row input') as HTMLInputElement
    fireEvent.change(input, { target: { value: '  Projects ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(a.onNewFolder).toHaveBeenCalledTimes(1)
    expect(a.onNewFolder).toHaveBeenCalledWith('beta', 'Projects')
  })

  it('commits on blur, and treats a blank name as a cancel', () => {
    const { container, a } = renderFolders({ newFolderParent: 'beta' })
    const input = container.querySelector('.mdnb-row input') as HTMLInputElement
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.blur(input)
    expect(a.onNewFolderEnd).toHaveBeenCalledTimes(1)
    expect(a.onNewFolder).not.toHaveBeenCalled()
  })

  it('abandons on Escape without creating anything', () => {
    const { container, a } = renderFolders({ newFolderParent: 'beta' })
    const input = container.querySelector('.mdnb-row input') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Projects' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(a.onNewFolderEnd).toHaveBeenCalledTimes(1)
    expect(a.onNewFolder).not.toHaveBeenCalled()
  })
})
