/**
 * A pending approval card of a chat whose sub-agents run inside the KAS engine shows
 * two display-only fields the gateway adds (`engine_subagent_cards.approval_card_fields`):
 * the task a waiting sub-agent dispatch was given, and the sub-agent whose call the
 * card is for. A card without them, as every other card, looks as it did.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { api } from '../api/client'
import { ALL_UNAVAILABLE } from '../api/sharedCrewUi'
import type { RootState } from '../store'

vi.mock('../api/client', () => {
  class MockApiError extends Error {
    readonly status: number
    readonly body: string
    constructor(status: number, message: string, body = '') { super(message); this.status = status; this.body = body }
  }
  return { api: {
    resolveApproval: vi.fn(), decideApproval: vi.fn(), approveChatSlot: vi.fn(),
    sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn(), sharedCrewViewer: vi.fn(), sharedCrewDecisions: vi.fn(),
  }, ApiError: MockApiError }
})

const permission = (id: string, extra: Record<string, string> = {}) => ({
  role: 'permission', content: 'Sub-agent: kirocrew-helper', ts: id,
  meta: { approval_id: id, mid: `mid-${id}`, tool_call_id: `tc-${id}`, ...extra },
})

function mount(rows: unknown[]) {
  const store = createTestStore({
    chat: { activeSlot: 'slot-1', messages: [{ role: 'user', content: 'x' }, ...rows], slotMessages: {}, toolLog: [], slotActivity: {}, slotStatusDetail: {} } as unknown as RootState['chat'],
    dashboard: { slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true }], approvalMode: 'normal', connected: true, refreshTrigger: 0, unreadSlots: [] } as unknown as RootState['dashboard'],
  })
  renderWithProviders(<SlotProvider slotId="slot-1"><ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} /></SlotProvider>, { store })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE })
})

describe('an approval card of an engine sub-agent', () => {
  it('shows the task a waiting dispatch was given, as plain text', async () => {
    mount([permission('ap-1', { subagent_task: 'Run <b>sleep 20</b>; echo one' })])
    await screen.findByText('Allow once')
    const task = screen.getByTestId('approval-subagent-task')
    expect(task).toHaveTextContent('Task: Run <b>sleep 20</b>; echo one')
    expect(task.querySelector('b')).toBeNull()
    expect(screen.queryByTestId('asked-by-subagent')).toBeNull()
  })

  it('names the sub-agent that asked, when the engine named it', async () => {
    mount([permission('ap-2', { asked_by_subagent: 'kirocrew-helper' })])
    await screen.findByText('Allow once')
    const chips = screen.getAllByTestId('asked-by-subagent')
    expect(chips.length).toBeGreaterThan(0)
    for (const chip of chips) expect(chip).toHaveTextContent(/^Asked by sub-agent kirocrew-helper$/)
    expect(screen.queryByTestId('approval-subagent-task')).toBeNull()
  })

  it('looks as it did when the gateway sent neither field', async () => {
    mount([permission('ap-3')])
    await screen.findByText('Allow once')
    expect(screen.queryByTestId('approval-subagent-task')).toBeNull()
    expect(screen.queryByTestId('asked-by-subagent')).toBeNull()
  })
})
