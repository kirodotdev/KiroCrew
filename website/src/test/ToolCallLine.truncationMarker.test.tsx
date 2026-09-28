/**
 * The clamp marker on an oversize tool payload is rendered at VIEW time.
 *
 * `clampToolOutput` stores head + tail plus a structural seam
 * (`ToolActivity.output_cut = { at, count }`), never a rendered string. The
 * expanded details panel puts the localized "N characters truncated" line at
 * that seam, so the marker follows a runtime language switch like every other
 * string on the panel and the count carries the locale's digit grouping.
 * A reducer that baked the string in at reduce time froze it in whichever
 * language was active when the result arrived.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import i18next from 'i18next'
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

const ID = 'tc_big'
const MSG: ChatMessage = { role: 'tool', content: '🔧 shell npm test', cls: '', meta: { tool_call_id: ID } }
const HEAD = 'HEAD- case 00001 passed\nHEAD- case 00002 passed'
const TAIL = 'TAIL- case 01999 passed\nexit status: 0'
const COUNT = 84_080

function storeWith(cut: { at: number; count: number } | undefined) {
  return createTestStore({
    chat: {
      messages: [MSG],
      toolLog: [{
        type: 'tool', text: 'shell', tool_call_id: ID, ts: 1, is_shell: true,
        input: '{"command":"npm test"}',
        output: HEAD + '\n' + TAIL,
        ...(cut ? { output_cut: cut } : {}),
      }],
      slotRunning: false,
    } as unknown as ChatState,
  })
}

const marker = () => screen.queryByTestId('tool-payload-truncated')
const panelText = () => document.querySelector('pre')?.textContent ?? ''

afterEach(async () => { await i18next.changeLanguage('en') })

describe('ToolCallLine truncation marker', () => {
  it('renders the marker at the seam with a locale-grouped count, head above and tail below', () => {
    renderWithProviders(<ToolCallLine message={MSG} running={false} disclosure />, {
      store: storeWith({ at: HEAD.length + 1, count: COUNT }),
    })
    const m = marker()
    expect(m).not.toBeNull()
    expect(m!.textContent).toBe('…(84,080 characters truncated — reopen the session to see the full output)')
    // Exactly head, newline, marker, newline, tail — what the reducer stored
    // with the marker spliced in where the middle was dropped.
    expect(panelText()).toBe(`${HEAD}\n${m!.textContent}\n${TAIL}`)
  })

  it('follows a runtime language switch instead of staying in the language the result arrived in', async () => {
    renderWithProviders(<ToolCallLine message={MSG} running={false} disclosure />, {
      store: storeWith({ at: HEAD.length + 1, count: COUNT }),
    })
    expect(marker()!.textContent).toContain('characters truncated')
    await act(async () => { await i18next.changeLanguage('zh-CN') })
    expect(marker()!.textContent).toBe('…（已截断 84,080 个字符 —— 重新打开会话可查看完整输出）')
    await act(async () => { await i18next.changeLanguage('de') })
    // German groups thousands with a period.
    expect(marker()!.textContent).toContain('84.080 Zeichen gekürzt')
  })

  it('renders no marker for an unclamped payload', () => {
    renderWithProviders(<ToolCallLine message={MSG} running={false} disclosure />, {
      store: storeWith(undefined),
    })
    expect(marker()).toBeNull()
    expect(panelText()).toBe(`${HEAD}\n${TAIL}`)
  })
})
