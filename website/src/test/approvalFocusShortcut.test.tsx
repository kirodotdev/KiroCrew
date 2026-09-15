import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.mock("@radix-ui/react-dropdown-menu", async () => await import("./__mocks__/@radix-ui/react-dropdown-menu"))
vi.mock("@radix-ui/react-popover", async () => await import("./__mocks__/@radix-ui/react-popover"))

import { screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ChatInput from '../components/ChatInput'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { queryPendingApprovalAction } from '../pages/chat/composerFocus'
import { api } from '../api/client'
import type { RootState } from '../store'

vi.mock('../api/client', () => {
  class MockApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) {
      super(message)
      this.name = 'ApiError'
      this.status = status
    }
  }
  return {
    api: {
      resolveApproval: vi.fn(() => Promise.resolve({})),
      approveChatSlot: vi.fn(() => Promise.resolve({})),
    },
    ApiError: MockApiError,
  }
})

/**
 * Alt+Shift+Enter focuses the pending tool-approval row.
 *
 * The property these tests exist to hold is NOT "the chord approves quickly" —
 * it is that the chord CANNOT approve at all. A keystroke that resolved a prompt
 * would be a one-press path to running a tool call the reader never read, so the
 * chord stops at focus and the decision keeps costing a second, deliberate press
 * on a control the user can now see is focused. `does not resolve anything` below
 * is the assertion that pins it, and mutation M6 in the PR body is what proves
 * that assertion is not decorative.
 */

const defaultProps = { value: '', onChange: vi.fn(), onSend: vi.fn() }

/** The chord, as the document-level handler sees it. */
function pressChord(): KeyboardEvent {
  const ev = new KeyboardEvent('keydown', {
    code: 'Enter', key: 'Enter', altKey: true, shiftKey: true, bubbles: true, cancelable: true,
  })
  document.dispatchEvent(ev)
  return ev
}

// ---------------------------------------------------------------- part 1: probe

/** A pane holding its own approval row, as the session grid mounts them. */
function buildPane(marker: string, labels: Array<{ text: string; disabled?: boolean }>): HTMLElement {
  const pane = document.createElement('div')
  pane.setAttribute('data-chat-pane', marker)
  const composer = document.createElement('textarea')
  composer.setAttribute('data-composer-input', '')
  pane.appendChild(composer)
  const row = document.createElement('div')
  row.setAttribute('data-approval-actions', '')
  for (const l of labels) {
    const b = document.createElement('button')
    b.textContent = l.text
    if (l.disabled) b.disabled = true
    row.appendChild(b)
  }
  pane.appendChild(row)
  document.body.appendChild(pane)
  return pane
}

describe('queryPendingApprovalAction', () => {
  afterEach(() => {
    for (const el of Array.from(document.querySelectorAll('[data-chat-pane]'))) el.remove()
  })

  it('returns null when no approval row is mounted, and non-null once one is', () => {
    // Absence is the visibility guarantee: no bar in the DOM means nothing to act
    // on. The second half is the same-axis positive control — without it a null
    // could equally mean the selector is simply wrong.
    expect(queryPendingApprovalAction()).toBeNull()
    buildPane('idle', [{ text: 'Allow once' }])
    expect(queryPendingApprovalAction()?.textContent).toBe('Allow once')
  })

  it('acts on the pane holding focus, not the first pane in document order', () => {
    buildPane('first', [{ text: 'first allow' }])
    const second = buildPane('second', [{ text: 'second allow' }])
    second.querySelector('textarea')!.focus()
    expect(queryPendingApprovalAction()?.textContent).toBe('second allow')
  })

  it('falls back to the grid-focused pane when the active element has no pane ancestor', () => {
    // A picker renders through a portal under document.body, so while one is open
    // the active element sits outside every pane and only the grid marker names
    // the pane the user is in.
    buildPane('first', [{ text: 'first allow' }])
    buildPane('focused', [{ text: 'focused allow' }])
    document.body.focus()
    expect(queryPendingApprovalAction()?.textContent).toBe('focused allow')
  })

  it('skips a disabled control and lands on the first enabled one', () => {
    // Every button in the row is disabled while a decision is in flight; landing
    // on one would put focus on something a press cannot activate.
    buildPane('idle', [{ text: 'submitting', disabled: true }, { text: 'Allow once' }])
    expect(queryPendingApprovalAction()?.textContent).toBe('Allow once')
  })

  it('does not reach into another pane when the focused pane has nothing pending', () => {
    // The document-wide branch exists for the single-pane page, which mounts no
    // [data-chat-pane] at all. Letting it also fire when a pane IS resolved would
    // focus a control belonging to a session the user is not looking at, which is
    // exactly the off-screen prompt this chord must never act on.
    const quiet = document.createElement('div')
    quiet.setAttribute('data-chat-pane', 'focused')
    const composer = document.createElement('textarea')
    quiet.appendChild(composer)
    document.body.appendChild(quiet)
    buildPane('other', [{ text: 'other pane allow' }])
    composer.focus()
    expect(queryPendingApprovalAction()).toBeNull()
  })
})

// ---------------------------------------------------------- part 2: integration

function stateWithApproval(): Partial<RootState> {
  return {
    chat: {
      activeSlot: 'slot-1',
      messages: [
        { role: 'user', content: 'list files' },
        {
          role: 'permission',
          content: 'Running: ls /tmp',
          meta: {
            approval_id: 'ap-123',
            request_id: 'req-123',
            tool_input: '{"command":"ls /tmp"}',
            is_read_only: '1',
            tool_title: 'Running: ls /tmp',
            is_shell: '1',
            full_command: 'ls /tmp',
            base_command: 'ls',
            trust_command_grantable: '1',
            trust_base_grantable: '1',
            tool_call_id: 'tc-1',
          },
        },
      ],
      toolLog: [],
      slotStatusDetail: {},
    } as unknown as RootState['chat'],
    dashboard: {
      slots: [{ key: 'slot-1', messages: 2, running: true, pending_approval: true, waiting_for_input: false, last_activity_ts: undefined }],
      approvalMode: 'normal',
      connected: true,
      channelTrusted: false,
      refreshTrigger: 0,
      unreadSlots: [],
      updateProgress: null,
    } as unknown as RootState['dashboard'],
  }
}

function stateWithoutApproval(): Partial<RootState> {
  const s = stateWithApproval()
  return {
    ...s,
    chat: { ...(s.chat as object), messages: [{ role: 'user', content: 'list files' }] } as unknown as RootState['chat'],
  }
}

/** The composer plus the root chord handler, which lives at the app root. */
function Harness() {
  useKeyboardShortcuts({ onToggleShortcutsModal: () => {}, onNewChat: () => {} })
  return <ChatInput {...defaultProps} />
}

describe('Alt+Shift+Enter focuses the pending approval', () => {
  beforeEach(() => { vi.clearAllMocks(); localStorage.clear() })

  it('lands on the row first control', () => {
    renderWithProviders(<Harness />, { store: createTestStore(stateWithApproval()) })
    const allow = screen.getByRole('button', { name: /Allow once/ })
    pressChord()
    expect(document.activeElement).toBe(allow)
  })

  it('fires from inside the composer, where the user actually is', () => {
    // Deliberately NOT gated on a focused text field. Alt+Enter carries the same
    // exemption ("works even from other inputs") and Alt+Shift+M states the
    // reason: a chord you reach for mid-sentence is dead if it bails out in the
    // composer. Safe here only because the chord moves focus and nothing else.
    renderWithProviders(<Harness />, { store: createTestStore(stateWithApproval()) })
    const composer = document.querySelector<HTMLTextAreaElement>('textarea[data-composer-input]')!
    composer.focus()
    expect(document.activeElement).toBe(composer)
    pressChord()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /Allow once/ }))
  })

  it('does not resolve anything', () => {
    // THE SAFETY PROPERTY. Focus moved; no decision was submitted by either the
    // one-shot endpoint or the slot-scoped trust endpoint.
    renderWithProviders(<Harness />, { store: createTestStore(stateWithApproval()) })
    pressChord()
    expect(api.resolveApproval).not.toHaveBeenCalled()
    expect(api.approveChatSlot).not.toHaveBeenCalled()
  })

  it('claims the chord so no newline reaches the draft', () => {
    // Claimed even though nothing else wants Alt+Shift+Enter, on the grounds the
    // Alt+digit jumps claim a dead digit: swallowing it avoids surprise typing.
    renderWithProviders(<Harness />, { store: createTestStore(stateWithApproval()) })
    expect(pressChord().defaultPrevented).toBe(true)
  })

  it('is a no-op with no approval pending', () => {
    renderWithProviders(<Harness />, { store: createTestStore(stateWithoutApproval()) })
    const before = document.activeElement
    pressChord()
    expect(document.activeElement).toBe(before)
    expect(api.resolveApproval).not.toHaveBeenCalled()
  })
})
