/**
 * A tool pill whose call a security rule refused, with an override card up for
 * it, reads as blocked rather than "Running": the command has not run, and the
 * card under it says so. Any other pending card leaves the pill's label alone.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
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

beforeEach(() => { localStorage.clear() })

const toolMsg: ChatMessage =
  { role: 'tool', content: '🔧 Running: git push origin main', cls: '', ts: 1, meta: { tool_call_id: 'tc_1' } }
const logEntry = { type: 'tool', text: 'git push origin main', tool_call_id: 'tc_1', input: 'x', ts: 1 }
const permission = (meta: Record<string, string>): ChatMessage =>
  ({ role: 'permission', content: 'Approve?', cls: '', meta: { tool_call_id: 'tc_1', approval_id: 'ap-1', ...meta } })

function renderPill(perm: ChatMessage) {
  const store = createTestStore({
    chat: { activeSlot: 'A', messages: [toolMsg, perm], toolLog: [logEntry], slotRunning: true } as unknown as ChatState,
  })
  renderWithProviders(<ToolCallLine message={toolMsg} running />, { store })
}

describe('ToolCallLine under a security-rule override card', () => {
  it('reads as waiting on the person, naming the refused command', () => {
    renderPill(permission({ deny_override_rule: 'git-publish-push-protected-branch-name', deny_override_command: 'git push origin main' }))
    expect(screen.getByText('Waiting for approval: git push origin main')).toBeInTheDocument()
    expect(screen.queryByText(/Running:/)).not.toBeInTheDocument()
  })

  it('keeps its label under an ordinary approval card', () => {
    renderPill(permission({}))
    // The pill's own label, not the "Waiting for approval" tag an ordinary card shows.
    expect(screen.queryByText(/^Waiting for approval: /)).not.toBeInTheDocument()
  })
})
