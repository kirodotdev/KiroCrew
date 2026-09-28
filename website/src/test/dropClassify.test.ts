import { describe, it, expect, afterEach } from 'vitest'
import { classifyDrop } from '../utils/dropClassify'

/* Fixture helpers — classifyDrop only touches items[].kind /
 * .webkitGetAsEntry() / .getAsFile() and dt.files, so plain objects cast to
 * the DOM types are a faithful stand-in for a real drop event's payload. */

function fileOf(name: string): File {
  return new File(['x'], name, { type: 'text/plain' })
}

interface ItemSpec {
  kind?: string
  entry?: { isDirectory: boolean } | null
  /** `false` drops webkitGetAsEntry from the item entirely (non-Chromium). */
  hasEntryApi?: boolean
  file?: File | null
}

function dt(items: ItemSpec[], files: File[] = []): DataTransfer {
  return {
    items: items.map(spec => ({
      kind: spec.kind ?? 'file',
      ...(spec.hasEntryApi === false ? {} : { webkitGetAsEntry: () => spec.entry ?? null }),
      getAsFile: () => (spec.file === undefined ? null : spec.file),
    })),
    files,
  } as unknown as DataTransfer
}

/** Install/remove the desktop shell's path bridge. */
function stubBridge(impl: ((f: File) => string) | null) {
  const w = window as { kirocrew?: { getPathForFile?: (f: File) => string } }
  if (impl) w.kirocrew = { getPathForFile: impl }
  else delete w.kirocrew
}

afterEach(() => stubBridge(null))

describe('classifyDrop', () => {
  it('routes regular files to the upload path', () => {
    const f = fileOf('a.txt')
    const out = classifyDrop(dt([{ entry: { isDirectory: false }, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('routes a directory to path insertion when the desktop shell resolves a path', () => {
    stubBridge(() => '/Users/me/Projects/demo')
    const out = classifyDrop(dt([{ entry: { isDirectory: true }, file: fileOf('demo') }]))
    expect(out.dirPaths).toEqual(['/Users/me/Projects/demo'])
    expect(out.files).toEqual([])
  })

  it('falls back to the upload route when the path contains whitespace (untokenizable)', () => {
    // DIR_TOKEN_RE (shared with the @-picker) cannot carry whitespace: the
    // inserted text would look like a folder token but never parse into a
    // chip or serialize on send. A silent dead token is worse than today's
    // upload attempt, so the classifier keeps the upload route.
    stubBridge(() => '/Users/me/My Project')
    const f = fileOf('My Project')
    const out = classifyDrop(dt([{ entry: { isDirectory: true }, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('falls back to the upload route when the path contains @ (untokenizable)', () => {
    stubBridge(() => '/Users/alice@team/work')
    const f = fileOf('work')
    const out = classifyDrop(dt([{ entry: { isDirectory: true }, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('falls back to the upload route for a filesystem root (POSIX `/`)', () => {
    // parseDirTokens rejects slash-only bodies, so `@/` would be a dead token.
    stubBridge(() => '/')
    const f = fileOf('root')
    const out = classifyDrop(dt([{ entry: { isDirectory: true }, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('falls back to the upload route for a Windows drive root (`C:\\`)', () => {
    stubBridge(() => 'C:\\')
    const f = fileOf('C')
    const out = classifyDrop(dt([{ entry: { isDirectory: true }, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('falls back to the upload route for a directory with NO resolvable path (browser)', () => {
    // No bridge at all: the folder name alone is not a usable path, so the
    // deliberate decision is to keep today's behaviour, not to insert it.
    const f = fileOf('demo')
    const out = classifyDrop(dt([{ entry: { isDirectory: true }, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('falls back to the upload route when the bridge returns an empty path', () => {
    stubBridge(() => '')
    const f = fileOf('demo')
    const out = classifyDrop(dt([{ entry: { isDirectory: true }, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('falls back to the upload route when the bridge throws', () => {
    stubBridge(() => { throw new Error('ipc gone') })
    const f = fileOf('demo')
    const out = classifyDrop(dt([{ entry: { isDirectory: true }, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('splits a mixed drop: files upload, folders insert paths', () => {
    stubBridge(() => '/abs/folder')
    const f = fileOf('a.txt')
    const out = classifyDrop(dt([
      { entry: { isDirectory: false }, file: f },
      { entry: { isDirectory: true }, file: fileOf('folder') },
    ]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual(['/abs/folder'])
  })

  it('skips non-file items (dragged text) without touching the file routes', () => {
    const f = fileOf('a.txt')
    const out = classifyDrop(dt([
      { kind: 'string', file: null },
      { entry: { isDirectory: false }, file: f },
    ]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('treats an item without the entry API as a plain file (non-Chromium)', () => {
    const f = fileOf('a.txt')
    const out = classifyDrop(dt([{ hasEntryApi: false, file: f }]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('treats a null entry (non-filesystem drag source) as a plain file', () => {
    const f = fileOf('a.txt')
    const out = classifyDrop(dt([{ entry: null, file: f }]))
    expect(out.files).toEqual([f])
  })

  it('skips a file item whose getAsFile() returns null', () => {
    const out = classifyDrop(dt([{ entry: { isDirectory: false }, file: null }]))
    expect(out.files).toEqual([])
    expect(out.dirPaths).toEqual([])
  })

  it('falls back to dataTransfer.files when there are no file-kind items', () => {
    const f = fileOf('a.txt')
    const out = classifyDrop(dt([{ kind: 'string', file: null }], [f]))
    expect(out.files).toEqual([f])
    expect(out.dirPaths).toEqual([])
  })

  it('falls back to dataTransfer.files when items is empty', () => {
    const f = fileOf('a.txt')
    const out = classifyDrop(dt([], [f]))
    expect(out.files).toEqual([f])
  })
})
