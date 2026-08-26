// The broadcast bar — ported from the upstream app's own test file.
//
// What it pins: Enter and the send button both dispatch, the input clears, empty
// text is refused (an empty broadcast would still cost every agent a turn), and
// the live caption is announced to assistive technology rather than appearing
// silently.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import BroadcastBar from '../apps/meetings/components/BroadcastBar'

afterEach(cleanup)

function input() {
  return screen.getByRole('textbox') as HTMLInputElement
}

describe('BroadcastBar', () => {
  it('sends on the button and clears the field', () => {
    const onSend = vi.fn()
    render(<BroadcastBar onSend={onSend} />)
    fireEvent.change(input(), { target: { value: 'the owner is Bob' } })
    fireEvent.click(screen.getByRole('button'))
    expect(onSend).toHaveBeenCalledWith('the owner is Bob')
    expect(input().value).toBe('')
  })

  it('sends on Enter', () => {
    const onSend = vi.fn()
    render(<BroadcastBar onSend={onSend} />)
    fireEvent.change(input(), { target: { value: 'enter works' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('enter works')
  })

  it('refuses empty and whitespace-only text', () => {
    const onSend = vi.fn()
    render(<BroadcastBar onSend={onSend} />)
    fireEvent.click(screen.getByRole('button'))
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.change(input(), { target: { value: '   ' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('trims the sent text', () => {
    const onSend = vi.fn()
    render(<BroadcastBar onSend={onSend} />)
    fireEvent.change(input(), { target: { value: '  padded  ' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('padded')
  })

  it('disables the field and the button when disabled', () => {
    render(<BroadcastBar onSend={vi.fn()} disabled />)
    expect(input().disabled).toBe(true)
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('announces the live caption politely', () => {
    render(<BroadcastBar onSend={vi.fn()} caption="Alice said hello" />)
    const caption = screen.getByTestId('meetings-caption')
    expect(caption.textContent).toContain('Alice said hello')
    // A caption that changes every second must not be silent to a screen reader,
    // nor interrupt it — hence polite, not assertive.
    expect(caption.getAttribute('aria-live')).toBe('polite')
  })

  it('wraps the caption instead of clipping it to a single line', () => {
    // `truncate` carried `white-space: nowrap` plus an ellipsis, and
    // `text-overflow` shows a string's HEAD — so a caption longer than the bar
    // displayed the oldest speech and looked like it had stopped updating.
    // `line-clamp-2` bounds the height: this bar is `flex-none`, so an unbounded
    // caption would push the composer off-screen.
    //
    // Asserted by class name because the vitest env computes no layout and does
    // not load index.css — the same reasoning recorded in
    // `ChatSidebar.scrollbar.test.tsx`.
    render(<BroadcastBar onSend={vi.fn()} caption="Alice said hello" />)
    const caption = screen.getByTestId('meetings-caption')
    expect(caption.className).not.toContain('truncate')
    expect(caption.className).toContain('break-words')
    expect(caption.className).toContain('line-clamp-2')
  })

  it('renders no caption row when there is nothing to show', () => {
    render(<BroadcastBar onSend={vi.fn()} />)
    expect(screen.queryByTestId('meetings-caption')).toBeNull()
  })
})
