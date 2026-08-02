import { describe, it, expect } from 'vitest'

import { classifyDrop } from '../utils/dropClassify'

/**
 * A dropped folder must become a PATH inserted into the composer, not an upload.
 * A dropped file must keep uploading. The reliable directory signal in a browser
 * is `DataTransferItem.webkitGetAsEntry().isDirectory`; the absolute path is only
 * available inside Electron (webUtils), threaded in via `resolvePath`.
 *
 * These build minimal DataTransfer-shaped fixtures so the routing decision is
 * regression-tested independently of the async upload plumbing (same approach as
 * uploadRouting.test.ts).
 */

interface EntryStub {
  isDirectory: boolean
}

function makeItem(file: File | null, entry: EntryStub | null): DataTransferItem {
  return {
    kind: 'file',
    type: file?.type ?? '',
    getAsFile: () => file,
    webkitGetAsEntry: () => entry as unknown as FileSystemEntry | null,
  } as unknown as DataTransferItem
}

function makeDataTransfer(items: DataTransferItem[], files: File[]): DataTransfer {
  return {
    items: items as unknown as DataTransferItemList,
    files: files as unknown as FileList,
  } as unknown as DataTransfer
}

const fileOf = (name: string) => new File(['x'], name, { type: 'text/plain' })
const folderStub = (name: string) => new File([], name, { type: '' })

describe('classifyDrop (folder-vs-file drop routing)', () => {
  it('routes a dropped folder to a composer path when the path resolves (Electron)', () => {
    const folder = folderStub('project')
    const dt = makeDataTransfer([makeItem(folder, { isDirectory: true })], [folder])
    const res = classifyDrop(dt, () => '/Users/me/project')
    expect(res.folderPaths).toEqual(['/Users/me/project'])
    expect(res.files).toEqual([]) // no upload started for the folder
    expect(res.blockedFolders).toBe(0)
  })

  it('keeps uploading a dropped file', () => {
    const file = fileOf('notes.txt')
    const dt = makeDataTransfer([makeItem(file, { isDirectory: false })], [file])
    const res = classifyDrop(dt, () => undefined)
    expect(res.files).toEqual([file])
    expect(res.folderPaths).toEqual([])
    expect(res.blockedFolders).toBe(0)
  })

  it('flags a folder as blocked (no upload) when no path resolves (plain browser)', () => {
    const folder = folderStub('project')
    const dt = makeDataTransfer([makeItem(folder, { isDirectory: true })], [folder])
    const res = classifyDrop(dt, () => undefined)
    expect(res.folderPaths).toEqual([])
    expect(res.files).toEqual([]) // folder never uploaded as garbage
    expect(res.blockedFolders).toBe(1)
  })

  it('separates a mixed folder + file drop', () => {
    const folder = folderStub('src')
    const file = fileOf('a.png')
    const dt = makeDataTransfer(
      [makeItem(folder, { isDirectory: true }), makeItem(file, { isDirectory: false })],
      [folder, file],
    )
    const res = classifyDrop(dt, f => (f === folder ? '/abs/src' : undefined))
    expect(res.folderPaths).toEqual(['/abs/src'])
    expect(res.files).toEqual([file])
  })

  it('falls back to uploading all files when the entry API is unavailable (undetectable)', () => {
    const file = fileOf('legacy.bin')
    // No `webkitGetAsEntry` on items -> cannot detect directories; preserve
    // current behavior rather than guessing.
    const items = [{ kind: 'file', getAsFile: () => file } as unknown as DataTransferItem]
    const dt = makeDataTransfer(items, [file])
    const res = classifyDrop(dt, () => undefined)
    expect(res.files).toEqual([file])
    expect(res.folderPaths).toEqual([])
    expect(res.blockedFolders).toBe(0)
  })
})
