// Feature: chat-older-history
// The top-of-transcript affordance: unloaded history, fetch in flight, failure, explicit control.

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import EarlierMessagesBar from '../pages/chat/EarlierMessagesBar'

const bar = () => screen.getByTestId('load-earlier-messages')

describe('EarlierMessagesBar', () => {
  it('offers a control the user can act on when idle', () => {
    const onLoad = vi.fn()
    render(<EarlierMessagesBar loading={false} failed={false} onLoad={onLoad} />)
    expect(bar()).not.toBeDisabled()
    expect(bar()).toHaveAttribute('aria-busy', 'false')
    fireEvent.click(bar())
    expect(onLoad).toHaveBeenCalledTimes(1)
  })

  it('shows the fetch in flight so it is not mistaken for the start of the conversation', () => {
    render(<EarlierMessagesBar loading failed={false} onLoad={vi.fn()} />)
    expect(bar()).toHaveAttribute('aria-busy', 'true')
    expect(bar()).toHaveAttribute('aria-disabled', 'true')
  })

  // The whole point of the explicit control is the keyboard and AT users the scroll
  // trigger cannot observe, and `disabled` moves their focus to <body> on every page.
  it('marks the in-flight state without the disabled attribute, so focus is not dropped', () => {
    const { rerender } = render(<EarlierMessagesBar loading={false} failed={false} onLoad={vi.fn()} />)
    bar().focus()
    expect(document.activeElement).toBe(bar())
    rerender(<EarlierMessagesBar loading failed={false} onLoad={vi.fn()} />)
    expect(bar()).not.toBeDisabled()
    expect(bar()).not.toHaveAttribute('disabled')
    expect(document.activeElement).toBe(bar())
  })

  it('does not fire while a fetch is already in flight', () => {
    const onLoad = vi.fn()
    render(<EarlierMessagesBar loading failed={false} onLoad={onLoad} />)
    fireEvent.click(bar())
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('surfaces a failed fetch instead of snapping silently back to the idle label', () => {
    const { unmount } = render(<EarlierMessagesBar loading={false} failed={false} onLoad={vi.fn()} />)
    const idle = bar().textContent
    unmount()
    render(<EarlierMessagesBar loading={false} failed onLoad={vi.fn()} />)
    const failedLabel = bar().textContent
    expect(failedLabel).not.toBe(idle)
    expect(failedLabel?.trim()).toBeTruthy()
  })

  it('does not duplicate the sticky spinner with a loading label of its own', () => {
    const { unmount } = render(<EarlierMessagesBar loading={false} failed={false} onLoad={vi.fn()} />)
    const idle = bar().textContent
    unmount()
    render(<EarlierMessagesBar loading failed={false} onLoad={vi.fn()} />)
    // Busy is carried by aria-busy and the dimming; a second text indicator beside
    // the sticky spinner is what both review lanes flagged as one state, two spellings.
    expect(bar().textContent).toBe(idle)
    expect(bar()).toHaveAttribute('aria-busy', 'true')
  })

  it('stays actionable after a failure so the user can retry', () => {
    const onLoad = vi.fn()
    render(<EarlierMessagesBar loading={false} failed onLoad={onLoad} />)
    expect(bar()).toHaveAttribute('aria-disabled', 'false')
    fireEvent.click(bar())
    expect(onLoad).toHaveBeenCalledTimes(1)
  })

  // website/AGENTS.md mandates the shared Btn primitive over a raw <button>; this
  // asserts a class only Btn contributes, so a hand-rolled button fails here.
  it('renders through the shared Btn primitive', () => {
    render(<EarlierMessagesBar loading={false} failed={false} onLoad={vi.fn()} />)
    expect(bar().className).toContain('active:scale-[0.97]')
  })
})
