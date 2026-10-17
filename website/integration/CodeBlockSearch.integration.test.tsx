import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import SearchHighlightContext, { MessageSearchScope } from '../src/hooks/SearchHighlightContext'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import { SEARCH_HL_MATCH, SEARCH_HL_CURRENT } from '../src/utils/domHighlight'
import { installHighlightApiStub, registeredRanges } from '../src/test/highlightApiStub'

vi.mock('../src/utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))

/** The subject here is the search pass -- `applySearchHighlights` plus the
 *  MutationObserver that re-runs it when a code block's DOM lands late -- not
 *  Pierre's highlighter. Pierre highlights in a worker, and under a loaded
 *  coverage-instrumented shard that worker never resolved inside the test
 *  budget, so the code text (and therefore every painted range) simply never
 *  appeared: the suite passed alone and failed in CI. Rendering the code
 *  synchronously keeps the whole chain under test and makes it deterministic.
 *
 *  The ranges still have to arrive through the observer, so the polls below stay
 *  -- with a deadline under vitest's own `testTimeout`, or a genuine failure is
 *  reported as a timeout instead of as the assertion that broke.
 *
 *  A fully synchronous stub would however delete the observer's whole reason to
 *  exist: with the code text present on first paint, `applySearchHighlights`
 *  finds it on the initial pass and every case here would keep passing with the
 *  MutationObserver removed. So the stub can also mount EMPTY and land its text
 *  in a later task (`stub.late`), which is the shape Pierre's worker actually
 *  produces -- the one case below that uses it can only go green via the
 *  observer's re-run.
 *
 *  Matches are Ranges on the page-wide `CSS.highlights` entries, never elements
 *  in the bubble (domHighlight.ts); each assertion scopes to the ranges that
 *  point into its own container. */
const stub = vi.hoisted(() => ({ late: false }))

vi.mock('../src/pierre', async () => {
  const actual = await vi.importActual<typeof import('../src/pierre')>('../src/pierre')
  const { useState, useEffect } = await import('react')
  return {
    ...actual,
    PierreCode: ({ file }: { file: { contents: string } }) => {
      const [text, setText] = useState(stub.late ? '' : file.contents)
      useEffect(() => {
        if (!stub.late) return
        const t = setTimeout(() => setText(file.contents), 0)
        return () => clearTimeout(t)
      }, [file.contents])
      return <pre><code>{text}</code></pre>
    },
  }
})

const PIERRE_RENDERED = { timeout: 8_000 } as const

beforeAll(() => { installHighlightApiStub() })

function inContainer(container: HTMLElement, name: string): Range[] {
  return registeredRanges(name).filter(r => container.contains(r.startContainer))
}
function painted(container: HTMLElement, name: string): string[] {
  return inContainer(container, name).map(r => r.toString())
}
function paintedInOrder(container: HTMLElement): Array<'match' | 'current'> {
  const all = [
    ...inContainer(container, SEARCH_HL_MATCH).map(r => ({ r, kind: 'match' as const })),
    ...inContainer(container, SEARCH_HL_CURRENT).map(r => ({ r, kind: 'current' as const })),
  ]
  all.sort((a, b) => a.r.compareBoundaryPoints(Range.START_TO_START, b.r))
  return all.map(x => x.kind)
}

function renderWithSearch(code: string, lang: string, term: string, currentOcc: number) {
  const content = `\`\`\`${lang}\n${code}\n\`\`\``
  return render(
    <SearchHighlightContext.Provider value={{ term, caseSensitive: false, currentMessageIdx: currentOcc >= 0 ? 0 : -1, currentOccurrenceIdx: currentOcc }}>
      <MessageSearchScope messageIdx={0}>
        <AssistantMessage content={content} isStreaming={false} />
      </MessageSearchScope>
    </SearchHighlightContext.Provider>,
  )
}

describe('Code block search highlighting via AssistantMessage', () => {
  it('paints nothing when term is empty', () => {
    const { container } = renderWithSearch('const x = 1', 'javascript', '', -1)
    expect(paintedInOrder(container)).toEqual([])
  })

  it('paints matching text inside code blocks', async () => {
    const { container } = renderWithSearch('const hello = "world"', 'javascript', 'hello', -1)
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH)).toEqual(['hello'])
    }, PIERRE_RENDERED)
    expect(inContainer(container, SEARCH_HL_MATCH)[0].startContainer.parentElement!.closest('pre')).not.toBeNull()
    expect(container.querySelectorAll('mark')).toHaveLength(0)
  })

  it('re-runs the pass when the code block DOM lands after mount', async () => {
    // The observer's reason to exist. Nothing is paintable on the initial pass,
    // so a range can only appear because the subtree mutation re-ran the walker.
    stub.late = true
    try {
      const { container } = renderWithSearch('const hello = "world"', 'javascript', 'hello', -1)
      expect(painted(container, SEARCH_HL_MATCH)).toEqual([])
      await waitFor(() => {
        expect(painted(container, SEARCH_HL_MATCH)).toEqual(['hello'])
      }, PIERRE_RENDERED)
    } finally {
      stub.late = false
    }
  })

  it('highlights clear when term changes to empty', async () => {
    const content = '```js\nconst hello = 1\n```'
    const { container, rerender } = render(
      <SearchHighlightContext.Provider value={{ term: 'hello', caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content={content} isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH).length).toBeGreaterThan(0)
    }, PIERRE_RENDERED)
    rerender(
      <SearchHighlightContext.Provider value={{ term: '', caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content={content} isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(paintedInOrder(container)).toEqual([])
    }, PIERRE_RENDERED)
  })

  it('case-sensitive toggle works in code blocks', async () => {
    const content = '```js\nHello hello HELLO\n```'
    const { container, rerender } = render(
      <SearchHighlightContext.Provider value={{ term: 'Hello', caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content={content} isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH)).toEqual(['Hello', 'hello', 'HELLO'])
    }, PIERRE_RENDERED)
    rerender(
      <SearchHighlightContext.Provider value={{ term: 'Hello', caseSensitive: true, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content={content} isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(painted(container, SEARCH_HL_MATCH)).toEqual(['Hello'])
    }, PIERRE_RENDERED)
  })

  it('currentOcc targets specific occurrence inside code block', async () => {
    const { container } = renderWithSearch('foo foo foo', 'javascript', 'foo', 1)
    await waitFor(() => {
      expect(paintedInOrder(container)).toEqual(['match', 'current', 'match'])
    }, PIERRE_RENDERED)
  })
})
