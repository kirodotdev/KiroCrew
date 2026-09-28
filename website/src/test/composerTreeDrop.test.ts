import { renderHook } from '@testing-library/react'
import type { DragEvent as ReactDragEvent } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { isInsideProject, useComposerTreeDrop } from '../components/composerTreeDrop'
import { TREE_ENTRY_DRAG_TYPE, TREE_ENTRY_REFUSED_TYPE, encodeTreeEntry } from '../lib/treeEntryDrag'

describe('isInsideProject', () => {
  it('accepts paths below the project root only', () => {
    expect(isInsideProject('/repo/src/a.ts', '/repo')).toBe(true)
    expect(isInsideProject('/repo/src', '/repo/')).toBe(true)
    expect(isInsideProject('C:/work/repo/a.ts', 'C:\\work\\repo')).toBe(true)
    expect(isInsideProject('/repo', '/repo')).toBe(false)
    expect(isInsideProject('/repository/a.ts', '/repo')).toBe(false)
    expect(isInsideProject('/home/u/.ssh/id_rsa', '/repo')).toBe(false)
    expect(isInsideProject('/repo/../etc/passwd', '/repo')).toBe(false)
    expect(isInsideProject('/repo/a.ts', '')).toBe(false)
    // An empty segment would relativize to an absolute path outside the root.
    expect(isInsideProject('/repo//etc', '/repo')).toBe(false)
    expect(isInsideProject('/repo/a//b.ts', '/repo')).toBe(false)
  })

  it('refuses Win32 segments that collapse to a parent reference', () => {
    for (const seg of ['.. ', '...', '. ', '..  .']) {
      expect(isInsideProject(`C:/repo/${seg}/secret.txt`, 'C:\\repo')).toBe(false)
    }
    expect(isInsideProject('C:/repo/./a.ts', 'C:\\repo')).toBe(false)
    expect(isInsideProject('C:/repo/..rc/a.ts', 'C:\\repo')).toBe(true)
    // On POSIX those are ordinary names; only `.` and `..` are special.
    expect(isInsideProject('/repo/.../a.ts', '/repo')).toBe(true)
  })
})

describe('useComposerTreeDrop', () => {
  function dragEvent(data: Record<string, string>) {
    const dataTransfer = {
      types: Object.keys(data),
      getData: (t: string) => data[t] ?? '',
      items: [],
      files: [],
      dropEffect: 'none',
    }
    const event = { dataTransfer, preventDefault: vi.fn(), stopPropagation: vi.fn(), relatedTarget: null }
    return event as unknown as ReactDragEvent & { dataTransfer: { dropEffect: string }; preventDefault: ReturnType<typeof vi.fn> }
  }
  const treeRow = (path: string, kind: 'file' | 'dir', refused = false) => dragEvent({
    'text/plain': path,
    [TREE_ENTRY_DRAG_TYPE]: encodeTreeEntry({ path, kind }),
    ...(refused ? { [TREE_ENTRY_REFUSED_TYPE]: '1' } : {}),
  })
  function mount({ enabled = true, withHandler = true } = {}) {
    const onTreeEntryDrop = vi.fn()
    const onDragOver = vi.fn()
    const onDrop = vi.fn()
    const { result } = renderHook(() => useComposerTreeDrop({
      enabled,
      project: '/repo',
      onTreeEntryDrop: withHandler ? onTreeEntryDrop : undefined,
      getControl: () => null,
      containerRef: { current: null },
      onDragOver,
      onDrop,
    }))
    return { result, onTreeEntryDrop, onDragOver, onDrop }
  }

  it('accepts a tree row as a copy, so the browser does not cancel the drop', () => {
    const t = mount()
    const event = treeRow('/repo/a.ts', 'file')
    t.result.current.onDragOver(event)
    expect(event.preventDefault).toHaveBeenCalled()
    expect(event.dataTransfer.dropEffect).toBe('copy')
    expect(t.onDragOver).not.toHaveBeenCalled()
  })

  it('shows the no-drop cursor for a folder the composer cannot mention', () => {
    const t = mount()
    const event = treeRow('/repo/My Folder', 'dir', true)
    t.result.current.onDragOver(event)
    expect(event.dataTransfer.dropEffect).toBe('none')
    t.result.current.onDrop(treeRow('/repo/My Folder', 'dir', true))
    expect(t.onTreeEntryDrop).not.toHaveBeenCalled()
    expect(t.onDrop).not.toHaveBeenCalled()
  })

  it('hands a dropped row to the host with its absolute path and kind', () => {
    const t = mount()
    t.result.current.onDrop(treeRow('/repo/src/a.ts', 'file'))
    t.result.current.onDrop(treeRow('/repo/src', 'dir'))
    expect(t.onTreeEntryDrop.mock.calls).toEqual([['/repo/src/a.ts', 'file'], ['/repo/src', 'dir']])
  })

  it('refuses a payload naming a path outside the project', () => {
    const t = mount()
    t.result.current.onDrop(treeRow('/home/u/.ssh/id_rsa', 'file'))
    expect(t.onTreeEntryDrop).not.toHaveBeenCalled()
  })

  it('leaves OS file drags and text drags to the host handlers', () => {
    const t = mount()
    const files = dragEvent({ Files: '' })
    const text = dragEvent({ 'text/plain': 'hello' })
    t.result.current.onDragOver(text)
    t.result.current.onDrop(files)
    t.result.current.onDrop(text)
    expect(t.onDragOver).toHaveBeenCalledWith(text)
    expect(t.onDrop.mock.calls).toEqual([[files], [text]])
    expect(text.dataTransfer.dropEffect).toBe('none')
    expect(t.onTreeEntryDrop).not.toHaveBeenCalled()
  })

  it('passes tree rows through untouched when disabled or when the host takes none', () => {
    for (const opts of [{ enabled: false }, { withHandler: false }]) {
      const t = mount(opts)
      const event = treeRow('/repo/a.ts', 'file')
      t.result.current.onDrop(event)
      expect(t.onDrop).toHaveBeenCalledWith(event)
      expect(t.onTreeEntryDrop).not.toHaveBeenCalled()
    }
  })
})
