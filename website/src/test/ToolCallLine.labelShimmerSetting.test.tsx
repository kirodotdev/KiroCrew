/**
 * Settings -> Chat -> "Shimmer running tool labels" decides whether a running
 * tool row's label gets the moving color sweep. The spinner beside it already
 * says the call is running, so turning the sweep off must leave a plain,
 * readable label while the row is still running.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const LS_KEY = 'mc-chat-config'
const CMD = 'tail -n 50 build.log'

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => {
  localStorage.clear()
})

/** Renders one tool row that is still running (no `output` in its log entry). */
function renderRunningRow() {
  const msg: ChatMessage = { role: 'tool', content: `🔧 ${CMD}`, cls: '', meta: { tool_call_id: 'tc_run' } }
  const store = createTestStore({
    chat: {
      messages: [msg],
      toolLog: [{ type: 'tool', text: CMD, tool_call_id: 'tc_run', is_shell: true, input: CMD, ts: 1 }],
      slotRunning: true,
    } as unknown as ChatState,
  })
  renderWithProviders(<ToolCallLine message={msg} running={true} />, { store })
}

const label = () => screen.getByTestId('tool-pill-label')

describe('ToolCallLine label shimmer setting', () => {
  it('shimmers a running label by default', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: false }))
    renderRunningRow()
    expect(label().className).toContain('bg-clip-text')
  })

  it('renders a plain label while running when the setting is off', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: false, toolLabelShimmer: false }))
    renderRunningRow()
    expect(label().className).not.toContain('bg-clip-text')
    expect(label().className).toContain('text-muted')
    expect(label().style.backgroundImage).toBe('')
  })

  it('treats a stored non-boolean as the default (on)', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: false, toolLabelShimmer: 'no' }))
    renderRunningRow()
    expect(label().className).toContain('bg-clip-text')
  })
})
