import { describe, it, expect } from 'vitest'
import React from 'react'
import { render } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import SearchHighlightContext, { useSearchHighlight, useMessageIdx, useCurrentOcc, MessageSearchScope } from '../hooks/SearchHighlightContext'

describe('useSearchHighlight', () => {
  it('returns default values when no provider', () => {
    const { result } = renderHook(() => useSearchHighlight())
    expect(result.current.term).toBe('')
    expect(result.current.caseSensitive).toBe(false)
    expect(result.current.currentMessageIdx).toBe(-1)
    expect(result.current.currentOccurrenceIdx).toBe(-1)
  })
})

describe('useMessageIdx', () => {
  it('returns default values when no provider', () => {
    const { result } = renderHook(() => useMessageIdx())
    expect(result.current.messageIdx).toBe(-1)
  })
})

describe('useCurrentOcc', () => {
  function wrapper(currentMessageIdx: number, currentOccurrenceIdx: number, messageIdx: number) {
    return ({ children }: { children: React.ReactNode }) => (
      <SearchHighlightContext.Provider value={{ term: 'x', caseSensitive: false, currentMessageIdx, currentOccurrenceIdx }}>
        <MessageSearchScope messageIdx={messageIdx}>{children}</MessageSearchScope>
      </SearchHighlightContext.Provider>
    )
  }

  it('returns currentOccurrenceIdx when messageIdx matches currentMessageIdx', () => {
    const { result } = renderHook(() => useCurrentOcc(), { wrapper: wrapper(3, 2, 3) })
    expect(result.current).toBe(2)
  })

  it('returns -1 when messageIdx does not match currentMessageIdx', () => {
    const { result } = renderHook(() => useCurrentOcc(), { wrapper: wrapper(3, 2, 5) })
    expect(result.current).toBe(-1)
  })

  it('returns -1 when currentMessageIdx is -1', () => {
    const { result } = renderHook(() => useCurrentOcc(), { wrapper: wrapper(-1, -1, 0) })
    expect(result.current).toBe(-1)
  })

  it('returns -1 when no providers (default context)', () => {
    const { result } = renderHook(() => useCurrentOcc())
    expect(result.current).toBe(-1)
  })
})

describe('MessageSearchScope', () => {
  function TestChild() {
    const { messageIdx } = useMessageIdx()
    return <div data-idx={messageIdx} />
  }

  it('provides messageIdx to children', () => {
    const { container } = render(
      <SearchHighlightContext.Provider value={{ term: 'x', caseSensitive: false, currentMessageIdx: 3, currentOccurrenceIdx: 0 }}>
        <MessageSearchScope messageIdx={3}><TestChild /></MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    expect(container.querySelector('div')!.getAttribute('data-idx')).toBe('3')
  })

  it('provides different messageIdx per scope', () => {
    const { container } = render(
      <SearchHighlightContext.Provider value={{ term: 'x', caseSensitive: false, currentMessageIdx: 3, currentOccurrenceIdx: 0 }}>
        <MessageSearchScope messageIdx={5}><TestChild /></MessageSearchScope>
      </SearchHighlightContext.Provider>,
    )
    expect(container.querySelector('div')!.getAttribute('data-idx')).toBe('5')
  })
})
