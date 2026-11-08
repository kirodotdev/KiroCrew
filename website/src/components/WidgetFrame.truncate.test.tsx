import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import WidgetFrame from './WidgetFrame'
import { ThemeProvider } from '../hooks/useTheme'
import { api } from '../api/client'

// The mc-widget-action handler caps the dispatched text at MAX_WIDGET_ACTION_TEXT
// with `text.slice(0, MAX) + '…'`. A UTF-16 slice through an emoji that straddles
// that cap leaves a lone high surrogate, which renders as U+FFFD. These tests pin
// that the cut lands on a whole code point instead.

/** Mirrors the module-private constant in WidgetFrame.tsx (same pattern as
 *  WidgetFrame.cov80.test.tsx's HEIGHT_SHRINK_DEBOUNCE_MS mirror). */
const MAX_WIDGET_ACTION_TEXT = 4000
const CONFETTI = '🎉' // U+1F389 — one astral char, two UTF-16 units: \uD83C\uDF89
const LONE_HIGH_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

let queryClient: QueryClient

beforeEach(() => {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  localStorage.clear()
  vi.spyOn(api, 'sandboxDocUrl').mockResolvedValue({ url: '/sandbox-doc/test/tok' })
})

afterEach(() => {
  vi.restoreAllMocks()
  queryClient.clear()
})

const wrap = (ui: ReactNode) =>
  render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{ui}</ThemeProvider>
    </QueryClientProvider>,
  )

async function frameIn(container: HTMLElement): Promise<HTMLIFrameElement> {
  return await waitFor(() => {
    const el = container.querySelector('iframe')
    if (!el) throw new Error('the widget frame never mounted an iframe')
    return el as HTMLIFrameElement
  })
}

/** Dispatch an mc-widget-action for `payload` and return the dispatched text. */
async function sendActionText(payload: Record<string, unknown>): Promise<string> {
  const { container } = wrap(<WidgetFrame html="<p>test</p>" title="T" />)
  const iframe = await frameIn(container)
  const events: CustomEvent[] = []
  const listener = (e: Event) => events.push(e as CustomEvent)
  window.addEventListener('mc-widget-send', listener)
  window.dispatchEvent(new MessageEvent('message', {
    data: { type: 'mc-widget-action', action: 'x', payload },
    source: iframe.contentWindow,
  }))
  await waitFor(() => expect(events).toHaveLength(1))
  window.removeEventListener('mc-widget-send', listener)
  return events[0].detail.text as string
}

/** Build a payload whose single string value places `CONFETTI`'s UTF-16 units at
 *  `text` indices (emojiStart, emojiStart+1), where text = `[UI] x: ` + JSON. */
function payloadStraddlingAt(emojiStart: number): Record<string, unknown> {
  // text prefix `[UI] x: ` then JSON.stringify({big: value}) = `{"big":"<value>"}`.
  const jsonValueStart = '[UI] x: '.length + '{"big":"'.length
  const fillerBeforeEmoji = emojiStart - jsonValueStart
  return { big: 'b'.repeat(fillerBeforeEmoji) + CONFETTI + 'b'.repeat(20000) }
}

describe('WidgetFrame mc-widget-action emoji truncation', () => {
  it('drops an emoji straddling the action-text cap without leaving a lone surrogate', async () => {
    // High surrogate at MAX-1, low surrogate at MAX: a UTF-16 cut at MAX splits it.
    const text = await sendActionText(payloadStraddlingAt(MAX_WIDGET_ACTION_TEXT - 1))
    expect(text.endsWith('…')).toBe(true)
    // The confetti's low surrogate at MAX is dropped with it, so the kept body
    // is MAX-1 units (filler only), never MAX-1 with a trailing lone surrogate.
    expect(text.slice(0, -1).length).toBe(MAX_WIDGET_ACTION_TEXT - 1)
    expect(text).not.toMatch(LONE_HIGH_SURROGATE)
  })

  it('keeps an emoji whole when it ends exactly at the action-text cap', async () => {
    // High surrogate at MAX-2, low surrogate at MAX-1: the pair ends at the cap.
    const text = await sendActionText(payloadStraddlingAt(MAX_WIDGET_ACTION_TEXT - 2))
    expect(text.endsWith('…')).toBe(true)
    expect(text.slice(0, -1).endsWith(CONFETTI)).toBe(true)
    expect(text).not.toMatch(LONE_HIGH_SURROGATE)
  })
})
