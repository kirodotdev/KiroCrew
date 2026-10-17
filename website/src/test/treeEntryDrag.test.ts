import { describe, expect, it } from 'vitest'
import {
  TREE_ENTRY_DRAG_TYPE,
  TREE_ENTRY_REFUSED_TYPE,
  classifyComposerDrop,
  decodeTreeEntry,
  encodeTreeEntry,
  readTreeEntry,
  treeEntryFromComposedPath,
  writeTreeEntry,
} from '../lib/treeEntryDrag'

/** A DataTransfer stand-in: these helpers only touch types/getData/setData,
 *  items[].kind, files and effectAllowed. */
function fakeDataTransfer(data: Record<string, string> = {}, opts: { files?: boolean } = {}): DataTransfer {
  const store: Record<string, string> = { ...data }
  const dt = {
    get types() { return [...Object.keys(store), ...(opts.files ? ['Files'] : [])] },
    getData: (type: string) => store[type] ?? '',
    setData: (type: string, value: string) => { store[type] = value },
    items: opts.files ? [{ kind: 'file' }] : [],
    files: [],
    effectAllowed: 'move',
    dropEffect: 'none',
  }
  return dt as unknown as DataTransfer
}

function row(path: string, type: 'file' | 'folder'): HTMLElement {
  const el = document.createElement('button')
  el.dataset.type = 'item'
  el.dataset.itemPath = path
  el.dataset.itemType = type
  return el
}

describe('tree entry payload', () => {
  it('round-trips a file and a folder', () => {
    for (const entry of [{ path: '/repo/src/a.ts', kind: 'file' as const }, { path: '/repo/src', kind: 'dir' as const }]) {
      expect(decodeTreeEntry(encodeTreeEntry(entry))).toEqual(entry)
    }
  })

  it('rejects malformed bodies', () => {
    for (const raw of [null, undefined, '', 'not json', '42', 'null', '{}', '{"path":"","kind":"file"}',
      '{"path":"/a","kind":"link"}', '{"path":5,"kind":"file"}']) {
      expect(decodeTreeEntry(raw)).toBeNull()
    }
  })

  it('writes the dashboard type and widens effectAllowed so a copy drop is not cancelled', () => {
    const dt = fakeDataTransfer({ 'text/plain': 'src/a.ts' })
    writeTreeEntry(dt, { path: '/repo/src/a.ts', kind: 'file' })
    expect(dt.types).toContain(TREE_ENTRY_DRAG_TYPE)
    expect(dt.effectAllowed).toBe('copyMove')
    expect(readTreeEntry(dt)).toEqual({ path: '/repo/src/a.ts', kind: 'file' })
  })

  it('reads nothing from a drag without the dashboard type', () => {
    expect(readTreeEntry(fakeDataTransfer({ 'text/plain': '{"path":"/a","kind":"file"}' }))).toBeNull()
    expect(readTreeEntry(null)).toBeNull()
  })
})

describe('treeEntryFromComposedPath', () => {
  it('builds the absolute path for a file row, skipping non-row targets', () => {
    const inner = document.createElement('span')
    expect(treeEntryFromComposedPath([inner, row('src/a.ts', 'file'), document.body], '/repo'))
      .toEqual({ entry: { path: '/repo/src/a.ts', kind: 'file' }, mentionable: true })
  })

  it('drops the folder trailing slash and a trailing slash on the root', () => {
    expect(treeEntryFromComposedPath([row('src/pages/', 'folder')], '/repo/'))
      .toEqual({ entry: { path: '/repo/src/pages', kind: 'dir' }, mentionable: true })
  })

  it('normalizes a Windows root to the form the context menu uses', () => {
    expect(treeEntryFromComposedPath([row('src/a.ts', 'file')], 'C:\\work\\repo'))
      .toEqual({ entry: { path: 'C:/work/repo/src/a.ts', kind: 'file' }, mentionable: true })
  })

  it('marks a folder whose path holds whitespace as not mentionable, but not a file', () => {
    expect(treeEntryFromComposedPath([row('docs/My Folder/', 'folder')], '/My Projects/repo')?.mentionable).toBe(false)
    expect(treeEntryFromComposedPath([row('node_modules/@types/', 'folder')], '/repo')?.mentionable).toBe(false)
    expect(treeEntryFromComposedPath([row('node_modules/@types/x.d.ts', 'file')], '/repo')?.mentionable).toBe(true)
    expect(treeEntryFromComposedPath([row('docs/My Report.pdf', 'file')], '/repo')?.mentionable).toBe(true)
    // Whitespace in the project root itself does not count: the mention is relative.
    expect(treeEntryFromComposedPath([row('docs/', 'folder')], '/My Projects/repo')?.mentionable).toBe(true)
  })

  it('is null when the drag did not start on a row', () => {
    const notRow = document.createElement('button')
    notRow.dataset.itemPath = 'src/a.ts'
    expect(treeEntryFromComposedPath([notRow, document.body], '/repo')).toBeNull()
    expect(treeEntryFromComposedPath([], '/repo')).toBeNull()
  })
})

describe('classifyComposerDrop', () => {
  it('routes a tree row even though it also carries text/plain', () => {
    const dt = fakeDataTransfer({ 'text/plain': 'src/a.ts', [TREE_ENTRY_DRAG_TYPE]: '{}' })
    expect(classifyComposerDrop(dt)).toBe('tree-entry')
  })

  it('routes a tree row marked not mentionable as refused', () => {
    const dt = fakeDataTransfer({ 'text/plain': 'docs/My Folder/' })
    writeTreeEntry(dt, { path: '/repo/docs/My Folder', kind: 'dir' }, { mentionable: false })
    expect(dt.types).toContain(TREE_ENTRY_REFUSED_TYPE)
    expect(classifyComposerDrop(dt)).toBe('tree-refused')
  })

  it('routes OS files, selected text and anything else', () => {
    expect(classifyComposerDrop(fakeDataTransfer({}, { files: true }))).toBe('files')
    expect(classifyComposerDrop(fakeDataTransfer({ 'text/plain': 'hello' }))).toBe('text')
    expect(classifyComposerDrop(fakeDataTransfer({ 'text/uri-list': 'https://x' }))).toBe('none')
    expect(classifyComposerDrop(null)).toBe('none')
  })
})
