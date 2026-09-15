/**
 * A FOLDED tool row is a status line, not a payload dump.
 *
 * The transport hands us the tool title verbatim, and for a shell call that is
 * the entire command. A chained one-liner with an inline heredoc is routinely
 * several kB, and rendering it with `break-words` wrapped it to forty-odd lines
 * — one folded tool row taller than the answer it belonged to, with the reader
 * scrolling a wall of shell quoting to reach the next message.
 *
 * The contract this pins: collapsed clamps to one line (`truncate`), expanded
 * wraps (`break-words`). Nothing is lost by clamping — ToolDetails already
 * renders the verbatim input, which for a shell call IS the command.
 *
 * jsdom does no layout, so the ellipsis itself is unobservable here; the class
 * is the observable proxy, and the toggle across the click is what makes the
 * assertion load-bearing rather than a spelling test.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const LS_KEY = 'mc-chat-config'

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => {
  localStorage.clear()
  // Raw titles, so the label under test is the command rather than a purpose.
  localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: false }))
})

/** Shaped like the row in the report: a chained command carrying a heredoc. */
const LONG_CMD = `cd /tmp/kc-1429 && sed -i 's/${'a'.repeat(40)}/${'b'.repeat(40)}/g' prbody.md && python3 - <<'EOF'\n${'x'.repeat(2000)}\nEOF`

function longToolMsg(): ChatMessage {
  return { role: 'tool', content: `🔧 ${LONG_CMD}`, cls: '', meta: { tool_call_id: 'tc_long' } }
}

function storeFor(msg: ChatMessage, running: boolean) {
  return createTestStore({
    chat: {
      messages: [msg],
      // `output` is OMITTED while running, not set to '': ToolCallLine reads
      // completion as `output != null`, so an empty string would mark the call
      // done and route the label through the settled (non-shimmer) branch.
      toolLog: [{
        type: 'tool', text: LONG_CMD, tool_call_id: 'tc_long', is_shell: true,
        input: LONG_CMD, ts: 1, ...(running ? {} : { output: 'done' }),
      }],
      slotRunning: running,
    } as unknown as ChatState,
  })
}

const labelClass = () => screen.getByTestId('tool-pill-label').className

describe('ToolCallLine collapsed label clamp', () => {
  it('clamps a kB-long shell title to one line while folded, and wraps once opened', () => {
    const msg = longToolMsg()
    renderWithProviders(<ToolCallLine message={msg} running={false} />, { store: storeFor(msg, false) })

    expect(labelClass()).toContain('truncate')
    expect(labelClass()).not.toContain('break-words')

    // The whole point of clamping: the text is still THERE, just not laid out
    // across forty lines — so opening the row must reveal it, not re-fetch it.
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    expect(labelClass()).toContain('break-words')
    expect(labelClass()).not.toContain('truncate')
  })

  it('clamps the shimmering label too, so a RUNNING row cannot be the tall one', () => {
    // The running row is exactly the one the report caught, and it renders
    // through the motion.span shimmer branch — a different element from the
    // settled span, so the settled test above does not cover it.
    const msg = longToolMsg()
    renderWithProviders(<ToolCallLine message={msg} running={true} />, { store: storeFor(msg, true) })

    expect(labelClass()).toContain('truncate')
    expect(labelClass()).toContain('bg-clip-text') // proves we are on the shimmer branch
  })
})
