/**
 * A CLAMPED tool payload still gets the Formatted/Raw rendering its toggle offers.
 *
 * `PayloadView` grew a seam branch (#12930) that returns the two halves of a
 * clamped payload verbatim around the localized marker. That branch is taken
 * BEFORE the `ToolInputText` path, so on a clamped payload the JSON whitespace
 * unescape and the JSON token highlighting are both skipped — while the
 * Formatted/Raw control above it is still rendered, because its `activeIsJson`
 * gate only inspects the head. The result is a visible control whose tooltip
 * promises "render escaped whitespace as real line breaks" and which changes
 * nothing, on exactly the oversize payloads that need it most.
 *
 * The seam branch's own reason for rendering verbatim is that neither half is a
 * whole document: the head would be drawn as a finished patch and the tail
 * starts mid-line. That argument covers the DIFF promotion, not the lexical
 * unescape — so the halves render as fragments: unescaped and highlighted,
 * never promoted to a patch card.
 */
import { describe, it, expect } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import '../i18n/all'

type ChatState = RootState['chat']

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

const ID = 'tc_clamped_input'
const MSG: ChatMessage = { role: 'tool', content: '🔧 shell deploy', cls: '', meta: { tool_call_id: ID } }

// A JSON tool input whose command value carries JSON-escaped line breaks — the
// exact shape the Formatted mode exists for. Written as a JS string with
// doubled backslashes, so the payload holds a literal backslash + 'n'.
const HEAD = '{"command":"set -e\\ncd /srv/app\\nnpm run deploy"'
const TAIL = ',"cwd":"/srv\\napp"}'
const COUNT = 84_080

/** No `output`: the panel auto-promotes to Output as soon as one exists, and
 *  this exercises the Input pane. A pending oversize call has exactly this
 *  shape — input clamped, output not back yet. */
function clampedInputStore() {
  return createTestStore({
    chat: {
      messages: [MSG],
      toolLog: [{
        type: 'tool', text: 'shell', tool_call_id: ID, ts: 1, is_shell: true,
        input: HEAD + '\n' + TAIL,
        input_cut: { at: HEAD.length + 1, count: COUNT },
      }],
      slotRunning: false,
    } as unknown as ChatState,
  })
}

const panelText = () => document.querySelector('pre')?.textContent ?? ''
const marker = () => screen.queryByTestId('tool-payload-truncated')

describe('ToolDetails — a clamped payload keeps Formatted/Raw rendering', () => {
  it('Formatted mode renders the JSON escaped line breaks as real newlines', () => {
    renderWithProviders(<ToolCallLine message={MSG} running={false} disclosure />, {
      store: clampedInputStore(),
    })
    // The marker still sits at the seam — this does not undo #12930.
    expect(marker()).not.toBeNull()
    const text = panelText()
    // Formatted is the default mode. `\n` inside the command value must have
    // become a real line break, exactly as it does on an unclamped payload.
    expect(text).toContain('set -e\ncd /srv/app\nnpm run deploy')
    expect(text).not.toContain('\\n')
  })

  it('the Raw/Formatted toggle it still offers actually changes the payload', () => {
    renderWithProviders(<ToolCallLine message={MSG} running={false} disclosure />, {
      store: clampedInputStore(),
    })
    const formatted = panelText()
    // The control is rendered (its `activeIsJson` gate reads the head, which
    // still starts with `{`), so it must not be inert.
    const rawBtn = screen.getByText('Raw')
    fireEvent.click(rawBtn)
    const raw = panelText()
    expect(raw).not.toBe(formatted)
    // Raw is byte-for-byte: the escape pairs come back.
    expect(raw).toContain('set -e\\ncd /srv/app\\nnpm run deploy')
  })

  it('still refuses to draw either half of a clamped diff as a patch card', () => {
    const dHead = '--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-old line\n+new line'
    const dTail = ' trailing context\n+another add'
    renderWithProviders(<ToolCallLine message={MSG} running={false} disclosure />, {
      store: createTestStore({
        chat: {
          messages: [MSG],
          toolLog: [{
            type: 'tool', text: 'edit', tool_call_id: ID, ts: 1,
            input: dHead + '\n' + dTail,
            input_cut: { at: dHead.length + 1, count: COUNT },
          }],
          slotRunning: false,
        } as unknown as ChatState,
      }),
    })
    expect(marker()).not.toBeNull()
    // No Pierre patch surface anywhere in the details panel.
    expect(document.querySelector('.pierre-surface')).toBeNull()
  })
})
