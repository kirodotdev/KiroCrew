import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { useChatPins } from '../hooks/useChatPins'
import { PinnedMessagesPanel } from '../pages/chat/PinnedMessagesPanel'
import { PIN_PREVIEW_INPUT_MAX_CHARS, type ChatPin } from '../api/pins'
import { ApiError } from '../api/apiError'
import { pinErrorCode } from '../hooks/useChatPins'

/** Build the `ApiError` (with a code-bearing JSON body) that pinsApi now throws
 *  via the shared transport, so `pinErrorCode` reads the code the real way. */
function pinError(status: number, code?: string): ApiError {
  const body = code ? JSON.stringify({ code }) : JSON.stringify({ error: 'boom' })
  return new ApiError(status, `HTTP ${status}`, body)
}

// Mock the pins API. The hook branches structurally on the error's `code`
// property via its own module-local pinErrorCode helper, so the mock needs no
// extra re-exports to keep the hook's error handling working.
vi.mock('../api/pins', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/pins')>()
  return {
    PIN_PREVIEW_INPUT_MAX_CHARS: actual.PIN_PREVIEW_INPUT_MAX_CHARS,
    pinsApi: {
      list: vi.fn(),
      create: vi.fn(),
      remove: vi.fn(),
    },
  }
})

// Mock i18n
vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) => {
    const base = key.split('.').pop() || key
    if (vars && 'count' in vars) return `${vars.count} ${base}`
    return base
  },
}))

// Mock clipboard — real copyToClipboard resolves `true` on success / `false`
// on a silent fallback failure (Promise<boolean>); mirror the success contract.
vi.mock('../utils/clipboard', () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

// Mock shareUrl — copySessionLink also resolves Promise<boolean>.
vi.mock('../utils/shareUrl', () => ({
  copySessionLink: vi.fn().mockResolvedValue(true),
}))

import { pinsApi } from '../api/pins'
import { copyToClipboard } from '../utils/clipboard'
import { copySessionLink } from '../utils/shareUrl'

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

  it('pinMessage sets pin_limit error when API returns 409 pin_limit_reached', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(
      pinError(409, 'pin_limit_reached'),
    )

    await act(async () => {
      try {
        await result.current.pinMessage({ mid: 'm-limit-pin', message_ts: 'ts-limit', role: 'user', preview: 'test' })
      } catch { /* expected */ }
    })

    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(result.current.error).toBe('pin_limit')
  })

  it('pinMessage sets generic pin error for non-limit API failures (e.g. 500)', async () => {
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper() })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockRejectedValue(
      pinError(500, 'persist_failed'),
    )

    await act(async () => {
      try {
        await result.current.pinMessage({ mid: 'm-server-error', message_ts: 'ts-err', role: 'user', preview: 'test' })
      } catch { /* expected */ }
    })

    await waitFor(() => expect(result.current.pins).toHaveLength(1))
    expect(result.current.error).toBe('pin')
  })

  it('pinErrorCode extracts the backend code from the ApiError body', () => {
    expect(pinErrorCode(pinError(409, 'pin_limit_reached'))).toBe('pin_limit_reached')
    // ApiError whose body carries no `code` (only an `error` message) -> undefined.
    expect(pinErrorCode(pinError(500))).toBeUndefined()
    // Structural fallback: a plain Error still carrying a string `.code` resolves.
    const legacy = new Error('x') as Error & { code?: unknown }
    legacy.code = 'preview_too_large'
    expect(pinErrorCode(legacy)).toBe('preview_too_large')
    expect(pinErrorCode(new Error('plain'))).toBeUndefined()
    expect(pinErrorCode('not an error')).toBeUndefined()
    expect(pinErrorCode(undefined)).toBeUndefined()
    // A non-string code (e.g. a Node errno number) is not a pins API code.
    const errno = new Error('x') as Error & { code?: unknown }
    errno.code = 42
    expect(pinErrorCode(errno)).toBeUndefined()
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

  // === In-flight create + unpin race (issue #5135) ===

  it('unpin during in-flight create issues NO network DELETE with a temp- id', async () => {
    // The create is pending; unpinById is called before it resolves.
    // No DELETE should ever reach the network with a temp-… id.
    let resolveCreate!: (pin: ChatPin) => void
    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise<ChatPin>(resolve => { resolveCreate = resolve }),
    )

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper(qc) })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    // Start the pin mutation (do not await — it's pending)
    let pinPromise!: Promise<void>
    act(() => {
      pinPromise = result.current.pinMessage({
        mid: 'm-inflight-mid',
        message_ts: 'ts-inflight',
        role: 'user',
        preview: 'in-flight pin',
      })
    })

    // Wait for the optimistic entry to appear in the cache
    let tempPin!: ChatPin
    await waitFor(() => {
      const found = result.current.pins.find(p => p.id.startsWith('temp-'))
      expect(found).toBeDefined()
      tempPin = found!
    })

    // Unpin immediately — create still pending
    let unpinPromise!: Promise<void>
    act(() => {
      unpinPromise = result.current.unpinById(tempPin.id)
    })

    // No DELETE should have been issued yet with a temp- id
    await waitFor(() => {
      const deleteCallsWithTempId = (pinsApi.remove as ReturnType<typeof vi.fn>)
        .mock.calls.filter(([id]: [string]) => id.startsWith('temp-'))
      expect(deleteCallsWithTempId).toHaveLength(0)
    })

    // Clean up: resolve the in-flight create so both promises settle
    const serverPin: ChatPin = { ...mockPin, id: 'pin-server-inflight', mid: 'm-inflight-mid' }
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [] })
    await act(async () => {
      resolveCreate(serverPin)
      await Promise.allSettled([pinPromise, unpinPromise])
    })
  })

  it('after create resolves the real id is deleted on the server', async () => {
    // After the in-flight create resolves, unpin should DELETE the real server id.
    let resolveCreate!: (pin: ChatPin) => void
    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise<ChatPin>(resolve => { resolveCreate = resolve }),
    )

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper(qc) })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    // Start the pin mutation (do not await — it's pending)
    let pinPromise!: Promise<void>
    act(() => {
      pinPromise = result.current.pinMessage({
        mid: 'm-resolve-mid',
        message_ts: 'ts-resolve',
        role: 'user',
        preview: 'will resolve',
      })
    })

    // Wait for the optimistic entry to appear in the cache
    let tempPin!: ChatPin
    await waitFor(() => {
      const found = result.current.pins.find(p => p.id.startsWith('temp-'))
      expect(found).toBeDefined()
      tempPin = found!
    })

    // Trigger unpin while create is still pending
    let unpinPromise!: Promise<void>
    act(() => {
      unpinPromise = result.current.unpinById(tempPin.id)
    })

    // Now resolve the create with a real server id
    const serverPin: ChatPin = { ...mockPin, id: 'pin-real-server', mid: 'm-resolve-mid' }
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [] })
    await act(async () => {
      resolveCreate(serverPin)
      await Promise.allSettled([pinPromise, unpinPromise])
    })

    // pinsApi.remove should have been called with the real server id, not the temp id
    await waitFor(() => {
      expect(pinsApi.remove).toHaveBeenCalledWith('pin-real-server')
    })
    const tempDeleteCalls = (pinsApi.remove as ReturnType<typeof vi.fn>)
      .mock.calls.filter(([id]: [string]) => id.startsWith('temp-'))
    expect(tempDeleteCalls).toHaveLength(0)
  })

  it('same mid in two slots: unpinning one slot never touches the other slot (in-flight map keyed by slot)', async () => {
    // Forked sessions can carry the SAME mid in DIFFERENT slots. Two hooks,
    // one per slot, each start a create for mid 'm-shared'. Unpinning the
    // temp pin in slot-a must await slot-a's create and delete slot-a's
    // server pin — never slot-b's.
    let resolveA!: (pin: ChatPin) => void
    let resolveB!: (pin: ChatPin) => void
    ;(pinsApi.create as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(new Promise<ChatPin>(r => { resolveA = r }))
      .mockReturnValueOnce(new Promise<ChatPin>(r => { resolveB = r }))
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [] })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const hookA = renderHook(() => useChatPins('slot-a'), { wrapper: createWrapper(qc) })
    const hookB = renderHook(() => useChatPins('slot-b'), { wrapper: createWrapper(qc) })
    await waitFor(() => expect(hookA.result.current.loading).toBe(false))
    await waitFor(() => expect(hookB.result.current.loading).toBe(false))

    const body = { mid: 'm-shared', message_ts: 'ts-x', role: 'user' as const, preview: 'p' }
    let pinA!: Promise<void>
    let pinB!: Promise<void>
    act(() => { pinA = hookA.result.current.pinMessage(body) })
    act(() => { pinB = hookB.result.current.pinMessage(body) })

    let tempA!: ChatPin
    await waitFor(() => {
      const found = hookA.result.current.pins.find(p => p.id.startsWith('temp-'))
      expect(found).toBeDefined()
      tempA = found!
    })

    // Unpin slot-a's temp pin while both creates are in flight.
    let unpinA!: Promise<void>
    act(() => { unpinA = hookA.result.current.unpinById(tempA.id) })

    // Resolve both creates: distinct server pins per slot.
    const serverA: ChatPin = { ...mockPin, id: 'pin-real-a', mid: 'm-shared', slot_key: 'slot-a' }
    const serverB: ChatPin = { ...mockPin, id: 'pin-real-b', mid: 'm-shared', slot_key: 'slot-b' }
    ;(pinsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })
    act(() => { resolveA(serverA); resolveB(serverB) })
    await act(async () => { await Promise.allSettled([pinA, pinB, unpinA]) })

    // slot-a's real pin was deleted; slot-b's was never touched.
    const removedIds = (pinsApi.remove as ReturnType<typeof vi.fn>).mock.calls.map(([id]: [string]) => id)
    expect(removedIds).toContain('pin-real-a')
    expect(removedIds).not.toContain('pin-real-b')
  })

  it('pin -> pending unpin -> pin again: the deferred delete is skipped and the re-created pin survives', async () => {
    // GPT round-2 scenario: while the unpin awaits the first create, the user
    // pins the same message again. The server create is idempotent (returns
    // the FIRST record), so the deferred DELETE would destroy the re-created
    // pin. The newer pin intent must win: no DELETE at all.
    let resolveCreate1!: (pin: ChatPin) => void
    const serverPin: ChatPin = { ...mockPin, id: 'pin-idem-1', mid: 'm-repin' }
    ;(pinsApi.create as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(new Promise<ChatPin>(r => { resolveCreate1 = r }))
      .mockResolvedValueOnce(serverPin) // idempotent second create: same record
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [] })
    ;(pinsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper(qc) })
    await waitFor(() => expect(result.current.loading).toBe(false))

    const body = { mid: 'm-repin', message_ts: 'ts-r', role: 'user' as const, preview: 'p' }
    let pin1!: Promise<void>
    act(() => { pin1 = result.current.pinMessage(body) })

    let tempPin!: ChatPin
    await waitFor(() => {
      const found = result.current.pins.find(p => p.id.startsWith('temp-'))
      expect(found).toBeDefined()
      tempPin = found!
    })

    // Unpin while create 1 is still pending.
    let unpin1!: Promise<void>
    act(() => { unpin1 = result.current.unpinById(tempPin.id) })

    // User pins the SAME message again before create 1 resolves.
    let pin2!: Promise<void>
    act(() => { pin2 = result.current.pinMessage(body) })

    // Now the first create resolves.
    act(() => { resolveCreate1(serverPin) })
    await act(async () => { await Promise.allSettled([pin1, pin2, unpin1]) })

    // The deferred delete was skipped: the re-created pin was never removed.
    expect((pinsApi.remove as ReturnType<typeof vi.fn>)).not.toHaveBeenCalled()
  })

  it('create-fails-then-unpin does not call remove and surfaces no unpin error', async () => {
    // If the create failed, there is nothing on the server.
    // Unpinning the already-gone optimistic entry must not call remove
    // and must not set error='unpin'.
    let rejectCreate!: (err: Error) => void
    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise<ChatPin>((_resolve, reject) => { rejectCreate = reject }),
    )

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useChatPins('slot-abc'), { wrapper: createWrapper(qc) })
    await waitFor(() => expect(result.current.pins).toHaveLength(1))

    // Start the pin mutation (do not await — it's pending, will be rejected later)
    let pinPromise!: Promise<void>
    act(() => {
      pinPromise = result.current.pinMessage({
        mid: 'm-fail-then-unpin',
        message_ts: 'ts-fail',
        role: 'user',
        preview: 'will fail',
      }).catch(() => { /* expected rejection */ })
    })

    // Wait for the optimistic entry to appear in the cache
    let tempPin!: ChatPin
    await waitFor(() => {
      const found = result.current.pins.find(p => p.id.startsWith('temp-'))
      expect(found).toBeDefined()
      tempPin = found!
    })

    // Trigger unpin while create is still pending (but we'll reject it next)
    let unpinPromise!: Promise<void>
    act(() => {
      unpinPromise = result.current.unpinById(tempPin.id)
    })

    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [mockPin] })
    // Reject the create — simulates a network failure
    await act(async () => {
      rejectCreate(new Error('create failed'))
      await Promise.allSettled([pinPromise, unpinPromise])
    })

    // remove must never have been called
    expect(pinsApi.remove).not.toHaveBeenCalled()
    // The error state should reflect 'pin' failure, not 'unpin'
    await waitFor(() => {
      expect(result.current.error).not.toBe('unpin')
    })
  })

  // === Coordination survives remount / second consumer (issue #5168) ===

  it('a create started by one hook instance is awaited by a DIFFERENT instance on the same QueryClient', async () => {
    // The unpin-race coordination lives on the QueryClient, not in per-instance
    // refs. Instance A starts a create and then unmounts (a remount, or a
    // second consumer of the same slot); instance B, sharing the QueryClient,
    // issues the unpin. B must still find A's in-flight promise, await it, and
    // DELETE the real server id — not fall into the "no tracked promise" branch
    // that silently drops the unpin (the pin would resurface on refetch).
    // With per-instance ref maps this test fails: B's map is empty.
    let resolveCreate!: (pin: ChatPin) => void
    ;(pinsApi.create as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise<ChatPin>(resolve => { resolveCreate = resolve }),
    )
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [] })
    ;(pinsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = createWrapper(qc)

    // Instance A starts the create, then unmounts.
    const hookA = renderHook(() => useChatPins('slot-remount'), { wrapper })
    await waitFor(() => expect(hookA.result.current.loading).toBe(false))
    let pinPromise!: Promise<void>
    act(() => {
      pinPromise = hookA.result.current.pinMessage({
        mid: 'm-remount',
        message_ts: 'ts-remount',
        role: 'user',
        preview: 'pin before remount',
      })
    })
    let tempPin!: ChatPin
    await waitFor(() => {
      const found = hookA.result.current.pins.find(p => p.id.startsWith('temp-'))
      expect(found).toBeDefined()
      tempPin = found!
    })
    hookA.unmount()

    // Instance B (fresh mount, same QueryClient, same slot) issues the unpin
    // while A's create is still in flight.
    const hookB = renderHook(() => useChatPins('slot-remount'), { wrapper })
    await waitFor(() => expect(hookB.result.current.loading).toBe(false))
    let unpinPromise!: Promise<void>
    act(() => {
      unpinPromise = hookB.result.current.unpinById(tempPin.id)
    })

    // Resolve A's create with a real server id and let both settle.
    const serverPin: ChatPin = { ...mockPin, id: 'pin-remount-real', slot_key: 'slot-remount', mid: 'm-remount' }
    await act(async () => {
      resolveCreate(serverPin)
      await Promise.allSettled([pinPromise, unpinPromise])
    })

    // B awaited A's create and deleted the REAL server id (never a temp id).
    await waitFor(() => {
      expect(pinsApi.remove).toHaveBeenCalledWith('pin-remount-real')
    })
    const tempDeletes = (pinsApi.remove as ReturnType<typeof vi.fn>)
      .mock.calls.filter(([id]: [string]) => id.startsWith('temp-'))
    expect(tempDeletes).toHaveLength(0)
  })

  it('pin intent and its deferred unpin coordinate across separate hook instances (generation survives)', async () => {
    // Pin -> pending unpin -> pin-again, but the SECOND pin comes from a
    // different hook instance on the same QueryClient (e.g. after a remount).
    // The generation bump must be visible to the deferred unpin so the newer
    // intent wins and the re-created pin is NOT deleted. With per-instance
    // generation maps the second instance's bump is invisible and the pin is
    // wrongly removed.
    let resolveCreate1!: (pin: ChatPin) => void
    const serverPin: ChatPin = { ...mockPin, id: 'pin-cross-idem', slot_key: 'slot-cross', mid: 'm-cross' }
    ;(pinsApi.create as ReturnType<typeof vi.fn>)
      .mockReturnValueOnce(new Promise<ChatPin>(r => { resolveCreate1 = r }))
      .mockResolvedValueOnce(serverPin) // idempotent second create: same record
    ;(pinsApi.list as ReturnType<typeof vi.fn>).mockResolvedValue({ pins: [] })
    ;(pinsApi.remove as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = createWrapper(qc)
    const body = { mid: 'm-cross', message_ts: 'ts-c', role: 'user' as const, preview: 'p' }

    // Instance A: pin (create 1 pending), then unpin awaiting create 1.
    const hookA = renderHook(() => useChatPins('slot-cross'), { wrapper })
    await waitFor(() => expect(hookA.result.current.loading).toBe(false))
    let pin1!: Promise<void>
    act(() => { pin1 = hookA.result.current.pinMessage(body) })
    let tempPin!: ChatPin
    await waitFor(() => {
      const found = hookA.result.current.pins.find(p => p.id.startsWith('temp-'))
      expect(found).toBeDefined()
      tempPin = found!
    })
    let unpin1!: Promise<void>
    act(() => { unpin1 = hookA.result.current.unpinById(tempPin.id) })

    // Instance B (same QueryClient) pins the SAME message again — a newer
    // intent that must supersede the deferred unpin.
    const hookB = renderHook(() => useChatPins('slot-cross'), { wrapper })
    await waitFor(() => expect(hookB.result.current.loading).toBe(false))
    let pin2!: Promise<void>
    act(() => { pin2 = hookB.result.current.pinMessage(body) })

    // Resolve create 1 and let everything settle.
    act(() => { resolveCreate1(serverPin) })
    await act(async () => { await Promise.allSettled([pin1, pin2, unpin1]) })

    // The newer pin intent won across instances: no DELETE was issued.
    expect(pinsApi.remove as ReturnType<typeof vi.fn>).not.toHaveBeenCalled()
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

  it('actions row reveals on keyboard focus (focus-within) — WCAG 2.4.7', () => {
    // The actions row must be visible when any child button receives focus so that
    // keyboard users can see and operate the controls they have tabbed onto.
    render(<PinnedMessagesPanel {...defaultProps} />)
    const actionRows = screen.getAllByTestId('pin-actions')
    // Row must carry focus-within:opacity-100 so CSS reveals it on child focus
    actionRows.forEach(row => {
      expect(row.className).toContain('focus-within:opacity-100')
    })
  })

  // === Action feedback (regression: buttons ran their side effect silently) ===
  //
  // The Copy and Copy-link buttons used to fire copyToClipboard / copySessionLink
  // with no visible confirmation, so a click looked like nothing happened — unlike
  // the in-chat message actions which swap to a green Check for 1.5s. These lock in
  // the confirmation and its per-row + per-action isolation.
  //
  // The confirmation lives on the button TITLE (tooltip) and ICON only. The
  // aria-label stays action-specific ("Copy text" / "Copy link") throughout, so a
  // screen reader can always tell the two adjacent buttons apart — swapping the
  // aria-label to "Copied" was a real a11y regression (GPT review on #7894), since
  // it made Copy text and Copy link report the same accessible name mid-confirmation.

  const titleOf = (btn: Element) => btn.getAttribute('title')

  it('Copy button shows a title confirmation after a successful copy', async () => {
    render(<PinnedMessagesPanel {...defaultProps} pins={[mockPin]} />)
    const copyBtn = screen.getByLabelText('copy_preview')
    expect(titleOf(copyBtn)).toBe('copy_preview')
    fireEvent.click(copyBtn)
    expect(copyToClipboard).toHaveBeenCalledWith(mockPin.preview)
    // Title flips to the "copied" confirmation once the promise resolves...
    await waitFor(() => expect(titleOf(copyBtn)).toBe('copied'))
    // ...but the aria-label stays action-specific so AT can still name the control.
    expect(copyBtn).toHaveAttribute('aria-label', 'copy_preview')
  })

  it('Copy-link button shows a title confirmation after a successful copy', async () => {
    render(<PinnedMessagesPanel {...defaultProps} pins={[mockPin]} />)
    const linkBtn = screen.getByLabelText('copy_link')
    fireEvent.click(linkBtn)
    expect(copySessionLink).toHaveBeenCalledWith(
      'slot-abc', 'Test Chat', mockPin.message_ts, 'dashboard', mockPin.mid,
    )
    await waitFor(() => expect(titleOf(linkBtn)).toBe('copied'))
    expect(linkBtn).toHaveAttribute('aria-label', 'copy_link')
  })

  it('confirmation is scoped to the clicked row, not every pin', async () => {
    render(<PinnedMessagesPanel {...defaultProps} pins={[mockPin, mockUserPin]} />)
    const copyBtns = screen.getAllByLabelText('copy_preview')
    expect(copyBtns).toHaveLength(2)
    fireEvent.click(copyBtns[0])
    // Only the first row's title flips; the second still shows the idle title.
    await waitFor(() => expect(titleOf(copyBtns[0])).toBe('copied'))
    expect(titleOf(copyBtns[1])).toBe('copy_preview')
    // aria-labels are unchanged on both rows.
    expect(screen.getAllByLabelText('copy_preview')).toHaveLength(2)
  })

  it('Copy and Copy-link confirmations are independent on the same row', async () => {
    render(<PinnedMessagesPanel {...defaultProps} pins={[mockPin]} />)
    const copyBtn = screen.getByLabelText('copy_preview')
    const linkBtn = screen.getByLabelText('copy_link')
    fireEvent.click(copyBtn)
    await waitFor(() => expect(titleOf(copyBtn)).toBe('copied'))
    // The link button's title is still idle — copying text did not flip the link.
    expect(titleOf(linkBtn)).toBe('copy_link')
  })

  it('does not confirm when the copy silently fails (resolves false)', async () => {
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    render(<PinnedMessagesPanel {...defaultProps} pins={[mockPin]} />)
    const copyBtn = screen.getByLabelText('copy_preview')
    fireEvent.click(copyBtn)
    await Promise.resolve()
    await Promise.resolve()
    // No confirmation because the clipboard write reported failure.
    expect(titleOf(copyBtn)).toBe('copy_preview')
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
