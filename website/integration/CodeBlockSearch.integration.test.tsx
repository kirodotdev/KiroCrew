import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import SearchHighlightContext, { MessageSearchScope } from '../src/hooks/SearchHighlightContext'
import AssistantMessage from '../src/pages/chat/AssistantMessage'

vi.mock('../src/utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))

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
    })
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
    })
    rerender(
      <SearchHighlightContext.Provider value={{ term: '', caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content={content} isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(container.querySelectorAll('mark.search-match, mark.search-current')).toHaveLength(0)
    })
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
    })
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
    })
  })

  it('currentOcc targets specific occurrence inside code block', async () => {
    const { container } = renderWithSearch('foo foo foo', 'javascript', 'foo', 1)
    await waitFor(() => {
      const marks = container.querySelectorAll('mark')
      expect(marks).toHaveLength(3)
      expect(marks[0].className).toBe('search-match')
      expect(marks[1].className).toBe('search-current')
      expect(marks[2].className).toBe('search-match')
    })
  })
})
