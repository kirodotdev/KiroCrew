import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createContext } from 'react'
import { render } from '@testing-library/react'

// The markdown preview hands its open handlers to the renderer, whose path chips
// read them through context. Without them a chip in the Files pane said "Click to
// open" and fell back to revealing the path on the host, which does nothing on a
// remote gateway.
const seen: Array<Record<string, unknown>> = []
vi.mock('../components/MarkdownRenderer', () => ({
  default: (props: Record<string, unknown>) => {
    seen.push(props)
    return <div data-testid="md" />
  },
  BasePathCtx: createContext<string | null>(null),
}))

const { ContentRenderer } = await import('../components/ContentRenderer')

function preview(extra: Record<string, unknown> = {}) {
  return render(
    <ContentRenderer
      isRichType={false}
      fileType="markdown"
      filePath="/w/notes.md"
      content="see `/w/a.ts`"
      editing={false}
      lang="markdown"
      lineNums={false}
      wordWrap
      onChange={() => {}}
      onSave={() => {}}
      previewRef={{ current: null }}
      displayContent="see `/w/a.ts`"
      isMarkdown
      {...extra}
    />,
  )
}

beforeEach(() => {
  seen.length = 0
})

describe('ContentRenderer markdown preview path actions', () => {
  it('passes the open handlers to the markdown renderer', () => {
    const onFileOpen = vi.fn()
    const onFolderOpen = vi.fn()
    preview({ onFileOpen, onFolderOpen })
    expect(seen.at(-1)?.onFileOpen).toBe(onFileOpen)
    expect(seen.at(-1)?.onFolderOpen).toBe(onFolderOpen)
  })

  it('passes none when the host wired none, so chips keep their fallback', () => {
    preview()
    expect(seen.at(-1)?.onFileOpen).toBeUndefined()
    expect(seen.at(-1)?.onFolderOpen).toBeUndefined()
  })
})
