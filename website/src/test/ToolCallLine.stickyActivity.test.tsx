// Feature: chat-tool-row — the shell activity line is STICKY within a turn.
//
// It appears when a shell tool starts, but completing the tool must NOT
// collapse it: the closing height ease pulsed ~26px above a bottom-pinned
// reader at every tool boundary of a working turn (text streaming was stable,
// tool execution bounced in place). The row freezes into the elapsed total
// and is reclaimed with the whole turn's collapse, so within-turn transcript
// height is monotonic at this row.

import { describe, it, expect } from 'vitest'
import { screen, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import { sseToolResult } from '../store/chatSlice'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

function shellMsg(): ChatMessage {
  return { role: 'tool', content: '🔧 Running: sleep 2 && echo done', cls: '', meta: { tool_call_id: 'tc_sticky' } }
}

describe('ToolCallLine — sticky shell activity', () => {
  it('shows the running line while the shell runs', () => {
    const store = createTestStore({
      chat: {
        messages: [shellMsg()],
        activeSlot: 'S',
        toolLog: [{ type: 'tool', text: 'sleep 2 && echo done', tool_call_id: 'tc_sticky', is_shell: true, ts: Date.now() }],
        slotRunning: true,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={shellMsg()} running={true} />, { store })
    expect(screen.getByTestId('shell-activity')).toBeTruthy()
  })

  it('keeps the line mounted after the tool completes (frozen, not collapsed)', () => {
    const store = createTestStore({
      chat: {
        messages: [shellMsg()],
        activeSlot: 'S',
        toolLog: [{ type: 'tool', text: 'sleep 2 && echo done', tool_call_id: 'tc_sticky', is_shell: true, ts: Date.now() }],
        slotRunning: true,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={shellMsg()} running={true} />, { store })
    expect(screen.getByTestId('shell-activity')).toBeTruthy()
    // Tool completes: output lands on the log entry.
    act(() => {
      store.dispatch(sseToolResult({ slot: 'S', tool_call_id: 'tc_sticky', output: 'done' }))
    })
    // The row must survive the completion -- collapsing here is the per-tool
    // bounce this design removes.
    expect(screen.queryByTestId('shell-activity')).toBeTruthy()
  })

  it('never shows the line for a tool that was ALREADY done at mount', () => {
    // A historical row (reload, scrollback) starts done: nothing was ever
    // shown, so nothing sticks -- the latch must not resurrect a status line
    // for tools that finished before this mount.
    const store = createTestStore({
      chat: {
        messages: [shellMsg()],
        activeSlot: 'S',
        toolLog: [{ type: 'tool', text: 'sleep 2 && echo done', tool_call_id: 'tc_sticky', is_shell: true, output: 'done', ts: 1 }],
        slotRunning: false,
      } as unknown as ChatState,
    })
    renderWithProviders(<ToolCallLine message={shellMsg()} running={false} />, { store })
    expect(screen.queryByTestId('shell-activity')).toBeNull()
  })
})
