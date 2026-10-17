import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { api, ApiError } from '../api/client'
import type { RootState } from '../store'

/* A chat-runner permission row and a coordinator row can carry the SAME
   approval id in one slot. A bare-id 404 proves nothing is pending under that
   id, so it must settle every still-pending row with it: settling only the
   first left the other row pending, and the bar stayed up under the "this
   approval request expired" notice (main chat and crew page alike). */

vi.mock('../api/client', () => {
  class MockApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  }
  return { api: { resolveApproval: vi.fn(), approveChatSlot: vi.fn() }, ApiError: MockApiError }
})

const perm = (extra: Record<string, unknown> = {}) => ({
  role: 'permission', content: 'Running: ls', ts: '1',
  meta: { approval_id: 'ap-1', tool_call_id: 'tc-1', tool_input: '{}', ...extra },
})
const runner = perm()
const coordinator = perm({ registry: 'coordinator' })

/** `active === slot` is the main chat; otherwise the slot is a background pane
 *  (the crew page renders a crewmate DM without switching to it). */
function state(active: string, slot: string, msgs: unknown[]): Partial<RootState> {
  const onActive = active === slot
  return {
    chat: {
      activeSlot: active,
      messages: onActive ? msgs : [],
      slotMessages: onActive ? {} : { [slot]: msgs },
      toolLog: [], slotActivity: {}, slotStatusDetail: {},
    } as unknown as RootState['chat'],
    dashboard: {
      slots: [{ key: slot, messages: 2, running: true, pending_approval: true }],
      approvalMode: 'normal', connected: true, refreshTrigger: 0, unreadSlots: [],
    } as unknown as RootState['dashboard'],
  }
}

function render(active: string, slot: string, rows: unknown[]) {
  const store = createTestStore(state(active, slot, [{ role: 'user', content: 'x' }, ...rows]))
  renderWithProviders(<SlotProvider slotId={slot}><ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} /></SlotProvider>, { store })
}

beforeEach(() => vi.clearAllMocks())

const layouts: [string, string, string, unknown[]][] = [
  ['main chat, runner row first', 's1', 's1', [runner, coordinator]],
  ['main chat, coordinator row first', 's1', 's1', [coordinator, runner]],
  ['crew page pane, runner row first', 'other', 'crew', [runner, coordinator]],
  ['crew page pane, one row', 'other', 'crew', [runner]],
]

describe('approval bar with rows sharing one approval id', () => {
  for (const [name, active, slot, rows] of layouts) {
    it(`clears on a 404 (${name})`, async () => {
      vi.mocked(api.resolveApproval).mockRejectedValue(new ApiError(404, 'gone'))
      render(active, slot, rows)
      fireEvent.click(screen.getByText('Allow once'))
      await waitFor(() => expect(screen.getByText(/no longer awaiting approval/)).toBeInTheDocument())
      expect(screen.queryByText('Allow once')).toBeNull()
    })
  }
})
