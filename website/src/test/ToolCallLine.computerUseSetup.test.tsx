/**
 * A computer-use tool row refused for want of setup offers "Set up Computer Use".
 *
 * Driven through the replayed-row path (persisted meta, no toolLog entry), which
 * carries the same trusted fields a live row does: `meta.mcp_server`,
 * `meta.tool_name` and `meta.output`.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const REFUSAL =
  'Error: computer use is disabled. Enable it in the Kiro Crew dashboard under Settings -> Computer Use; it cannot be enabled by an agent.'

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => { localStorage.clear() })

function row(meta: Record<string, unknown>): ChatMessage {
  return {
    role: 'tool',
    content: '🔧 computer_get_state',
    cls: 'msg msg-tool',
    meta: { tool_call_id: 'tc_cu', purpose: 'Read the Notes window', done: true, ...meta },
  }
}

function renderRow(msg: ChatMessage) {
  const store = createTestStore({
    chat: { messages: [msg], toolLog: [], slotRunning: false, activeSlot: 'slot-1', mcpApps: {} } as unknown as ChatState,
  })
  renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
}

describe('ToolCallLine computer-use setup affordance', () => {
  it('offers setup under a refusal that names the setup page', () => {
    renderRow(row({ mcp_server: 'kirocrew-computer', tool_name: 'computer_get_state', output: REFUSAL }))
    expect(screen.getByTestId('computer-use-setup-link')).toBeTruthy()
  })

  it('stays out of every other row', () => {
    renderRow(row({ mcp_server: 'kirocrew-computer', tool_name: 'computer_get_state', output: 'Window "Notes" (12 elements)' }))
    expect(screen.queryByTestId('computer-use-setup-link')).toBeNull()
  })
})
