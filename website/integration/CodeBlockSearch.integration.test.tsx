import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import SearchHighlightContext, { MessageSearchScope } from '../src/hooks/SearchHighlightContext'
import AssistantMessage from '../src/pages/chat/AssistantMessage'

vi.mock('../src/utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))

/** The subject here is the search pass -- `applySearchHighlights` plus the
 *  MutationObserver that re-runs it when a code block's DOM lands late -- not
 *  Pierre's highlighter. Pierre highlights in a worker, and under a loaded
 *  coverage-instrumented shard that worker never resolved inside the test
 *  budget, so the code text (and therefore every `<mark>`) simply never
 *  appeared: the suite passed alone and failed in CI. Rendering the code
 *  synchronously keeps the whole chain under test and makes it deterministic.
 *
 *  The marks still have to arrive through the observer, so the polls below stay
 *  -- with a deadline under vitest's own `testTimeout`, or a genuine failure is
 *  reported as a timeout instead of as the assertion that broke.
 *
 *  A fully synchronous stub would however delete the observer's whole reason to
 *  exist: with the code text present on first paint, `applySearchHighlights`
 *  finds it on the initial pass and every case here would keep passing with the
 *  MutationObserver removed. So the stub can also mount EMPTY and land its text
 *  in a later task (`stub.late`), which is the shape Pierre's worker actually
 *  produces -- the one case below that uses it can only go green via the
 *  observer's re-run. */
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
  it('no <mark> elements when term is empty', () => {
    const { container } = renderWithSearch('const x = 1', 'javascript', '', -1)
    expect(container.querySelectorAll('mark.search-match, mark.search-current')).toHaveLength(0)
  })

  it('wraps matching text inside code blocks', async () => {
    const { container } = renderWithSearch('const hello = "world"', 'javascript', 'hello', -1)
    await waitFor(() => {
      const marks = container.querySelectorAll('mark.search-match')
      expect(marks).toHaveLength(1)
      expect(marks[0].textContent).toBe('hello')
    }, PIERRE_RENDERED)
  })

  it('re-runs the pass when the code block DOM lands after mount', async () => {
    // The observer's reason to exist. Nothing is markable on the initial pass,
    // so a mark can only appear because the subtree mutation re-ran the walker.
    stub.late = true
    try {
      const { container } = renderWithSearch('const hello = "world"', 'javascript', 'hello', -1)
      expect(container.querySelectorAll('mark.search-match')).toHaveLength(0)
      await waitFor(() => {
        const marks = container.querySelectorAll('mark.search-match')
        expect(marks).toHaveLength(1)
        expect(marks[0].textContent).toBe('hello')
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
      expect(container.querySelectorAll('mark.search-match').length).toBeGreaterThan(0)
    }, PIERRE_RENDERED)
    rerender(
      <SearchHighlightContext.Provider value={{ term: '', caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content={content} isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(container.querySelectorAll('mark.search-match, mark.search-current')).toHaveLength(0)
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
      expect(container.querySelectorAll('mark.search-match')).toHaveLength(3)
    }, PIERRE_RENDERED)
    rerender(
      <SearchHighlightContext.Provider value={{ term: 'Hello', caseSensitive: true, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content={content} isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(container.querySelectorAll('mark.search-match')).toHaveLength(1)
      expect(container.querySelector('mark')!.textContent).toBe('Hello')
    }, PIERRE_RENDERED)
  })

  it('currentOcc targets specific occurrence inside code block', async () => {
    const { container } = renderWithSearch('foo foo foo', 'javascript', 'foo', 1)
    await waitFor(() => {
      const marks = container.querySelectorAll('mark')
      expect(marks).toHaveLength(3)
      expect(marks[0].className).toBe('search-match')
      expect(marks[1].className).toBe('search-current')
      expect(marks[2].className).toBe('search-match')
    }, PIERRE_RENDERED)
  })
})
