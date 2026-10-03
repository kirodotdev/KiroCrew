/**
 * The chat scratch editor (`EditableCodeBlock` -> `PierreEditorImpl`) after an
 * edit that changes the line count.
 *
 * Pierre keeps a highlighted file's rows aligned with its own document from an
 * incremental render cache, so the `file` the seam hands it can stay one object
 * for the whole edit session (the caret survives, the key never moves). A file
 * Pierre renders as PLAIN TEXT -- a fence with no language, ```text, or a tag
 * that is not a file extension (`snippet.python`, `snippet.typescript`) -- is
 * different: on every line-count change Pierre renders it again from
 * `file.contents`, and a seed frozen at the opening text puts the deleted line
 * back on screen while the document keeps the deletion, so the next keystroke
 * lands on the line that moved into that row. These tests pin what the seam
 * hands Pierre: one object per session, whose `contents` mirror the buffer.
 *
 * Pierre's custom elements never upgrade under happy-dom, so the library is
 * replaced with doubles that keep the `file` OBJECT each render hands the editor
 * surface (its `contents` are read live, since a mirrored edit re-renders
 * nothing) and expose the editor's `onChange` to emit edits the way the real
 * editor does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup, fireEvent, act, screen } from '@testing-library/react'
import { useEffect, type ReactNode } from 'react'

type Emit = (file: { name: string; contents: string }) => void
type PierreFile = { name: string; contents: string; cacheKey?: string }

const pierre = vi.hoisted(() => ({
  fileRenders: [] as { file: PierreFile; onChange?: Emit }[],
  mounts: { current: 0 },
}))

vi.mock('@pierre/diffs/edit', () => ({ Editor: class {} }))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  function File(props: { file: PierreFile; editorOptions?: { onChange?: Emit } }) {
    useEffect(() => {
      pierre.mounts.current++
    }, [])
    pierre.fileRenders.push({ file: props.file, onChange: props.editorOptions?.onChange })
    return <div data-testid="pierre-file" />
  }
  return {
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    EditProvider: ({ children }: { children?: ReactNode }) => <>{children}</>,
    File,
    MultiFileDiff: () => null,
    FileDiff: () => null,
    WorkerPoolContext: createContext(null),
  }
})

vi.mock('../pierre/PierreImpl', async () => {
  const { activeWorkerPool, contentCacheKey } = await vi.importActual<typeof import('../pierre/PierreImpl')>('../pierre/PierreImpl')
  return {
    activeWorkerPool,
    contentCacheKey,
    PierreShell: ({ children, generation }: { children?: ReactNode; generation: number }) => <div key={generation}>{children}</div>,
    usePierreWorkerPool: () => ({ phase: 'ready', generation: 1, pool: {} }),
    useRegisterEditorSurface: () => {},
  }
})

// `../pierre` lazy-loads the editor; resolve it eagerly so the chain renders synchronously.
vi.mock('../pierre', async () => {
  const { PierreEditorImpl } = await vi.importActual<typeof import('../pierre/PierreEditorImpl')>('../pierre/PierreEditorImpl')
  return { PierreEditor: PierreEditorImpl }
})

// The rendered (non-editing) block only matters here as the host of the pencil
// that swaps in the editor.
vi.mock('../components/CodeBlock', () => ({
  CodeBlock: ({ headerActions }: { headerActions?: ReactNode }) => <div>{headerActions}</div>,
}))

import EditableCodeBlock from '../components/EditableCodeBlock'

const CODE = ['const one = 1', 'const two = 2', 'const three = 3', 'const four = 4', 'const five = 5'].join('\n')
const DELETED = ['const one = 1', 'const two = 2', 'const four = 4', 'const five = 5'].join('\n')
const lines = (text: string) => text.split('\n')

const lastRender = () => pierre.fileRenders[pierre.fileRenders.length - 1]

/** Mount the block, click the pencil, return the editor's emit. */
function openEditor(lang: string | undefined) {
  render(<EditableCodeBlock code={CODE} lang={lang} complete />)
  fireEvent.click(screen.getAllByRole('button')[0])
  expect(screen.getByTestId('pierre-file')).toBeTruthy()
  const emit = (contents: string) => {
    const onChange = lastRender().onChange
    expect(onChange, 'the editor surface carries an onChange').toBeTruthy()
    act(() => onChange!({ name: lastRender().file.name, contents }))
  }
  return { emit }
}

beforeEach(() => {
  cleanup()
  pierre.fileRenders.length = 0
  pierre.mounts.current = 0
})

describe('EditableCodeBlock line re-flow in a plain-text fence', () => {
  // A fence with no language becomes `snippet.txt`, which Pierre renders as
  // plain text: every line-count change re-renders the rows from `file.contents`.
  it('hands Pierre the buffer when a middle line is deleted, as the same file under the same key', () => {
    const { emit } = openEditor(undefined)
    const opened = lastRender().file
    expect(opened.name).toBe('snippet.txt')
    expect(lines(opened.contents)).toEqual(lines(CODE))
    const openedKey = opened.cacheKey

    // Delete line 3 ("const three = 3") whole, as the editor reports it.
    emit(DELETED)

    // The rows Pierre renders a plain file from are the buffer's lines ...
    expect(lines(lastRender().file.contents)).toEqual(lines(DELETED))
    // ... read off the very object it holds, under the key it opened with, so
    // the document and caret are kept and nothing remounted.
    expect(lastRender().file).toBe(opened)
    expect(lastRender().file.cacheKey).toBe(openedKey)
    expect(pierre.mounts.current).toBe(1)
  })

  it('shows the line the next keystroke edits in the row it lands on', () => {
    const { emit } = openEditor(undefined)
    emit(DELETED)
    const afterDelete = lastRender().file
    // Row 3 shows the line that moved up into it ...
    expect(lines(afterDelete.contents)[2]).toBe('const four = 4')

    // ... and typing at the start of row 3 edits exactly that line.
    const typed = ['const one = 1', 'const two = 2', 'Xconst four = 4', 'const five = 5'].join('\n')
    emit(typed)

    expect(lastRender().file).toBe(afterDelete)
    expect(lines(lastRender().file.contents)).toEqual(lines(typed))
  })

  it('hands Pierre the buffer when a line is inserted, under the same key', () => {
    const { emit } = openEditor(undefined)
    const opened = lastRender().file
    const openedKey = opened.cacheKey

    const inserted = ['const one = 1', 'const two = 2', 'const three = 3', 'const inserted = 0', 'const four = 4', 'const five = 5'].join('\n')
    emit(inserted)

    expect(lines(lastRender().file.contents)).toEqual(lines(inserted))
    expect(lastRender().file).toBe(opened)
    expect(lastRender().file.cacheKey).toBe(openedKey)
    expect(pierre.mounts.current).toBe(1)
  })

  it('keeps mirroring through a delete, an in-line edit and an insert', () => {
    const { emit } = openEditor(undefined)
    const opened = lastRender().file
    emit(DELETED)
    emit(['const one = 1', 'const two = 2', 'Xconst four = 4', 'const five = 5'].join('\n'))
    const grown = ['const one = 1', 'const two = 2', 'Xconst four = 4', '', 'const five = 5'].join('\n')
    emit(grown)

    expect(lines(lastRender().file.contents)).toEqual(lines(grown))
    expect(lastRender().file).toBe(opened)
    expect(pierre.mounts.current).toBe(1)
  })
})

describe('EditableCodeBlock line re-flow in a highlighted fence', () => {
  // A ```js fence becomes `snippet.js`, which Pierre highlights: its rows come
  // from the editor's own render cache, realigned on every line-count change.
  // The same contract holds -- one object, one key -- and the mirrored text is
  // simply never read.
  it('keeps one file object and one key across a line deletion and an insertion', () => {
    const { emit } = openEditor('js')
    const opened = lastRender().file
    expect(opened.name).toBe('snippet.js')
    const openedKey = opened.cacheKey

    emit(DELETED)
    expect(lastRender().file).toBe(opened)
    expect(lastRender().file.cacheKey).toBe(openedKey)
    expect(lines(lastRender().file.contents)).toEqual(lines(DELETED))

    emit(['const one = 1', 'const two = 2', 'const four = 4', '', 'const five = 5'].join('\n'))
    expect(lastRender().file).toBe(opened)
    expect(lastRender().file.cacheKey).toBe(openedKey)
    expect(pierre.mounts.current).toBe(1)
  })
})
