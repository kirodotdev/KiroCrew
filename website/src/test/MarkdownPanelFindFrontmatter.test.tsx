/**
 * Cmd+F in the markdown preview reaches the frontmatter strip.
 *
 * The strip renders beside `previewRef` (the body-only root comment offsets are
 * measured in), so a find scoped to `previewRef` reported zero matches for text
 * the reader could see in the expanded strip, while its key handler had already
 * blocked the browser's own find. The find now searches `findRootRef`, which
 * holds the strip and the body together.
 *
 * `Highlight` and `CSS.highlights` are stubbed BEFORE the hook is imported,
 * because it captures both into module-level constants at load time.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { useRef } from 'react'
import { render, act, screen, fireEvent } from '@testing-library/react'
import { installHighlightApiStub, paintedText } from './highlightApiStub'
import { stubManualFrames } from './manualFrames'
import '../i18n/all'

installHighlightApiStub()
const { useMarkdownPanelFind } = await import('../components/useMarkdownPanelFind')
const { ContentRenderer } = await import('../components/ContentRenderer')

const DOC = ['---', 'owner: reliability-guild', 'title: Hello', '---', '# Hello', '', 'Body text.'].join('\n')

type Find = ReturnType<typeof useMarkdownPanelFind>
let find: Find

function Panel() {
  const previewRef = useRef<HTMLDivElement>(null)
  const findRootRef = useRef<HTMLDivElement>(null)
  const fullscreenPreviewRef = useRef<HTMLDivElement>(null)
  find = useMarkdownPanelFind({
    active: true, editing: false, diffMode: false, isMarkdown: true, fullscreen: false, content: DOC,
    previewRef, fullscreenPreviewRef, findRootRef,
  })
  return (
    <ContentRenderer
      isRichType={false} fileType="markdown" filePath="/w/notes.md" content={DOC} editing={false}
      lang="markdown" lineNums={false} wordWrap onChange={() => {}}
      previewRef={previewRef} findRootRef={findRootRef} displayContent={DOC} isMarkdown
    />
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

let frames: ReturnType<typeof stubManualFrames>

function search(term: string) {
  frames = stubManualFrames()
  render(<Panel />)
  act(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true })) })
  act(() => frames.runFrame())
  act(() => find.setFindTerm(term))
}

describe('markdown preview find and the frontmatter strip', () => {
  it('finds a value that appears only in the frontmatter strip', () => {
    search('reliability-guild')
    expect(find.findCount).toBe(1)
    expect(paintedText('mc-find-current')).toEqual(['reliability-guild'])
  })

  it('still finds body text, counting a word once per place it is shown', () => {
    search('Hello')
    // `title: Hello` in the strip and `# Hello` in the body.
    expect(find.findCount).toBe(2)
  })

  it('drops the strip matches when the strip collapses', async () => {
    search('reliability-guild')
    expect(find.findCount).toBe(1)
    fireEvent.click(screen.getByRole('button', { expanded: true }))
    await Promise.resolve() // happy-dom hands over a mutation's records on a microtask
    act(() => frames.runFrame())
    expect(find.findCount).toBe(0)
  })
})
