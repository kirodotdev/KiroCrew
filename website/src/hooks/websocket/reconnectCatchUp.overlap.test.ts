/**
 * TWO OVERLAPPING RECONNECTS KEEP THE CATCH-UP WINDOW OPEN UNTIL THE NEWEST ONE
 * SETTLES (#34420).
 *
 * Two socket drops close together run two catch-ups. When the first one's slot
 * list read settled first it cleared `reconnectingRef` while the second socket's
 * replay was still arriving, and a replayed old turn end marked a chat unread.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { createTestStore } from '../../test/helpers'
import { api } from '../../api/client'
import type { ChatSlot } from '../../types'
import { runReconnectCatchUp, type CatchUpContext } from './reconnectCatchUp'
import type { SocketConnection } from './connection'
import type { StreamBuffers } from './streamBuffers'

afterEach(() => { vi.restoreAllMocks() })

describe('runReconnectCatchUp', () => {
  it('an older catch-up settling first leaves the newer one suppressing unread marks', async () => {
    const reads: Array<(rows: ChatSlot[]) => void> = []
    vi.spyOn(api, 'chatSlots').mockImplementation(() => new Promise<ChatSlot[]>(resolve => { reads.push(resolve) }))
    vi.spyOn(api, 'notifications').mockResolvedValue({ notifications: [], unread: 0 } as Awaited<ReturnType<typeof api.notifications>>)
    const store = createTestStore()
    const socket = {
      reconnectingRef: { current: false },
      catchUpGenerationRef: { current: 0 },
      logCbRef: { current: null },
    } as unknown as SocketConnection
    const ctx: CatchUpContext = {
      dispatch: store.dispatch,
      queryClient: new QueryClient(),
      socket,
      buffers: { dropForReconnect: vi.fn() } as unknown as StreamBuffers,
      seedAutomations: vi.fn(),
      syncPendingApprovals: vi.fn().mockResolvedValue(undefined),
      syncPendingQuestions: vi.fn().mockResolvedValue(undefined),
      syncWorkflowRuns: vi.fn().mockResolvedValue(undefined),
      focusedSlot: () => null,
    }
    const ws = () => ({ send: vi.fn() }) as unknown as WebSocket

    runReconnectCatchUp(ws(), ctx)
    runReconnectCatchUp(ws(), ctx)
    expect(reads).toHaveLength(2)
    expect(socket.reconnectingRef.current).toBe(true)

    // The first drop's read settles while the second socket is still replaying.
    reads[0]([])
    await vi.waitFor(() => expect(store.getState().dashboard.slotFetchesInFlight).toHaveLength(1))
    expect(socket.reconnectingRef.current).toBe(true)

    reads[1]([])
    await vi.waitFor(() => expect(socket.reconnectingRef.current).toBe(false))
  })
})
