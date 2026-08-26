import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import SearchHighlightContext, { MessageSearchScope } from '../src/hooks/SearchHighlightContext'
import AssistantMessage from '../src/pages/chat/AssistantMessage'

vi.mock('../src/utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))

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
  it('no <mark> elements when term is empty', () => {
    const { container } = renderWithSearch('hello world', '', -1)
    expect(container.querySelectorAll('mark.search-match, mark.search-current')).toHaveLength(0)
  })

  it('wraps matching text in rendered paragraphs', async () => {
    const { container } = renderWithSearch('hello world', 'world', -1)
    await waitFor(() => {
      const marks = container.querySelectorAll('mark.search-match')
      expect(marks).toHaveLength(1)
      expect(marks[0].textContent).toBe('world')
    })
  })

  it('uses search-current only for the specified occurrence', async () => {
    const { container } = renderWithSearch('world hello world', 'world', 1)
    await waitFor(() => {
      const marks = container.querySelectorAll('mark')
      expect(marks).toHaveLength(2)
      expect(marks[0].className).toBe('search-match')
      expect(marks[1].className).toBe('search-current')
    })
  })

  it('highlights inside bold text', async () => {
    const { container } = renderWithSearch('**bold text** here', 'bold', -1)
    await waitFor(() => {
      expect(container.querySelectorAll('mark.search-match')).toHaveLength(1)
      expect(container.querySelector('mark')!.textContent).toBe('bold')
    })
  })

  it('highlights inside list items', async () => {
    const { container } = renderWithSearch('- item one\n- item two', 'item', -1)
    await waitFor(() => {
      expect(container.querySelectorAll('mark.search-match')).toHaveLength(2)
    })
  })

  it('single occurrence counter across markdown and code blocks', async () => {
    const content = 'The word deploy here.\n\n```python\nprint("deploy")\n```\n\nAnd deploy again.'
    const { container } = renderWithSearch(content, 'deploy', 1)
    await waitFor(() => {
      const marks = container.querySelectorAll('mark')
      expect(marks).toHaveLength(3)
      expect(marks[0].className).toBe('search-match')
      expect(marks[1].className).toBe('search-current')
      expect(marks[2].className).toBe('search-match')
    })
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
      expect(container.querySelectorAll('mark.search-match').length).toBeGreaterThan(0)
    })
    rerender(
      <SearchHighlightContext.Provider value={{ term: '', caseSensitive: false, currentMessageIdx: -1, currentOccurrenceIdx: -1 }}>
        <MessageSearchScope messageIdx={0}>
          <AssistantMessage content="hello world" isStreaming={false} />
        </MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    await waitFor(() => {
      expect(container.querySelectorAll('mark.search-match, mark.search-current')).toHaveLength(0)
    })
  })

  it('no highlights in content that does not match', () => {
    const { container } = renderWithSearch('hello world', 'xyz', -1)
    expect(container.querySelectorAll('mark.search-match, mark.search-current')).toHaveLength(0)
  })

  it('highlights inside table cells', async () => {
    const content = '| Name | Status |\n|------|--------|\n| deploy | active |\n| deploy | idle |'
    const { container } = renderWithSearch(content, 'deploy', -1)
    await waitFor(() => {
      expect(container.querySelectorAll('mark.search-match')).toHaveLength(2)
    })
  })

  it('highlights inside inline code', async () => {
    const { container } = renderWithSearch('Use the `deploy` command to run `deploy-prod`', 'deploy', -1)
    await waitFor(() => {
      expect(container.querySelectorAll('mark.search-match').length).toBeGreaterThanOrEqual(2)
    })
  })
})
