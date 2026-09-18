import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import SearchHighlightContext, { MessageSearchScope } from '../src/hooks/SearchHighlightContext'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import { SEARCH_HL_MATCH, SEARCH_HL_CURRENT } from '../src/utils/domHighlight'
import { installHighlightApiStub, registeredRanges } from '../src/test/highlightApiStub'

vi.mock('../src/utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))

/** Search matches are Ranges on the page-wide `CSS.highlights` entries, never
 *  elements in the bubble (the bubble is React-owned; see domHighlight.ts). The
 *  registry is shared by every mounted bubble, so each assertion scopes to the
 *  ranges that point into its own container. */
beforeAll(() => { installHighlightApiStub() })

function inContainer(container: HTMLElement, name: string): Range[] {
  return registeredRanges(name).filter(r => container.contains(r.startContainer))
}
function painted(container: HTMLElement, name: string): string[] {
  return inContainer(container, name).map(r => r.toString())
}
/** Every range on either highlight, in document order, tagged by which one holds it. */
function paintedInOrder(container: HTMLElement): Array<'match' | 'current'> {
  const all = [
    ...inContainer(container, SEARCH_HL_MATCH).map(r => ({ r, kind: 'match' as const })),
    ...inContainer(container, SEARCH_HL_CURRENT).map(r => ({ r, kind: 'current' as const })),
  ]
  all.sort((a, b) => a.r.compareBoundaryPoints(Range.START_TO_START, b.r))
  return all.map(x => x.kind)
}

function renderWithSearch(content: string, term: string, currentOcc: number) {
  return render(
    <SearchHighlightContext.Provider value={{ term, caseSensitive: false, currentMessageIdx: currentOcc >= 0 ? 0 : -1, currentOccurrenceIdx: currentOcc }}>
      <MessageSearchScope messageIdx={0}>
        <AssistantMessage content={content} isStreaming={false} />
      </MessageSearchScope>
    </SearchHighlightContext.Provider>,
  )
}

describe('AssistantMessage search highlighting', () => {
  it('paints nothing when term is empty', () => {
    const { container } = renderWithSearch('hello world', '', -1)
    expect(paintedInOrder(container)).toEqual([])
  })

  it('paints matching text in rendered paragraphs without touching the DOM', async () => {
    const { container } = renderWithSearch('hello world', 'world', -1)
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH)).toEqual(['world'])
    })
    expect(container.querySelectorAll('mark')).toHaveLength(0)
    expect(container.querySelector('p')!.childNodes).toHaveLength(1)
  })

  it('paints only the specified occurrence as current', async () => {
    const { container } = renderWithSearch('world hello world', 'world', 1)
    await waitFor(() => {
      expect(paintedInOrder(container)).toEqual(['match', 'current'])
    })
    expect(inContainer(container, SEARCH_HL_CURRENT)[0].startOffset).toBe(12)
  })

  it('paints inside bold text', async () => {
    const { container } = renderWithSearch('**bold text** here', 'bold', -1)
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH)).toEqual(['bold'])
    })
    expect(inContainer(container, SEARCH_HL_MATCH)[0].startContainer.parentElement!.tagName).toBe('STRONG')
  })

  it('paints inside list items', async () => {
    const { container } = renderWithSearch('- item one\n- item two', 'item', -1)
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH)).toEqual(['item', 'item'])
    })
  })

  it('single occurrence counter across markdown and code blocks', async () => {
    const content = 'The word deploy here.\n\n```python\nprint("deploy")\n```\n\nAnd deploy again.'
    const { container } = renderWithSearch(content, 'deploy', 1)
    // The code block's "deploy" starts as plain text (the `pierre-plain`
    // stand-in in CodeBlock.tsx) and is re-painted once Pierre's staged,
    // chunk-loaded mount admits it -- a MutationObserver re-runs the TreeWalker
    // pass when that swap lands (AssistantMessage.tsx). On Windows that chunk
    // load + staged-mount queue can outlast the default waitFor timeout
    // (1000ms), so the assertion below can observe an in-between DOM state
    // where the occurrence count is right but the swap hasn't finished
    // reordering text nodes yet. Same class as DiffBlock.streaming.test.tsx /
    // PierreWorkspaceTree.lazy.test.tsx; widen rather than assume it always
    // settles within 1s.
    await waitFor(() => {
      expect(paintedInOrder(container)).toEqual(['match', 'current', 'match'])
    }, { timeout: 5000 })
  })

  it('highlights clear when term changes to empty', async () => {
    const { container, rerender } = render(
      <SearchHighlightContext.Provider value={{ term: 'hello', caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content="hello world" isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH).length).toBeGreaterThan(0)
    })
    rerender(
      <SearchHighlightContext.Provider value={{ term: '', caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content="hello world" isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(paintedInOrder(container)).toEqual([])
    })
  })

  it('paints nothing in content that does not match', () => {
    const { container } = renderWithSearch('hello world', 'xyz', -1)
    expect(paintedInOrder(container)).toEqual([])
  })

  it('paints inside table cells', async () => {
    const content = '| Name | Status |\n|------|--------|\n| deploy | active |\n| deploy | idle |'
    const { container } = renderWithSearch(content, 'deploy', -1)
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH)).toEqual(['deploy', 'deploy'])
    })
  })

  it('paints inside inline code', async () => {
    const { container } = renderWithSearch('Use the `deploy` command to run `deploy-prod`', 'deploy', -1)
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH).length).toBeGreaterThanOrEqual(2)
    })
  })
})
