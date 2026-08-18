/**
 * Regression tests for issue #3900 — tool timer no longer inflates after
 * approve → navigate away → return. The durable anchor (execution_started_at)
 * is stamped in Redux by the approval_resolved frame and survives remount.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { sseToolActivity, sseActivityEvent, setActiveSlot, appendMessage, appendSlotMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { ChatMessage } from '../types'

function makeStore() {
  return configureStore({
    reducer: {
      chat: chatReducer,
      dashboard: dashboardReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
  })
}

describe('issue #3900 — tool timer execution_started_at persistence', () => {
  let store: ReturnType<typeof makeStore>
  const slot = 'test-slot'

  beforeEach(() => {
    store = makeStore()
    // Set active slot so tool dispatches target state.toolLog directly
    store.dispatch(setActiveSlot(slot))
  })

  it('sseToolActivity is_update updates in place without duplicating', () => {
    store.dispatch(sseToolActivity({
      slot, tool: 'shell', kind: 'execute', purpose: 'run test',
      input_preview: 'npm test', tool_call_id: 'tc-1',
    }))
    store.dispatch(sseToolActivity({
      slot, tool: 'shell', kind: 'execute', purpose: 'run test (updated)',
      input_preview: 'npm test -- --watch', tool_call_id: 'tc-1', is_update: true,
    }))

    const log = store.getState().chat.toolLog
    expect(log).toHaveLength(1)
    expect(log[0].purpose).toBe('run test (updated)')
    expect(log[0].input).toBe('npm test -- --watch')
  })

  it('approval_resolved stamps execution_started_at on the linked tool entry', () => {
    store.dispatch(sseToolActivity({
      slot, tool: 'shell', kind: 'execute', purpose: 'dangerous cmd',
      input_preview: 'rm -rf /tmp/test', tool_call_id: 'tc-2',
    }))
    // Permission message links approval_id -> tool_call_id (the exact tool)
    store.dispatch(appendMessage({
      role: 'permission', content: 'Approve?', ts: Date.now(),
      meta: { approval_id: 'ap-1', tool_call_id: 'tc-2' },
    } as unknown as ChatMessage))

    expect(store.getState().chat.toolLog[0].execution_started_at).toBeUndefined()

    store.dispatch(sseActivityEvent({
      slot, kind: 'approval_resolved', text: 'Approved', approval_id: 'ap-1',
    }))

    const entry = store.getState().chat.toolLog[0]
    expect(entry.execution_started_at).toBeGreaterThan(0)
    expect(entry.execution_started_at).toBeLessThanOrEqual(Date.now())
  })

  it('approval_resolved stamps only the tool matching the approval, not an unrelated one', () => {
    // Two non-auto tools in the log
    store.dispatch(sseToolActivity({
      slot, tool: 'shell', kind: 'execute', purpose: 'first',
      input_preview: 'echo one', tool_call_id: 'tc-a',
    }))
    store.dispatch(sseToolActivity({
      slot, tool: 'shell', kind: 'execute', purpose: 'second',
      input_preview: 'echo two', tool_call_id: 'tc-b',
    }))
    // Approval belongs to the FIRST tool
    store.dispatch(appendMessage({
      role: 'permission', content: 'Approve?', ts: Date.now(),
      meta: { approval_id: 'ap-x', tool_call_id: 'tc-a' },
    } as unknown as ChatMessage))

    store.dispatch(sseActivityEvent({
      slot, kind: 'approval_resolved', text: 'Approved', approval_id: 'ap-x',
    }))

    const log = store.getState().chat.toolLog
    const a = log.find(e => e.tool_call_id === 'tc-a')!
    const b = log.find(e => e.tool_call_id === 'tc-b')!
    expect(a.execution_started_at).toBeGreaterThan(0)
    expect(b.execution_started_at).toBeUndefined()
  })

  it('execution_started_at is not overwritten by subsequent is_update frames', () => {
    store.dispatch(sseToolActivity({
      slot, tool: 'shell', kind: 'execute', purpose: 'cmd',
      input_preview: 'echo hi', tool_call_id: 'tc-3',
    }))
    store.dispatch(appendMessage({
      role: 'permission', content: 'Approve?', ts: Date.now(),
      meta: { approval_id: 'ap-2', tool_call_id: 'tc-3' },
    } as unknown as ChatMessage))
    store.dispatch(sseActivityEvent({
      slot, kind: 'approval_resolved', text: 'Approved', approval_id: 'ap-2',
    }))

    const startedAt = store.getState().chat.toolLog[0].execution_started_at!

    store.dispatch(sseToolActivity({
      slot, tool: 'shell', kind: 'execute', purpose: 'cmd running',
      input_preview: 'echo hi (running)', tool_call_id: 'tc-3', is_update: true,
    }))

    expect(store.getState().chat.toolLog[0].execution_started_at).toBe(startedAt)
  })

  it('stamps execution_started_at for a BACKGROUND-slot approval (not just the active slot)', () => {
    const bg = 'bg-slot'
    store.dispatch(sseToolActivity({
      slot: bg, tool: 'shell', kind: 'execute', purpose: 'bg cmd',
      input_preview: 'echo bg', tool_call_id: 'tc-bg',
    }))
    store.dispatch(appendSlotMessage({
      slot: bg,
      message: {
        role: 'permission', content: 'Approve?', ts: Date.now(),
        meta: { approval_id: 'ap-bg', tool_call_id: 'tc-bg' },
      } as unknown as ChatMessage,
    }))

    store.dispatch(sseActivityEvent({
      slot: bg, kind: 'approval_resolved', text: 'Approved', approval_id: 'ap-bg',
    }))

    const entry = store.getState().chat.slotActivity[bg].toolLog.find(e => e.tool_call_id === 'tc-bg')!
    expect(entry.execution_started_at).toBeGreaterThan(0)
  })
})
