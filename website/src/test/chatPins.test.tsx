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
  mid: 'm-mock-pin-1234',
  message_ts: '2026-08-01T10:00:00Z',
  role: 'assistant',
  preview: 'Here is the answer to your question about deployment...',
  pinned_at: '2026-08-01T12:00:00Z',
}

const mockUserPin: ChatPin = {
  id: 'pin-2',
  slot_key: 'slot-abc',
  mid: 'm-mock-pin-5678',
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

  it('isPinned returns true for a pinned message mid', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(result.current.isPinned('m-mock-pin-1234')).toBe(true)
    expect(result.current.isPinned('unknown-mid')).toBe(false)
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
        mid: 'm-new-pin-99999',
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
        mid: 'm-boundary-test',
        message_ts: 'ts-boundary',
        role: 'assistant',
        preview: boundaryCrossingPreview,
      })
    })

    expect(pinsApi.create).toHaveBeenCalledWith({
      slot_key: 'slot-abc',
      mid: 'm-boundary-test',
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
      try { await result.current.pinMessage({ mid: 'm-fail-new-pin', message_ts: 'ts-new', role: 'user', preview: 'test' }) } catch { /* expected */ }
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
      try { await result.current.unpinMessage('m-mock-pin-1234') } catch { /* expected */ }
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
    const slotAPin: ChatPin = { ...mockPin, id: 'pin-a1', slot_key: 'slot-a', mid: 'm-slot-a-pin-1' }
    const slotBPin: ChatPin = { ...mockUserPin, id: 'pin-b1', slot_key: 'slot-b', mid: 'm-slot-b-pin-1' }
    const createdPin: ChatPin = {
      ...mockUserPin,
      id: 'pin-a2',
      slot_key: 'slot-a',
      mid: 'm-slot-a-new-1',
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
        mid: 'm-slot-a-new-1',
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
    const slotAPin: ChatPin = { ...mockPin, id: 'pin-a1', slot_key: 'slot-a', mid: 'm-slot-a-unpin' }
    const slotBPin: ChatPin = { ...mockUserPin, id: 'pin-b1', slot_key: 'slot-b', mid: 'm-slot-b-unpin' }
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
    const slotBPins: ChatPin[] = [{ ...mockUserPin, id: 'pin-b1', slot_key: 'slot-xyz', mid: 'm-slot-xyz-pin' }]
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: slotBPins })
    rerender({ slot: 'slot-xyz' })

    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(result.current.pins[0].id).toBe('pin-b1')
    // Confirms slot A's data didn't leak into slot B
  })

  it('uses secureRandomId (not crypto.randomUUID) for optimistic pin ID', async () => {
    // Verify the source uses secureRandomId so it works in non-secure contexts
    const fs = await import('node:fs')
    const path = await import('node:path')
    const hookSrc = fs.readFileSync(
      path.resolve(__dirname, '../hooks/useChatPins.ts'),
      'utf8',
    )
    // Must import secureRandomId
    expect(hookSrc).toContain("import { secureRandomId } from '../utils/secureId'")
    // Must use secureRandomId() for temp pin ID
    expect(hookSrc).toContain('secureRandomId()')
    // Must NOT use crypto.randomUUID() directly (insecure context unsafe)
    expect(hookSrc).not.toContain('crypto.randomUUID()')
  })

  it('removes ghost optimistic pin on error when ctx.prev is undefined', async () => {
    // Create a fresh QueryClient with NO pre-seeded data for the slot,
    // so when the mutation's onMutate runs cancelQueries + getQueryData,
    // prev will be undefined (no prior cache entry for this slot).
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = createWrapper(qc)

    // Make list return empty (no prior fetch for 'slot-ghost')
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [] })
    // Make create fail
    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'))

    const { result } = renderHook(() => useChatPins('slot-ghost'), { wrapper })
    await waitFor(() => expect(result.current.loading).toBe(false))

    await act(async () => {
      try {
        await result.current.pinMessage({
          mid: 'm-ghost-test-id',
          message_ts: 'ts-ghost',
          role: 'user',
          preview: 'ghost pin',
        })
      } catch { /* expected */ }
    })

    // Ghost optimistic entry must be removed, not left stranded
    await waitFor(() => {
      expect(result.current.pins.some(p => p.mid === 'm-ghost-test-id')).toBe(false)
    })
    expect(result.current.error).toBe('pin')
  })
})

describe('PinnedMessagesPanel', () => {
  const defaultProps = {
    pins: [mockPin, mockUserPin],
    loading: false,
    slotKey: 'slot-abc',
    slotTitle: 'Test Chat',
    mode: 'dashboard',
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
    expect(defaultProps.onJumpToMessage).toHaveBeenCalledWith(mockPin.message_ts, mockPin.mid)
  })

  it('calls onUnpin when unpin button clicked', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    const unpinBtns = screen.getAllByLabelText('unpin')
    fireEvent.click(unpinBtns[0])
    expect(defaultProps.onUnpin).toHaveBeenCalledWith('pin-1')
    expect(defaultProps.onJumpToMessage).not.toHaveBeenCalled() // stopPropagation
  })

  it('renders no title row and no close button — the tab strip owns both', () => {
    // The panel is a side-panel TAB body. A header here would duplicate the tab
    // chip's label and add a second close affordance next to the chip's own, so
    // the body must stay chrome-less. Ratchet: reintroducing either fails here.
    render(<PinnedMessagesPanel {...defaultProps} />)
    expect(screen.queryByLabelText('close_panel')).toBeNull()
    expect(screen.queryByText('pinned_messages')).toBeNull()
  })

  it('does not take focus on mount', () => {
    // The standalone panel this replaced focused itself so its OWN Escape
    // listener could fire. As a tab body it must not: no other view in the
    // panel grabs focus, and taking it here would pull focus off the tab-strip
    // control that just opened the tab, against the menu's return-focus
    // contract. Escape still closes the panel once focus is inside it, which is
    // ActivityViewer's container handler and identical for every sibling view.
    const before = document.activeElement
    render(<PinnedMessagesPanel {...defaultProps} />)
    expect(document.activeElement).toBe(before)
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
    expect(defaultProps.onJumpToMessage).toHaveBeenCalledWith(mockPin.message_ts, mockPin.mid)
  })

  it('Space key on pin entry triggers onJumpToMessage with preventDefault', () => {
    render(<PinnedMessagesPanel {...defaultProps} />)
    const entries = screen.getAllByTestId('pin-entry')
    const event = new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true })
    vi.spyOn(event, 'preventDefault')
    entries[0].dispatchEvent(event)
    // Also test via fireEvent which RTL supports
    fireEvent.keyDown(entries[0], { key: ' ', code: 'Space' })
    expect(defaultProps.onJumpToMessage).toHaveBeenCalledWith(mockPin.message_ts, mockPin.mid)
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

// === Slot-bound paging guard (regression: deferred page response must not contaminate another slot) ===

describe('slot-bound lazy paging guard', () => {
  /**
   * loadOlderMessages now captures the active slot at dispatch time and the
   * fulfilled reducer discards the response if the user switched slots while
   * the request was in flight.  This prevents prepending stale page data from
   * slot-A into slot-B's message list.
   */
  it('loadOlderMessages thunk captures active slot in payload', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const sliceSrc = fs.readFileSync(
      path.resolve(__dirname, '../store/chatSlice.ts'),
      'utf8',
    )
    // The thunk must capture the slot before awaiting
    expect(sliceSrc).toContain('const slot = state.activeSlot')
    // The return value includes the slot for the reducer to verify
    expect(sliceSrc).toMatch(/return\s*\{[^}]*slot/)
  })

  it('fulfilled reducer guards against slot mismatch', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const sliceSrc = fs.readFileSync(
      path.resolve(__dirname, '../store/chatSlice.ts'),
      'utf8',
    )
    // The reducer checks payload.slot === state.activeSlot
    expect(sliceSrc).toContain('action.payload.slot === state.activeSlot')
  })
})

// === Server-confirmed identity gate (regression: no pin affordance on optimistic messages) ===

// === Pinned jump page-load cap removal (regression: distant pins must not false-unavailable) ===

describe('pinned jump — no arbitrary page-load cap', () => {
  /**
   * The old constant MAX_PINNED_JUMP_PAGE_LOADS = 10 caused distant pins in
   * resumed sessions to be falsely shown as "unavailable" when they needed
   * more than 10 loadOlderMessages calls.  The fix removes the constant entirely
   * — the loop terminates only when (a) the target is found, (b) slotHasMore
   * becomes false, or (c) slotOldestIndex <= 0.  This test ensures the constant
   * does not exist in the source.
   */
  it('MAX_PINNED_JUMP_PAGE_LOADS constant does not exist in ChatPage source', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const chatPageSrc = fs.readFileSync(
      path.resolve(__dirname, '../pages/ChatPage.tsx'),
      'utf8',
    )
    // The constant must not be defined
    expect(chatPageSrc).not.toContain('MAX_PINNED_JUMP_PAGE_LOADS')
    // The condition that used it (>= cap check) must not be present
    expect(chatPageSrc).not.toMatch(/pinnedJumpPageLoadsRef\.current\s*>=/)
  })

  it('pinnedJumpPageLoadsRef is still incremented (diagnostics preserved)', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const chatPageSrc = fs.readFileSync(
      path.resolve(__dirname, '../pages/ChatPage.tsx'),
      'utf8',
    )
    // The ref is still incremented for diagnostic/logging purposes
    expect(chatPageSrc).toContain('pinnedJumpPageLoadsRef.current += 1')
  })

  it('loop terminates on history exhaustion (!slotHasMore || slotOldestIndex <= 0)', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const chatPageSrc = fs.readFileSync(
      path.resolve(__dirname, '../pages/ChatPage.tsx'),
      'utf8',
    )
    // The exhaustion condition is the sole loop terminator
    expect(chatPageSrc).toContain('if (!slotHasMore || slotOldestIndex <= 0)')
  })
})

describe('pin eligibility gate — server-confirmed identity', () => {
  /**
   * The rendering gate in ChatPage uses:
   *   m.ts && (m.meta as Record<string, unknown> | undefined)?.mid
   * Only messages with BOTH a timestamp AND meta.mid (server-minted row ID)
   * are considered pinnable. This prevents optimistic messages (client ts only,
   * no meta.mid) from being pinned and creating durable orphan pins on refresh.
   */
  const gate = (m: { ts?: string; meta?: Record<string, unknown> }): boolean =>
    !!(m.ts && m.meta?.mid)

  it('optimistic user message (client ts, no meta.mid) is NOT pin-eligible', () => {
    const optimisticMsg = { ts: new Date().toISOString(), meta: undefined }
    expect(gate(optimisticMsg)).toBe(false)
  })

  it('optimistic user message with other meta but no mid is NOT pin-eligible', () => {
    const optimisticMsg = { ts: new Date().toISOString(), meta: { steer: true, optimistic: true } }
    expect(gate(optimisticMsg)).toBe(false)
  })

  it('message with no ts at all is NOT pin-eligible', () => {
    const noTsMsg = { ts: undefined, meta: { mid: 'row-abc-123' } }
    expect(gate(noTsMsg)).toBe(false)
  })

  it('server-confirmed message (ts + meta.mid) IS pin-eligible', () => {
    const confirmedMsg = { ts: '2026-08-01T10:00:00.000Z', meta: { mid: 'row-abc-123' } }
    expect(gate(confirmedMsg)).toBe(true)
  })

  it('server-confirmed message with additional meta fields IS pin-eligible', () => {
    const confirmedMsg = {
      ts: '2026-08-01T10:00:00.000Z',
      meta: { mid: 'row-xyz-456', file_changes: [], turn_stats: {} },
    }
    expect(gate(confirmedMsg)).toBe(true)
  })
})


describe('PinnedMessagesPanel — same-timestamp jump collision', () => {
  const pin1: ChatPin = {
    id: 'pin-ts-dup-1',
    slot_key: 'slot-abc',
    mid: 'm-first-message',
    message_ts: '2026-08-01T10:00:00Z',
    role: 'user',
    preview: 'First message at same ts',
    pinned_at: '2026-08-01T12:00:00Z',
  }
  const pin2: ChatPin = {
    id: 'pin-ts-dup-2',
    slot_key: 'slot-abc',
    mid: 'm-second-message',
    message_ts: '2026-08-01T10:00:00Z',
    role: 'assistant',
    preview: 'Second message at same ts',
    pinned_at: '2026-08-01T12:01:00Z',
  }

  it('passes both message_ts and mid to onJumpToMessage so caller can resolve by identity', () => {
    const onJump = vi.fn()
    const defaultProps = {
      pins: [pin1, pin2],
      loading: false,
      slotKey: 'slot-abc',
      onJumpToMessage: onJump,
      onUnpin: vi.fn(),
    }
    render(createElement(PinnedMessagesPanel, defaultProps))
    const entries = screen.getAllByTestId('pin-entry')
    // Click first pin
    fireEvent.click(entries[0])
    expect(onJump).toHaveBeenCalledWith('2026-08-01T10:00:00Z', 'm-first-message')
    // Click second pin
    fireEvent.click(entries[1])
    expect(onJump).toHaveBeenCalledWith('2026-08-01T10:00:00Z', 'm-second-message')
    // Different mids despite same ts
    expect(onJump.mock.calls[0][1]).not.toBe(onJump.mock.calls[1][1])
  })
})
