import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'

// ── Mocks ──

const mockDispatch = vi.fn(() => ({ unwrap: () => Promise.resolve() }))
const mockSwitchSlot = vi.fn((key: string) => ({ type: 'chat/switchSlot', payload: key }))

vi.mock('../store', () => ({
  useAppDispatch: () => mockDispatch,
}))

vi.mock('../store/chatSlice', () => ({
  switchSlot: (key: string) => mockSwitchSlot(key),
}))

vi.mock('../pages/ChatPage', () => ({
  default: (props: { embedded?: boolean }) => (
    <div data-testid="chat-page" data-embedded={props.embedded ? 'true' : 'false'}>
      ChatPage
    </div>
  ),
}))

import ChatPanel from '../app-sdk/ChatPanel'

beforeEach(() => {
  vi.restoreAllMocks()
  // vitest 4's restoreAllMocks only restores spyOn spies; it no longer clears
  // the call history of standalone vi.fn() mocks (mockDispatch/mockSwitchSlot),
  // so clear them explicitly or counts leak across tests.
  vi.clearAllMocks()
  // Re-assign after restoreAllMocks clears the implementations
  mockDispatch.mockReturnValue({ unwrap: () => Promise.resolve() })
})

describe('ChatPanel', () => {
  it('dispatches switchSlot with provided slotKey on mount', () => {
    render(<ChatPanel slotKey="my-slot-123" />)
    expect(mockSwitchSlot).toHaveBeenCalledWith('my-slot-123')
    expect(mockDispatch).toHaveBeenCalled()
  })

  it('renders ChatPage with embedded prop', () => {
    render(<ChatPanel slotKey="my-slot-123" />)
    const chatPage = screen.getByTestId('chat-page')
    expect(chatPage).toBeInTheDocument()
    expect(chatPage.getAttribute('data-embedded')).toBe('true')
  })

  it('does not re-dispatch when slotKey stays the same on rerender', () => {
    const { rerender } = render(<ChatPanel slotKey="slot-a" />)
    expect(mockSwitchSlot).toHaveBeenCalledTimes(1)

    mockSwitchSlot.mockClear()
    mockDispatch.mockClear()

    rerender(<ChatPanel slotKey="slot-a" />)
    // prevSlotRef prevents re-dispatch for the same key
    expect(mockSwitchSlot).not.toHaveBeenCalled()
    expect(mockDispatch).not.toHaveBeenCalled()
  })

  it('dispatches when slotKey changes', () => {
    const { rerender } = render(<ChatPanel slotKey="slot-a" />)
    expect(mockSwitchSlot).toHaveBeenCalledWith('slot-a')

    mockSwitchSlot.mockClear()
    mockDispatch.mockClear()

    rerender(<ChatPanel slotKey="slot-b" />)
    expect(mockSwitchSlot).toHaveBeenCalledWith('slot-b')
    expect(mockDispatch).toHaveBeenCalled()
  })

  it('does not dispatch when slotKey is empty string', () => {
    render(<ChatPanel slotKey="" />)
    // The condition `if (slotKey && slotKey !== prevSlotRef.current)` prevents dispatch for falsy keys
    expect(mockSwitchSlot).not.toHaveBeenCalled()
  })

  it('wraps ChatPage in a flex container', () => {
    const { container } = render(<ChatPanel slotKey="slot-x" />)
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper.className).toContain('flex')
    expect(wrapper.className).toContain('flex-col')
    expect(wrapper.className).toContain('h-full')
  })
})
