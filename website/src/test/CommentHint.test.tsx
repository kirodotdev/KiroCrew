/**
 * The file viewer's one-time tip names the image gesture only where it works:
 * a touch press scrolls and never draws a box, so a reader with no fine
 * pointer is told about text selection alone.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { CommentHint } from '../components/MarkdownPanelBody'

const withPointer = (fine: boolean) => {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query === '(any-pointer: fine)' ? fine : false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
}

afterEach(() => vi.unstubAllGlobals())

describe('CommentHint', () => {
  it('mentions dragging across an image when a fine pointer is present', () => {
    withPointer(true)
    render(<CommentHint onDismiss={() => {}} />)
    expect(screen.getByText(/drag across an image/)).toBeInTheDocument()
  })

  it('names text selection only on a touch-only device', () => {
    withPointer(false)
    render(<CommentHint onDismiss={() => {}} />)
    expect(screen.getByText(/Select any text to add inline comments/)).toBeInTheDocument()
    expect(screen.queryByText(/drag across an image/)).toBeNull()
  })

  it('updates while shown when a mouse is connected', () => {
    let fine = false
    const listeners = new Set<() => void>()
    vi.stubGlobal('matchMedia', (query: string) => ({
      get matches() { return query === '(any-pointer: fine)' ? fine : false },
      media: query,
      addEventListener: (_: string, cb: () => void) => { listeners.add(cb) },
      removeEventListener: (_: string, cb: () => void) => { listeners.delete(cb) },
    }))
    render(<CommentHint onDismiss={() => {}} />)
    expect(screen.queryByText(/drag across an image/)).toBeNull()
    act(() => { fine = true; for (const cb of listeners) cb() })
    expect(screen.getByText(/drag across an image/)).toBeInTheDocument()
  })
})
