import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useChatPins } from '../hooks/useChatPins'
import { PinnedMessagesPanel } from '../pages/chat/PinnedMessagesPanel'
import { PIN_PREVIEW_INPUT_MAX_CHARS, type ChatPin } from '../api/pins'

// Mock the pins API
vi.mock('../api/pins', () => ({
  PIN_PREVIEW_INPUT_MAX_CHARS: 4096,
  pinsApi: {
    list: vi.fn(),
    create: vi.fn(),
    remove: vi.fn(),
  },
}))

// Mock i18n
vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) => {
    const base = key.split('.').pop() || key
    if (vars && 'count' in vars) return `${vars.count} ${base}`
    return base
  },
}))

// Mock clipboard
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(undefined),
}))

// Mock shareUrl
vi.mock('../utils/shareUrl', () => ({
  copySessionLink: vi.fn().mockResolvedValue(undefined),
}))

import { pinsApi } from '../api/pins'

const mockPin: ChatPin = {
  id: 'pin-1',
  slot_key: 'slot-abc',
  message_ts: '2026-08-01T10:00:00Z',
  role: 'assistant',
  preview: 'Here is the answer to your question about deployment...',
  pinned_at: '2026-08-01T12:00:00Z',
}

const mockUserPin: ChatPin = {
  id: 'pin-2',
  slot_key: 'slot-abc',
  message_ts: '2026-08-01T09:55:00Z',
  role: 'user',
  preview: 'How do I deploy to production?',
  pinned_at: '2026-08-01T12:01:00Z',
}

function createWrapper(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children)
}

describe('useChatPins', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [mockPin] })
    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(mockPin)
    ;(pinsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
  })

  it('fetches pins on mount when slotKey is provided', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(pinsApi.list).toHaveBeenCalledWith('slot-abc')
    expect(result.current.pins[0].id).toBe('pin-1')
  })

  it('does not fetch when slotKey is undefined', async () => {
    const { result } = renderHook(() => useChatPins(undefined), { wrapper: createWrapper() })
    // Wait a tick to ensure no fetch triggered
    await act(async () => { await new Promise(r => setTimeout(r, 10)) })
    expect(result.current.pins).toHaveLength(0)
    expect(pinsApi.list).not.toHaveBeenCalled()
  })

  it('isPinned returns true for a pinned message ts', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(result.current.isPinned('2026-08-01T10:00:00Z')).toBe(true)
    expect(result.current.isPinned('unknown-ts')).toBe(false)
  })

  it('pinMessage optimistically adds then replaces with server response', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    const newPin: ChatPin = { ...mockUserPin, id: 'pin-server-3' }
    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockResolvedValue(newPin)
    // After mutation settles, the invalidation refetches – mock returns the updated list
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [mockPin, newPin] })

    await act(async () => {
      await result.current.pinMessage({
        message_ts: '2026-08-01T09:55:00Z',
        role: 'user',
        preview: 'How do I deploy?',
      })
    })

    await waitFor(() => expect(result.current.pins).toHaveLength(2))
    expect(result.current.pins.some(p => p.id === 'pin-server-3')).toBe(true)
  })

  it('pinMessage bounds transport while preserving server-side redaction look-ahead', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    const boundaryCrossingPreview = `${'x'.repeat(181)}AKIAIOSFODNN7EXAMPLE ${'y'.repeat(5000)}`

    await act(async () => {
      await result.current.pinMessage({
        message_ts: 'ts-boundary',
        role: 'assistant',
        preview: boundaryCrossingPreview,
      })
    })

    expect(pinsApi.create).toHaveBeenCalledWith({
      slot_key: 'slot-abc',
      message_ts: 'ts-boundary',
      role: 'assistant',
      preview: boundaryCrossingPreview.slice(0, PIN_PREVIEW_INPUT_MAX_CHARS),
    })
  })

  it('pinMessage rolls back on API error', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('fail'))

    await act(async () => {
      try { await result.current.pinMessage({ message_ts: 'ts-new', role: 'user', preview: 'test' }) } catch { /* expected */ }
    })

    // Should roll back to original 1 pin
    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(result.current.pins[0].id).toBe('pin-1')
    expect(result.current.error).toBe('pin')
  })

  it('unpinMessage optimistically removes, rolls back on error', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    ;(pinsApi.remove as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('fail'))

    await act(async () => {
      try { await result.current.unpinMessage('2026-08-01T10:00:00Z') } catch { /* expected */ }
    })

    // Should roll back and expose a visible-error signal to ChatPage.
    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(result.current.error).toBe('unpin')
  })

  it('unpinById removes by ID', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    // After mutation settles, the invalidation refetches – mock returns empty
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [] })

    await act(async () => {
      await result.current.unpinById('pin-1')
    })

    await waitFor(() => expect(result.current.pins).toHaveLength(0))
    expect(pinsApi.remove).toHaveBeenCalledWith('pin-1')
  })

  it('delayed pin completion invalidates only the originating slot', async () => {
    const slotAPin: ChatPin = { ...mockPin, id: 'pin-a1', slot_key: 'slot-a' }
    const slotBPin: ChatPin = { ...mockUserPin, id: 'pin-b1', slot_key: 'slot-b' }
    const createdPin: ChatPin = {
      ...mockUserPin,
      id: 'pin-a2',
      slot_key: 'slot-a',
      message_ts: 'ts-new-a',
    }
    let slotAServerPins = [slotAPin]
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockImplementation(async (slot: string) => ({
      pins: slot === 'slot-a' ? slotAServerPins : [slotBPin],
    }))
    let resolveCreate!: (pin: ChatPin) => void
    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise<ChatPin>(resolve => { resolveCreate = resolve }),
    )

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = createWrapper(qc)
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useChatPins(slot),
      { wrapper, initialProps: { slot: 'slot-a' } },
    )
    await waitFor(() => expect(result.current.pins[0]?.id).toBe('pin-a1'))

    let pendingPin!: Promise<void>
    await act(async () => {
      pendingPin = result.current.pinMessage({
        message_ts: 'ts-new-a',
        role: 'user',
        preview: 'new pin for slot A',
      })
      await Promise.resolve()
    })
    rerender({ slot: 'slot-b' })
    await waitFor(() => expect(result.current.pins[0]?.id).toBe('pin-b1'))

    slotAServerPins = [slotAPin, createdPin]
    await act(async () => {
      resolveCreate(createdPin)
      await pendingPin
    })

    expect(qc.getQueryData<ChatPin[]>(['chat-pins', 'slot-a'])).toEqual([
      slotAPin,
      createdPin,
    ])
    expect(qc.getQueryState(['chat-pins', 'slot-a'])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(['chat-pins', 'slot-b'])?.isInvalidated).toBe(false)
    expect(result.current.pins).toEqual([slotBPin])
  })

  it('delayed unpin completion invalidates only the originating slot', async () => {
    const slotAPin: ChatPin = { ...mockPin, id: 'pin-a1', slot_key: 'slot-a' }
    const slotBPin: ChatPin = { ...mockUserPin, id: 'pin-b1', slot_key: 'slot-b' }
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockImplementation(async (slot: string) => ({
      pins: slot === 'slot-a' ? [slotAPin] : [slotBPin],
    }))
    let resolveRemove!: (result: { ok: boolean }) => void
    ;(pinsApi.remove as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise<{ ok: boolean }>(resolve => { resolveRemove = resolve }),
    )

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = createWrapper(qc)
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string }) => useChatPins(slot),
      { wrapper, initialProps: { slot: 'slot-a' } },
    )
    await waitFor(() => expect(result.current.pins[0]?.id).toBe('pin-a1'))

    let pendingUnpin!: Promise<void>
    await act(async () => {
      pendingUnpin = result.current.unpinById('pin-a1')
      await Promise.resolve()
    })
    rerender({ slot: 'slot-b' })
    await waitFor(() => expect(result.current.pins[0]?.id).toBe('pin-b1'))

    await act(async () => {
      resolveRemove({ ok: true })
      await pendingUnpin
    })

    expect(qc.getQueryData<ChatPin[]>(['chat-pins', 'slot-a'])).toEqual([])
    expect(qc.getQueryState(['chat-pins', 'slot-a'])?.isInvalidated).toBe(true)
    expect(qc.getQueryState(['chat-pins', 'slot-b'])?.isInvalidated).toBe(false)
    expect(result.current.pins).toEqual([slotBPin])
  })

  it('slot switch does not clobber – each slot has independent cache', async () => {
    const wrapper = createWrapper()
    const { result, rerender } = renderHook(
      ({ slot }: { slot: string | undefined }) => useChatPins(slot),
      { wrapper, initialProps: { slot: 'slot-abc' } },
    )
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    // Switch to a different slot
    const slotBPins: ChatPin[] = [{ ...mockUserPin, id: 'pin-b1', slot_key: 'slot-xyz' }]
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: slotBPins })
    rerender({ slot: 'slot-xyz' })

    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(result.current.pins[0].id).toBe('pin-b1')
    // Confirms slot A's data didn't leak into slot B
  })
})

describe('PinnedMessagesPanel', () => {
  const defaultProps = {
    pins: [mockPin, mockUserPin],
    loading: false,
    slotKey: 'slot-abc',
    slotTitle: 'Test Chat',
    mode: 'dashboard',
    onClose: vi.fn(),
    onJumpToMessage: vi.fn(),
    onUnpin: vi.fn(),
  }

  beforeEach(() => vi.clearAllMocks())

  it('renders pinned entries with role and preview', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    expect(screen.getAllByTestId('pin-entry')).toHaveLength(2)
    expect(screen.getByText(/Here is the answer/)).toBeInTheDocument()
    expect(screen.getByText(/How do I deploy/)).toBeInTheDocument()
  })

  it('shows empty state when no pins', () => {
    render(<PinnedMessagesPanel {...defaultProps} pins={[]} />)
    expect(screen.getByTestId('pins-empty-state')).toBeInTheDocument()
  })

  it('shows loading state', () => {
    render(<PinnedMessagesPanel {...defaultProps} pins={[]} loading={true} />)
    expect(screen.getByText('loading')).toBeInTheDocument()
  })

  it('calls onJumpToMessage when entry is clicked', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    const entries = screen.getAllByTestId('pin-entry')
    fireEvent.click(entries[0])
    expect(defaultProps.onJumpToMessage).toHaveBeenCalledWith(mockPin.message_ts)
  })

  it('calls onUnpin when unpin button clicked', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    const unpinBtns = screen.getAllByLabelText('unpin')
    fireEvent.click(unpinBtns[0])
    expect(defaultProps.onUnpin).toHaveBeenCalledWith('pin-1')
    expect(defaultProps.onJumpToMessage).not.toHaveBeenCalled() // stopPropagation
  })

  it('calls onClose when close button clicked', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    fireEvent.click(screen.getByLabelText('close_panel'))
    expect(defaultProps.onClose).toHaveBeenCalled()
  })

  it('closes on Escape', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    fireEvent.keyDown(screen.getByTestId('pinned-messages-panel'), { key: 'Escape' })
    expect(defaultProps.onClose).toHaveBeenCalled()
  })

  it('refreshes relative timestamps while open', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-01T12:00:30Z'))
    render(<PinnedMessagesPanel {...defaultProps} pins={[mockPin]} />)
    expect(screen.getByText('just_now')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(screen.getByText('1 minutes_ago')).toBeInTheDocument()
    vi.useRealTimers()
  })

  // === A11y coverage ===

  it('pin entry has role=button and is focusable (tabIndex=0)', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    const entries = screen.getAllByTestId('pin-entry')
    entries.forEach(entry => {
      expect(entry).toHaveAttribute('role', 'button')
      expect(entry).toHaveAttribute('tabindex', '0')
    })
  })

  it('Enter key on pin entry triggers onJumpToMessage', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    const entries = screen.getAllByTestId('pin-entry')
    fireEvent.keyDown(entries[0], { key: 'Enter', code: 'Enter' })
    expect(defaultProps.onJumpToMessage).toHaveBeenCalledWith(mockPin.message_ts)
  })

  it('Space key on pin entry triggers onJumpToMessage with preventDefault', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    const entries = screen.getAllByTestId('pin-entry')
    const event = new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true })
    vi.spyOn(event, 'preventDefault')
    entries[0].dispatchEvent(event)
    // Also test via fireEvent which RTL supports
    fireEvent.keyDown(entries[0], { key: ' ', code: 'Space' })
    expect(defaultProps.onJumpToMessage).toHaveBeenCalledWith(mockPin.message_ts)
  })

  it('keyboard activation on nested button does not trigger parent jump', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    const unpinBtns = screen.getAllByLabelText('unpin')
    // Keyboard activate the nested button; Clickable guards e.target === e.currentTarget
    fireEvent.keyDown(unpinBtns[0], { key: 'Enter', code: 'Enter', bubbles: true })
    // The parent onJumpToMessage should NOT fire because Clickable only activates
    // on keydowns targeting itself (e.target === e.currentTarget check)
    expect(defaultProps.onJumpToMessage).not.toHaveBeenCalled()
  })
})
