/**
 * A tool pill's simplified label is the agent's PURPOSE — prose — and prose is
 * never a shell command.
 *
 * The flood-length substitution in ToolCallLine hands over-long labels to
 * deriveShellSummary, whose bare-command mode parses the string as a command
 * line and keeps the segment's head token as "the binary". Asserting that mode
 * from `isShell` alone parsed long PURPOSES on shell calls too, so a live
 * session rendered "Run the full benchmark suite and archive…" as the pill
 * "Run" (and "The …" as "The", "Prepare …" as "Prepare"). Restored-history
 * rows hardcode isShell=false, so the same row read correctly after a reload —
 * the two paths disagreed about the same call.
 *
 * The contract this pins: bare-command digestion applies only when the shown
 * label actually FELL BACK to the raw command (no purpose). A long or
 * multi-line purpose passes through intact — the collapsed row's CSS
 * `truncate` owns the overflow — while a purpose-less flood-length command
 * still digests to its binaries.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
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
  // Simplified mode: the pill prefers the agent's purpose over the raw title.
  localStorage.setItem(LS_KEY, JSON.stringify({ simplifiedToolNames: true }))
})

const CMD = `cd /tmp/kc-bench && sed -i 's/old/new/g' rig.toml && python3 - <<'EOF'\n${'x'.repeat(600)}\nEOF`

/** Over DERIVE_LABEL_THRESHOLD_CHARS (200), single line, head token "Run". */
const LONG_PURPOSE =
  'Run the full un-normalised benchmark suite against the standalone rig, ' +
  'collect per-op latency distributions for GET, RNG and PUT at the paced ' +
  'target rate, archive the raw histograms next to the run manifest, and ' +
  'compare the tails against the banked baseline from the previous round'

function toolMsg(id: string): ChatMessage {
  return { role: 'tool', content: `🔧 ${CMD}`, cls: '', meta: { tool_call_id: id } }
}

function storeFor(msg: ChatMessage, purpose?: string) {
  const id = msg.meta?.tool_call_id as string
  return createTestStore({
    chat: {
      messages: [msg],
      toolLog: [{
        type: 'tool', text: CMD, tool_call_id: id, is_shell: true,
        input: CMD, ts: 1, output: 'done',
        ...(purpose ? { purpose } : {}),
      }],
      slotRunning: false,
    } as unknown as ChatState,
  })
}

const labelText = () => screen.getByTestId('tool-pill-label').textContent

describe('ToolCallLine simplified shell labels', () => {
  it('shows a flood-length prose purpose intact instead of its first word', () => {
    expect(LONG_PURPOSE.length).toBeGreaterThan(200)
    const msg = toolMsg('tc_purpose')
    renderWithProviders(<ToolCallLine message={msg} running={false} />, {
      store: storeFor(msg, LONG_PURPOSE),
    })
    expect(labelText()).toBe(LONG_PURPOSE)
  })

  it('still digests a purpose-less flood-length command to its binaries', () => {
    const msg = toolMsg('tc_bare')
    renderWithProviders(<ToolCallLine message={msg} running={false} />, {
      store: storeFor(msg),
    })
    // cd is bookkeeping; the meaningful binaries survive, the quoting wall
    // and heredoc body do not.
    expect(labelText()).toBe('sed, python3')
  })

  it('digests when the language guard suppresses the purpose back to the raw command', () => {
    // A Han-script purpose under a Latin UI fails labelMatchesLanguage, so
    // pickToolLabel falls back to the raw title — which IS the command, and
    // must digest exactly like the purpose-less case.
    const msg = toolMsg('tc_lang')
    renderWithProviders(<ToolCallLine message={msg} running={false} />, {
      store: storeFor(msg, `运行完整的基准测试套件并归档原始直方图，${'长'.repeat(200)}`),
    })
    expect(labelText()).toBe('sed, python3')
  })
})
