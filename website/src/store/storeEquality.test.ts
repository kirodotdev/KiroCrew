import { describe, it, expect, vi } from 'vitest'
import chatReducer, { setSlotStatusDetail, sseContextUsage } from './chatSlice'
import dashboardReducer, { sseSlots, sseStatus, sseTodoUpdate, sseMcpReportUpdate } from './dashboardSlice'
import type { ChatSlot, StatusData } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

/**
 * Pushed frames that repeat what the store already holds must keep the stored
 * object's identity. Selectors compare by reference, so a fresh but equal
 * object re-renders every subscriber: the session rows for a status line, the
 * approval picker for the 5 s status frame, ChatPage for a context reading.
 * Each payload is cloned so the reducer sees a new object, as from the wire.
 */
const wire = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T

const chat0 = () => chatReducer(undefined, { type: '@@init' })
const dash0 = () => dashboardReducer(undefined, { type: '@@init' })

describe('setSlotStatusDetail', () => {
  const tool = { slot: 's1', kind: 'tool' as const, purpose: 'Read the config', toolName: 'read', toolCallId: 'tc1' }

  it('keeps the map when the status repeats', () => {
    const a = chatReducer(chat0(), setSlotStatusDetail(wire(tool)))
    const b = chatReducer(a, setSlotStatusDetail(wire(tool)))
    expect(b.slotStatusDetail).toBe(a.slotStatusDetail)
  })

  it('replaces the entry when any field changes', () => {
    const a = chatReducer(chat0(), setSlotStatusDetail(wire(tool)))
    const b = chatReducer(a, setSlotStatusDetail({ ...tool, toolName: 'grep' }))
    expect(b.slotStatusDetail).not.toBe(a.slotStatusDetail)
    expect(b.slotStatusDetail.s1).toEqual({ kind: 'tool', purpose: 'Read the config', toolName: 'grep', toolCallId: 'tc1' })
    const c = chatReducer(b, setSlotStatusDetail({ slot: 's1', kind: 'thinking' }))
    expect(c.slotStatusDetail.s1).toEqual({ kind: 'thinking' })
  })

  it('stores no timestamp', () => {
    const a = chatReducer(chat0(), setSlotStatusDetail({ slot: 's1', kind: 'idle' }))
    expect(a.slotStatusDetail.s1).toEqual({ kind: 'idle' })
  })
})

describe('sseContextUsage', () => {
  const frame = { slot: 's1', pct: 12, used_tokens: 1200, window_tokens: 10000 }

  it('keeps the token counts object when the numbers repeat', () => {
    const a = chatReducer(chat0(), sseContextUsage(wire(frame)))
    const b = chatReducer(a, sseContextUsage(wire(frame)))
    expect(b.slotContextTokens.s1).toBe(a.slotContextTokens.s1)
    expect(b.slotContextTokens).toBe(a.slotContextTokens)
  })

  it('writes new counts', () => {
    const a = chatReducer(chat0(), sseContextUsage(wire(frame)))
    const b = chatReducer(a, sseContextUsage({ ...frame, used_tokens: 1300 }))
    expect(b.slotContextTokens.s1).toEqual({ used: 1300, window: 10000 })
    const c = chatReducer(b, sseContextUsage({ ...frame, used_tokens: 1300, window_tokens: 20000 }))
    expect(c.slotContextTokens.s1).toEqual({ used: 1300, window: 20000 })
  })
})

describe('sseStatus', () => {
  const status = (modes: string[], uptime: string): StatusData =>
    ({ uptime, sessions: 1, messages: 2, disabled_approval_modes: modes }) as unknown as StatusData

  it('keeps disabled_approval_modes when an equal list arrives', () => {
    const a = dashboardReducer(dash0(), sseStatus(status(['yolo'], '1m')))
    const b = dashboardReducer(a, sseStatus(status(['yolo'], '2m')))
    expect(b.status).not.toBe(a.status)
    expect(b.status?.uptime).toBe('2m')
    expect(b.status?.disabled_approval_modes).toBe(a.status?.disabled_approval_modes)
  })

  it('takes a changed list', () => {
    const a = dashboardReducer(dash0(), sseStatus(status(['yolo'], '1m')))
    const b = dashboardReducer(a, sseStatus(status(['yolo', 'reads'], '2m')))
    expect(b.status?.disabled_approval_modes).toEqual(['yolo', 'reads'])
    const c = dashboardReducer(b, sseStatus({ uptime: '3m', sessions: 1, messages: 2 } as unknown as StatusData))
    expect(c.status?.disabled_approval_modes).toBeUndefined()
  })
})

describe('sseTodoUpdate and sseMcpReportUpdate', () => {
  const slot = { key: 's1', title: 's1', messages: 1, running: true, pending_approval: false, waiting_for_input: false } as ChatSlot
  const todo = { items: [{ id: '1', content: 'write the test', status: 'pending' }] }
  const report = { servers: [{ name: 'core', status: 'ready' }] }
  const loaded = () => dashboardReducer(dash0(), sseSlots(wire([slot])))

  it('keeps the slots list when the todo list repeats', () => {
    const a = dashboardReducer(loaded(), sseTodoUpdate(wire({ slot: 's1', todo }) as never))
    const b = dashboardReducer(a, sseTodoUpdate(wire({ slot: 's1', todo }) as never))
    expect(b.slots).toBe(a.slots)
  })

  it('writes a changed todo list', () => {
    const a = dashboardReducer(loaded(), sseTodoUpdate(wire({ slot: 's1', todo }) as never))
    const next = { items: [{ id: '1', content: 'write the test', status: 'completed' }] }
    const b = dashboardReducer(a, sseTodoUpdate({ slot: 's1', todo: next } as never))
    expect(b.slots).not.toBe(a.slots)
    expect(b.slots?.[0].todo).toEqual(next)
  })

  it('keeps the slots list when the MCP report repeats, and stores a null reset', () => {
    const a = dashboardReducer(loaded(), sseMcpReportUpdate(wire({ slot: 's1', mcp_report: report }) as never))
    const b = dashboardReducer(a, sseMcpReportUpdate(wire({ slot: 's1', mcp_report: report }) as never))
    expect(b.slots).toBe(a.slots)
    const c = dashboardReducer(b, sseMcpReportUpdate({ slot: 's1', mcp_report: null }))
    expect(c.slots?.[0].mcp_report).toBeNull()
  })
})
