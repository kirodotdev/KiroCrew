/**
 * The file viewer's one-time tip names the image gesture only where it works:
 * a touch press scrolls and never draws a box, so a reader with no fine
 * pointer is told about text selection alone. A reader who closed the
 * text-only tip before box comments existed sees the image tip once more,
 * with a New badge.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { CommentHint } from '../components/MarkdownPanelBody'
import { HINT_KEY, IMAGE_HINT_KEY, useCommentHint } from '../components/useCommentHint'

const withPointer = (fine: boolean) => {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query === '(any-pointer: fine)' ? fine : false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
}

beforeEach(() => localStorage.clear())
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

describe('CommentHint', () => {
  it('mentions dragging across an image when told to', () => {
    render(<CommentHint image isNew={false} onDismiss={() => {}} />)
    expect(screen.getByText(/drag across an image/)).toBeInTheDocument()
    expect(screen.queryByText('New')).toBeNull()
  })

  it('names text selection only otherwise', () => {
    render(<CommentHint image={false} isNew={false} onDismiss={() => {}} />)
    expect(screen.getByText(/Select any text to add inline comments/)).toBeInTheDocument()
    expect(screen.queryByText(/drag across an image/)).toBeNull()
  })

  it('carries a New badge when it is back for a returning reader', () => {
    render(<CommentHint image isNew onDismiss={() => {}} />)
    expect(screen.getByText('New')).toBeInTheDocument()
  })
})

describe('useCommentHint', () => {
  it('shows the image tip, with no badge, to a reader who never closed a tip', () => {
    withPointer(true)
    const { result } = renderHook(() => useCommentHint())
    expect(result.current).toMatchObject({ show: true, image: true, isNew: false })
  })

  it('brings the image tip back once, with a badge, to a reader who closed the old one', () => {
    withPointer(true)
    localStorage.setItem(HINT_KEY, '1')
    const { result } = renderHook(() => useCommentHint())
    expect(result.current).toMatchObject({ show: true, image: true, isNew: true })
    act(() => result.current.dismiss())
    expect(result.current.show).toBe(false)
    expect(localStorage.getItem(IMAGE_HINT_KEY)).toBe('1')
    expect(renderHook(() => useCommentHint()).result.current.show).toBe(false)
  })

  it('keeps the old tip closed on a touch-only device', () => {
    withPointer(false)
    localStorage.setItem(HINT_KEY, '1')
    const { result } = renderHook(() => useCommentHint())
    expect(result.current).toMatchObject({ show: false, image: false, isNew: false })
  })

  it('still owes a touch-only reader the image tip once a mouse is connected', () => {
    withPointer(false)
    const { result } = renderHook(() => useCommentHint())
    act(() => result.current.dismiss())
    expect(localStorage.getItem(HINT_KEY)).toBe('1')
    expect(localStorage.getItem(IMAGE_HINT_KEY)).toBeNull()
    withPointer(true)
    expect(renderHook(() => useCommentHint()).result.current).toMatchObject({ show: true, isNew: true })
  })

  it('closes from its Got it button', () => {
    withPointer(true)
    function Host() {
      const hint = useCommentHint()
      return hint.show ? <CommentHint image={hint.image} isNew={hint.isNew} onDismiss={hint.dismiss} /> : null
    }
    render(<Host />)
    fireEvent.click(screen.getByText('Got it'))
    expect(screen.queryByText('Got it')).toBeNull()
  })
})
