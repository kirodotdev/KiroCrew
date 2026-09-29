import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { TREE_ENTRY_DRAG_TYPE, TREE_ENTRY_REFUSED_TYPE, encodeTreeEntry, type TreeEntryDragPayload } from '../lib/treeEntryDrag'

/**
 * Drop routing on the real composer: a file-tree row goes to the host's "Add
 * to chat" handler; OS file drags and text drags still reach the host's own
 * drop handler untouched, and the composer text is never edited here.
 */

function dataTransfer(data: Record<string, string>, opts: { files?: boolean } = {}) {
  return {
    types: [...Object.keys(data), ...(opts.files ? ['Files'] : [])],
    getData: (type: string) => data[type] ?? '',
    setData: vi.fn(),
    items: opts.files ? [{ kind: 'file' }] : [],
    files: [],
    effectAllowed: 'copyMove',
    dropEffect: 'none',
  }
}

const treeDrag = (entry: TreeEntryDragPayload) => dataTransfer({
  'text/plain': entry.path,
  [TREE_ENTRY_DRAG_TYPE]: encodeTreeEntry(entry),
})

function setup(value = 'explain') {
  const onChange = vi.fn()
  const onTreeEntryDrop = vi.fn()
  const onDrop = vi.fn()
  const onDragOver = vi.fn()
  renderWithProviders(
    <ChatInput
      value={value}
      onChange={onChange}
      onSend={vi.fn()}
      project="/repo"
      onTreeEntryDrop={onTreeEntryDrop}
      onDrop={onDrop}
      onDragOver={onDragOver}
    />,
  )
  const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
  const wrapper = screen.getByTestId('input-wrapper')
  return { onChange, onTreeEntryDrop, onDrop, onDragOver, textarea, wrapper }
}

describe('ChatInput file-tree drop', () => {
  it('hands a file row dropped on the text area to the host', () => {
    const t = setup()
    fireEvent.drop(t.textarea, { dataTransfer: treeDrag({ path: '/repo/src/a.ts', kind: 'file' }) })
    expect(t.onTreeEntryDrop).toHaveBeenCalledWith('/repo/src/a.ts', 'file', null)
    expect(t.onDrop).not.toHaveBeenCalled()
    expect(t.onChange).not.toHaveBeenCalled()
  })

  it('hands a folder row dropped on the composer frame to the host', () => {
    const t = setup('')
    fireEvent.drop(t.wrapper, { dataTransfer: treeDrag({ path: '/repo/src/pages', kind: 'dir' }) })
    expect(t.onTreeEntryDrop).toHaveBeenCalledWith('/repo/src/pages', 'dir', null)
    expect(t.onDrop).not.toHaveBeenCalled()
  })

  it('leaves OS file drops to the host upload handler', () => {
    const t = setup()
    fireEvent.drop(t.textarea, { dataTransfer: dataTransfer({}, { files: true }) })
    expect(t.onDrop).toHaveBeenCalledTimes(1)
    expect(t.onTreeEntryDrop).not.toHaveBeenCalled()
  })

  it('leaves text drops to the existing handler', () => {
    const t = setup()
    fireEvent.drop(t.textarea, { dataTransfer: dataTransfer({ 'text/plain': 'some words' }) })
    expect(t.onDrop).toHaveBeenCalledTimes(1)
    expect(t.onTreeEntryDrop).not.toHaveBeenCalled()
  })

  it('refuses a payload naming a path outside the project', () => {
    const t = setup('')
    fireEvent.drop(t.textarea, { dataTransfer: treeDrag({ path: '/home/u/.ssh/id_rsa', kind: 'file' }) })
    expect(t.onTreeEntryDrop).not.toHaveBeenCalled()
  })

  it('shows the drop indicator only while a tree row is over the composer', () => {
    const t = setup()
    expect(screen.queryByTestId('composer-tree-drop-indicator')).toBeNull()
    fireEvent.dragOver(t.wrapper, { dataTransfer: dataTransfer({}, { files: true }) })
    expect(screen.queryByTestId('composer-tree-drop-indicator')).toBeNull()
    expect(t.onDragOver).toHaveBeenCalledTimes(1)

    const drag = treeDrag({ path: '/repo/a.ts', kind: 'file' })
    fireEvent.dragOver(t.wrapper, { dataTransfer: drag })
    expect(screen.getByTestId('composer-tree-drop-indicator')).toBeTruthy()
    expect(t.wrapper.getAttribute('data-tree-drop-active')).toBe('true')
    expect(t.onDragOver).toHaveBeenCalledTimes(1)

    fireEvent.dragLeave(t.wrapper, { dataTransfer: drag, relatedTarget: document.body })
    expect(screen.queryByTestId('composer-tree-drop-indicator')).toBeNull()
  })

  it('says why a folder is refused while it is over the composer', () => {
    const t = setup()
    const drag = dataTransfer({
      'text/plain': '/repo/My Docs',
      [TREE_ENTRY_DRAG_TYPE]: encodeTreeEntry({ path: '/repo/My Docs', kind: 'dir' }),
      [TREE_ENTRY_REFUSED_TYPE]: '1',
    })
    fireEvent.dragOver(t.wrapper, { dataTransfer: drag })
    const note = screen.getByTestId('composer-tree-drop-refused')
    expect(note.textContent).toBe("This folder can't be added: its path contains a space or @. Add its files one by one instead.")
    expect(note.getAttribute('role')).toBe('status')
    expect(screen.queryByTestId('composer-tree-drop-indicator')).toBeNull()
    expect(t.wrapper.getAttribute('data-tree-drop-refused')).toBe('true')
    fireEvent.drop(t.wrapper, { dataTransfer: drag })
    expect(t.onTreeEntryDrop).not.toHaveBeenCalled()
    expect(screen.queryByTestId('composer-tree-drop-refused')).toBeNull()
  })

  it('clears the indicator when the drag ends elsewhere', () => {
    const t = setup()
    fireEvent.dragOver(t.wrapper, { dataTransfer: treeDrag({ path: '/repo/a.ts', kind: 'file' }) })
    expect(screen.getByTestId('composer-tree-drop-indicator')).toBeTruthy()
    act(() => { window.dispatchEvent(new Event('dragend')) })
    expect(screen.queryByTestId('composer-tree-drop-indicator')).toBeNull()
  })

  it('passes a tree row to the host drop handler when no "Add to chat" handler is wired', () => {
    const onDrop = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onDrop={onDrop} />)
    fireEvent.drop(screen.getByRole('textbox'), { dataTransfer: treeDrag({ path: '/repo/a.ts', kind: 'file' }) })
    expect(onDrop).toHaveBeenCalledTimes(1)
  })
})
