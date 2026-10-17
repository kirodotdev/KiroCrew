/**
 * Typing through the real host chain (`CodeEditor` -> `PierreEditorImpl`)
 * keeps one Pierre surface and one document identity. Pierre rebuilds its
 * TextDocument, dropping focus and moving the caret to line 1, whenever its
 * `File` remounts or `file.cacheKey` changes, so the doubles record both.
 * Pierre's custom elements never upgrade under happy-dom, hence the doubles.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import { useEffect, useState, type ReactNode } from 'react'

type Emit = (file: { name: string; contents: string }) => void

const pierre = vi.hoisted(() => ({
  fileRenders: [] as { cacheKey?: string; onChange?: Emit }[],
  mounts: { current: 0 },
}))

vi.mock('@pierre/diffs/edit', () => ({ Editor: class {} }))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  function File(props: { file: { cacheKey?: string }; editorOptions?: { onChange?: Emit } }) {
    useEffect(() => {
      pierre.mounts.current++
    }, [])
    pierre.fileRenders.push({ cacheKey: props.file.cacheKey, onChange: props.editorOptions?.onChange })
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

import { CodeEditor } from '../components/CodeEditor'

/** Echoes every edit back as `content`, the way the side-panel file viewer does. */
function Host({ seed, onContent }: { seed: string; onContent: (v: string) => void }) {
  const [content, setContent] = useState(seed)
  onContent(content)
  return <CodeEditor content={content} lang="markdown" lineNums wordWrap filePath="notes/readme.md" onChange={setContent} />
}

beforeEach(() => {
  cleanup()
  pierre.fileRenders.length = 0
  pierre.mounts.current = 0
})

describe('CodeEditor typing', () => {
  it('keeps one Pierre surface and document identity across several keystrokes and a Return', () => {
    let hostContent = ''
    render(<Host seed={'# Title\n\nbody'} onContent={v => { hostContent = v }} />)
    const firstKey = pierre.fileRenders[0].cacheKey
    let text = '# Title\n\nbody'
    for (const ch of ['a', 'b', 'c', '\n', 'd']) {
      text += ch
      const emit = pierre.fileRenders[pierre.fileRenders.length - 1].onChange!
      act(() => emit({ name: 'readme.md', contents: text }))
    }
    expect(hostContent).toBe('# Title\n\nbodyabc\nd')
    expect(pierre.mounts.current).toBe(1)
    expect(new Set(pierre.fileRenders.map(r => r.cacheKey))).toEqual(new Set([firstKey]))
  })
})
