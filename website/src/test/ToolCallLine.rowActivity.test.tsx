/**
 * A tool pill tells its turn row when it is live, so the turn's fold never
 * hides it: running, awaiting approval, hosting an MCP App, or opened by the
 * user. The pill reports from its own state, so it holds in every host and
 * for a pill that mounts already pending (after a reload or a session switch).
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { appendMessage, resolveByApprovalId, setSlotRunning } from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import TurnBlock from '../pages/chat/TurnBlock'
import { RowActivityContext } from '../pages/chat/rowActivity'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

type ChatState = RootState['chat']

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => { localStorage.clear() })

const toolMsg = (id = 'tc_1', ts = 1): ChatMessage =>
  ({ role: 'tool', content: `🔧 Running: echo ${id}`, cls: '', ts, meta: { tool_call_id: id, purpose: `Run ${id}` } })
const logEntry = (id: string, output?: string) =>
  ({ type: 'tool', text: `echo ${id}`, tool_call_id: id, input: 'x', ts: 1, ...(output !== undefined ? { output } : {}) })
const pending = (id: string): ChatMessage =>
  ({ role: 'permission', content: 'Approve?', cls: '', meta: { tool_call_id: id, approval_id: `ap-${id}` } })

/** Live-report count for one row: reports minus undos. */
function probe() {
  let live = 0
  const report = () => { live++; return () => { live-- } }
  return { report, live: () => live }
}

function renderPill(chat: Partial<ChatState>, p = probe()) {
  const store = createTestStore({ chat: { activeSlot: 'A', ...chat } as unknown as ChatState })
  renderWithProviders(
    <RowActivityContext.Provider value={p.report}><ToolCallLine message={toolMsg()} running={!!chat.slotRunning} /></RowActivityContext.Provider>,
    { store },
  )
  return p
}

const pill = () => screen.getAllByRole('button').find(b => b.hasAttribute('aria-expanded'))!

describe('ToolCallLine reports itself live', () => {
  it('while it runs', () => {
    expect(renderPill({ messages: [toolMsg()], toolLog: [logEntry('tc_1')], slotRunning: true } as never).live()).toBe(1)
  })

  it('when it mounts with its approval already pending', () => {
    const p = renderPill({ messages: [toolMsg(), pending('tc_1')], toolLog: [logEntry('tc_1')], slotRunning: true } as never)
    expect(p.live()).toBe(1)
  })

  it('when it hosts an MCP App', () => {
    const p = renderPill({
      messages: [toolMsg()], toolLog: [], slotRunning: false,
      mcpApps: { 'A\u001Ftc_1': { session_key: 'A', tool_call_id: 'tc_1' } },
    } as never)
    expect(p.live()).toBe(1)
  })

  it('not when it is finished and closed; yes once the user opens it; not after they close it', () => {
    const p = renderPill({ messages: [toolMsg()], toolLog: [logEntry('tc_1', 'out')], slotRunning: false } as never)
    expect(p.live()).toBe(0)
    fireEvent.click(pill())
    expect(p.live()).toBe(1)
    fireEvent.click(pill())
    expect(p.live()).toBe(0)
  })

  it('not when it is open only because "Tool calls start expanded" is on', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ toolCallsStartExpanded: true }))
    const p = renderPill({ messages: [toolMsg()], toolLog: [logEntry('tc_1', 'out')], slotRunning: false } as never)
    expect(pill()).toHaveAttribute('aria-expanded', 'true')
    expect(p.live()).toBe(0)
    fireEvent.click(pill())
    fireEvent.click(pill())
    expect(p.live()).toBe(1)
  })
})

describe('an approval auto-open is not a choice', () => {
  /** Pill under a host-owned disclosure map, as ChatPage holds it. */
  function hostPill(p: ReturnType<typeof probe>, store: ReturnType<typeof createTestStore>) {
    const host: Record<string, boolean> = {}
    const ui = () => (
      <RowActivityContext.Provider value={p.report}>
        <ToolCallLine message={toolMsg()} running disclosure={host.k} disclosureKey="k" onDisclosureChange={(k, v) => { host[k] = v }} />
      </RowActivityContext.Provider>
    )
    return { host, ...renderWithProviders(ui(), { store }), ui }
  }

  for (const startExpanded of [false, true]) {
    it(`writes nothing to the host and stops being live once approved (start expanded ${startExpanded ? 'on' : 'off'})`, async () => {
      if (startExpanded) localStorage.setItem('mc-chat-config', JSON.stringify({ toolCallsStartExpanded: true }))
      const p = probe()
      const store = createTestStore({ chat: { activeSlot: 'A', slotRunning: true, messages: [toolMsg()], toolLog: [logEntry('tc_1')] } as unknown as ChatState })
      const { host, rerender, ui } = hostPill(p, store)
      act(() => { store.dispatch(appendMessage(pending('tc_1'))) })
      rerender(ui())
      expect(p.live()).toBe(1)
      act(() => { store.dispatch(resolveByApprovalId({ id: 'ap-tc_1', slot: 'A', decision: 'approved' })) })
      act(() => { store.dispatch(setSlotRunning(false)) })
      rerender(ui())
      await waitFor(() => expect(p.live()).toBe(0))
      expect(host.k).toBeUndefined()
    })
  }
})

describe('TurnBlock + ToolCallLine: a pending pill is never folded', () => {
  it('mounted pending (after a reload), with 6 newer finished calls', () => {
    const ids = ['tc_0', 'tc_1', 'tc_2', 'tc_3', 'tc_4', 'tc_5', 'tc_6']
    const msgs = ids.map((id, i) => toolMsg(id, i + 1))
    const store = createTestStore({
      chat: {
        activeSlot: 'A', slotRunning: true,
        messages: [...msgs, pending('tc_0')],
        toolLog: ids.map((id, i) => logEntry(id, i === 0 ? undefined : 'out')),
      } as unknown as ChatState,
    })
    const items: TurnItem[] = msgs.map((msg, i) => ({ kind: 'single', msg, idx: i }))
    const turn: Extract<DisplayItem, { kind: 'turn' }> = { kind: 'turn', items, complete: false }
    const renderItem = (it: TurnItem) => it.kind === 'single'
      ? <div data-testid={String(it.msg.meta?.tool_call_id)}><ToolCallLine message={it.msg} running /></div>
      : null
    renderWithProviders(<TurnBlock turn={turn} renderItem={renderItem} />, { store })
    expect(screen.getByTestId('tc_0').closest('[data-fold-row]')).not.toHaveAttribute('data-collapsed')
    expect(screen.getByRole('button', { name: /Awaiting approval/i })).toBeVisible()
    expect(screen.getByTestId('tc_1').closest('[data-fold-row]')).toHaveAttribute('data-collapsed', 'true')
    expect(screen.getByRole('button', { name: '1 tool call' })).toBeInTheDocument()
  })
})
