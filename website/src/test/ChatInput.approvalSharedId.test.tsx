import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { api, ApiError } from '../api/client'
import { i18nT } from '../i18n/t'
import type { RootState } from '../store'

/* A chat-runner permission row and a coordinator row can carry the SAME
   approval id in one slot. Each row names its own request (a native row by its
   slot and mid, a coordinator row by its slot and instance), so a refused press
   settles exactly the row it was sent for. Settling both on one 404 would
   withdraw the other, still-live request without anyone having decided it. */

vi.mock('../api/client', () => {
  class MockApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  }
  return { api: { decideApproval: vi.fn(), approveChatSlot: vi.fn() }, ApiError: MockApiError }
})

const gone = () => i18nT('components.approvalCard.approval_no_longer_pending')

const perm = (extra: Record<string, unknown> = {}) => ({
  role: 'permission', content: 'Running: ls', ts: '1',
  meta: { approval_id: 'ap-1', tool_call_id: 'tc-1', tool_input: '{}', ...extra },
})
/** The runner's own request: named by the slot that holds it and its row's mid. */
const runner = perm({ mid: 'mid-1' })
/** The coordinator's request under the same id: named by its instance. */
const coordinator = (slot: string) => perm({
  registry: 'coordinator',
  approval_target: { origin: 'coordinator', id: 'ap-1', slot, instance: 'inst-1' },
})

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
  return store
}

beforeEach(() => vi.clearAllMocks())

describe('approval bar with rows sharing one approval id', () => {
  const layouts: [string, string, string][] = [
    ['main chat', 's1', 's1'],
    ['crew page pane', 'other', 'crew'],
  ]

  for (const [name, active, slot] of layouts) {
    it(`a 404 on the only row clears the bar (${name})`, async () => {
      vi.mocked(api.approveChatSlot).mockRejectedValue(new ApiError(404, 'gone'))
      vi.mocked(api.decideApproval).mockRejectedValue(new ApiError(404, 'gone'))
      render(active, slot, [runner])
      fireEvent.click(screen.getByText('Allow once'))
      await waitFor(() => expect(screen.getByText(gone())).toBeInTheDocument())
      expect(screen.queryByText('Allow once')).toBeNull()
      expect(api.decideApproval).toHaveBeenCalledWith({ origin: 'native', id: 'ap-1', slot, mid: 'mid-1' }, 'approve')
    })

    for (const [order, rows] of [
      ['runner row first', [runner, coordinator(slot)]],
      ['coordinator row first', [coordinator(slot), runner]],
    ] as const) {
      it(`a 404 settles only the row it was sent for, so the other request stays decidable (${name}, ${order})`, async () => {
        vi.mocked(api.decideApproval).mockRejectedValueOnce(new ApiError(404, 'gone')).mockResolvedValue({})
        const store = render(active, slot, [...rows])
        fireEvent.click(screen.getByText('Allow once'))
        await waitFor(() => expect(screen.getByText(gone())).toBeInTheDocument())
        const first = vi.mocked(api.decideApproval).mock.calls[0][0]

        // The bar now shows the OTHER request, still live: pressing it names
        // that request's own target, never the one the refusal settled.
        const pending = (slot === active ? store.getState().chat.messages : store.getState().chat.slotMessages[slot])
          .filter((m: { role: string; meta?: { resolved?: unknown } }) => m.role === 'permission' && !m.meta?.resolved)
        expect(pending).toHaveLength(1)
        fireEvent.click(screen.getByText('Allow once'))
        await waitFor(() => expect(api.decideApproval).toHaveBeenCalledTimes(2))
        const second = vi.mocked(api.decideApproval).mock.calls[1][0]
        expect(second.origin).not.toBe(first.origin)
      })
    }
  }
})
