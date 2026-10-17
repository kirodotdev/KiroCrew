// The four gaps reported after the running-turn window shipped, driven through
// the real ToolCallLine where the pill itself decides it is live. Each case
// failed on the tree before the one-disclosure fold.
import { describe, it, expect, beforeEach } from 'vitest'
import { createElement, useLayoutEffect, useRef } from 'react'
import { render, screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import TurnBlock from '../pages/chat/TurnBlock'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver
}
type ChatState = RootState['chat']
const folded = (el: Element | null) => !el || !!el.closest('[data-collapsed="true"],[hidden]')
const toolMsg = (id: string, ts: number): ChatMessage => ({ role: 'tool', content: `🔧 Running: echo ${id}`, cls: '', ts, meta: { tool_call_id: id } })
const log = (id: string, output?: string) => ({ type: 'tool', text: `echo ${id}`, tool_call_id: id, input: 'x', ts: 1, ...(output !== undefined ? { output } : {}) })
const turnOf = (items: TurnItem[], complete = false): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })

function pills(first: 'pending' | 'running') {
  const ids = ['tc_0', 'tc_1', 'tc_2', 'tc_3', 'tc_4', 'tc_5', 'tc_6']
  const msgs = ids.map((id, i) => toolMsg(id, i + 1))
  const perm: ChatMessage = { role: 'permission', content: 'Approve?', cls: '', meta: { tool_call_id: 'tc_0', approval_id: 'ap' } }
  const store = createTestStore({ chat: { activeSlot: 'A', slotRunning: true, messages: first === 'pending' ? [...msgs, perm] : msgs,
    toolLog: ids.map((id, i) => log(id, i === 0 ? undefined : 'out')) } as unknown as ChatState })
  const items: TurnItem[] = msgs.map((msg, i) => ({ kind: 'single', msg, idx: i }))
  renderWithProviders(<TurnBlock turn={turnOf(items)} renderItem={it => it.kind === 'single' ? <div data-testid={String(it.msg.meta?.tool_call_id)}><ToolCallLine message={it.msg} running /></div> : null} />, { store })
}

let disconnects = 0
if (!customElements.get('x-app-probe')) customElements.define('x-app-probe', class extends HTMLElement { disconnectedCallback() { disconnects++ } })
beforeEach(() => { disconnects = 0 })
const tool = (n: number): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `🔧 Running: step${n}`, ts: `t${n}` }, idx: n })
const app: TurnItem = { kind: 'single', msg: { role: 'tool', content: '🔧 Running: app', ts: 'app', meta: { tool_call_id: 'tc_app' } }, idx: 300 }
const done: TurnItem = { kind: 'single', msg: { role: 'assistant', content: 'Done.', ts: 'z' }, idx: 50 }
const body = (it: TurnItem, i: number) => it === app ? <div data-testid="app">{createElement('x-app-probe')}</div> : <div data-testid={`item-${i}`}>x</div>

describe('gap 1: pending approval mounted after reload', () => {
  it('the pending pill is not folded', () => { pills('pending'); expect(folded(screen.getByTestId('tc_0'))).toBe(false) })
})
describe('gap 2: MCP App iframe', () => {
  it('is not detached at completion', () => {
    const items = [app, ...Array.from({ length: 7 }, (_, i) => tool(i))]
    const ids = new Set(['tc_app'])
    const { rerender } = render(<TurnBlock turn={turnOf(items)} renderItem={body} appToolCallIds={ids} />)
    rerender(<TurnBlock turn={turnOf([...items, done], true)} renderItem={body} appToolCallIds={ids} />)
    expect(disconnects).toBe(0)
  })
  it('does not fold mid-run in split view (no app set passed)', () => {
    const ids = ['tc_app', 'tc_1', 'tc_2', 'tc_3', 'tc_4', 'tc_5', 'tc_6']
    const msgs = ids.map((id, i) => toolMsg(id, i + 1))
    const store = createTestStore({ chat: { activeSlot: 'A', slotRunning: true, messages: msgs,
      toolLog: ids.map(id => log(id, 'out')), mcpApps: { 'A\u001Ftc_app': { session_key: 'A', tool_call_id: 'tc_app' } } } as unknown as ChatState })
    const items: TurnItem[] = msgs.map((msg, i) => ({ kind: 'single', msg, idx: i }))
    renderWithProviders(<TurnBlock turn={turnOf(items)} renderItem={it => it.kind === 'single' ? <div data-testid={String(it.msg.meta?.tool_call_id)}><ToolCallLine message={it.msg} running appInPanel onOpenApp={() => {}} /></div> : null} />, { store })
    expect(folded(screen.getByTestId('tc_app'))).toBe(false)
  })
})
describe('gap 3: completion flash', () => {
  it('no folded row is visible in any commit at completion', () => {
    const seen: string[] = []
    function Probe({ n }: { n: number }) {
      const ref = useRef<HTMLDivElement>(null)
      useLayoutEffect(() => { if (ref.current && !folded(ref.current)) seen.push(`t${n}`) })
      return <div ref={ref} data-testid={`item-${n}`}>x</div>
    }
    const items = Array.from({ length: 40 }, (_, i) => tool(i))
    const ri = (it: TurnItem, i: number) => it.kind === 'single' && it.msg.role === 'tool' ? <Probe n={i} /> : <div>done</div>
    const { rerender } = render(<TurnBlock turn={turnOf(items)} renderItem={ri} />)
    seen.length = 0
    rerender(<TurnBlock turn={turnOf([...items, done], true)} renderItem={ri} />)
    expect(seen.filter(s => s === 't0')).toEqual([])
  })
})
describe('gap 4: a still-running tool call', () => {
  it('is not folded', () => { pills('running'); expect(folded(screen.getByTestId('tc_0'))).toBe(false) })
})
