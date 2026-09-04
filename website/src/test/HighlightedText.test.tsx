import { describe, it, expect } from 'vitest'
import React from 'react'
import { render } from '@testing-library/react'
import SearchHighlightContext, { MessageSearchScope } from '../hooks/SearchHighlightContext'
import HighlightedText from '../components/HighlightedText'

function renderWithSearch(text: string, term: string, caseSensitive: boolean, currentMessageIdx: number, currentOccurrenceIdx: number, messageIdx: number) {
  return render(
    <SearchHighlightContext.Provider value={{ term, caseSensitive, currentMessageIdx, currentOccurrenceIdx }}>
      <MessageSearchScope messageIdx={messageIdx}>
        <HighlightedText text={text} />
      </MessageSearchScope>
    </SearchHighlightContext.Provider>,
  )
}

describe('HighlightedText', () => {
  it('renders plain text when no search context', () => {
    const { container } = render(<HighlightedText text="hello world" />)
    expect(container.textContent).toBe('hello world')
    expect(container.querySelectorAll('mark')).toHaveLength(0)
  })

  it('renders plain text when term is empty', () => {
    const { container } = renderWithSearch('hello world', '', false, -1, -1, 0)
    expect(container.textContent).toBe('hello world')
    expect(container.querySelectorAll('mark')).toHaveLength(0)
  })

  it('renders <mark> elements when term matches', () => {
    const { container } = renderWithSearch('hello world', 'world', false, -1, -1, 0)
    expect(container.querySelectorAll('mark')).toHaveLength(1)
    expect(container.querySelector('mark')!.textContent).toBe('world')
  })

  it('uses search-current for the specified occurrence when message is current', () => {
    const { container } = renderWithSearch('a b a', 'a', false, 5, 1, 5)
    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(2)
    expect(marks[0].className).toBe('search-match')
    expect(marks[1].className).toBe('search-current')
  })

  it('uses search-match for all when message is not current', () => {
    const { container } = renderWithSearch('a b a', 'a', false, 3, 0, 5)
    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(2)
    expect(marks[0].className).toBe('search-match')
    expect(marks[1].className).toBe('search-match')
  })

  it('uses search-current for first occurrence when currentOcc=0', () => {
    const { container } = renderWithSearch('a b a', 'a', false, 5, 0, 5)
    const marks = container.querySelectorAll('mark')
    expect(marks).toHaveLength(2)
    expect(marks[0].className).toBe('search-current')
    expect(marks[1].className).toBe('search-match')
  })
})
