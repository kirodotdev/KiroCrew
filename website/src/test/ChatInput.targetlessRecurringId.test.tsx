// A composer card whose row names no request (no mid) is refused locally.
// The refusal must settle THAT row: under a recurring runner id an earlier,
// already-decided row carries the same id, and settling by id would land on
// it and leave the bar's card pending, so every press repeats the refusal.
import { describe, it, expect, vi } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import type { RootState } from '../store'

vi.mock('../api/client', () => {
  class MockApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) { super(message); this.name = 'ApiError'; this.status = status }
  }
  return {
    api: {
      decideApproval: vi.fn(() => Promise.resolve({})),
      approveChatSlot: vi.fn(() => Promise.resolve({})),
    },
    ApiError: MockApiError,
  }
})

const row = (meta: Record<string, unknown>) => ({
  role: 'permission',
  content: 'Running: ls /tmp',
  meta: { approval_id: 'ap-1', tool_input: '{"command":"ls /tmp"}', tool_title: 'Running: ls /tmp', ...meta },
})

function state(): Partial<RootState> {
  return {
    chat: {
      activeSlot: 'slot-1',
      messages: [
        { role: 'user', content: 'go' },
        // An earlier request under the same runner id, already decided.
        row({ mid: 'mid-old', resolved: 'approved', tool_call_id: 'tc-old' }),
        // The bar's card: same id, no mid (names no request).
        row({ tool_call_id: 'tc-new' }),
      ],
      toolLog: [],
      slotStatusDetail: {},
    } as unknown as RootState['chat'],
    dashboard: {
      slots: [{ key: 'slot-1', messages: 3, running: true, pending_approval: true, waiting_for_input: false }],
      approvalMode: 'normal', connected: true, channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
    } as unknown as RootState['dashboard'],
  }
}

describe('a targetless composer card under a recurring id', () => {
  it('retires itself after the local refusal', async () => {
    const store = createTestStore(state())
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />, { store })
    fireEvent.click(screen.getByText('Allow once'))
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())
    expect(api.decideApproval).not.toHaveBeenCalled()
    const msgs = store.getState().chat.messages as { meta?: Record<string, unknown> }[]
    // The bar's own row is settled, and the earlier row keeps its decision.
    expect(msgs[2].meta?.resolved).toBe('stale')
    expect(msgs[1].meta?.resolved).toBe('approved')
    await waitFor(() => expect(screen.queryByText('Allow once')).not.toBeInTheDocument())
  })
})
