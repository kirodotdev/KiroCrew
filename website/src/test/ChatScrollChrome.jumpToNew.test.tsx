// Feature: the jump-to-bottom pill carries the unread count.
//
// With unseen messages below the reader the pill widens to "N new · Jump to
// latest"; with none it is the bare arrow it always was. One element in both
// states, so the reader sees a label join the pill, not a pill replaced.
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { JumpToBottomButton } from '../app-sdk/ChatScrollChrome'

describe('JumpToBottomButton unread count', () => {
  it('names the count and keeps the click', () => {
    const onClick = vi.fn()
    render(<JumpToBottomButton visible onClick={onClick} unreadCount={12} />)
    const pill = screen.getByTestId('jump-to-bottom')
    expect(pill).toHaveTextContent('12 new · Jump to latest')
    expect(pill).toHaveAccessibleName('12 new · Jump to latest')
    fireEvent.click(pill)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('is the bare arrow at zero', () => {
    render(<JumpToBottomButton visible onClick={() => {}} unreadCount={0} />)
    const pill = screen.getByTestId('jump-to-bottom')
    expect(pill).toHaveAccessibleName('Scroll to bottom')
    expect(pill.textContent).toBe('')
  })

  it('renders nothing while hidden, whatever the count', () => {
    const { container } = render(<JumpToBottomButton visible={false} onClick={() => {}} unreadCount={3} />)
    expect(container).toBeEmptyDOMElement()
  })
})
