import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useSelectionComposerAnchor } from './useSelectionComposerAnchor'

type Anchor = { quote: string }

// A lone high surrogate with no trailing low surrogate — the broken half an
// emoji leaves behind when a UTF-16 slice cuts through it.
const LONE_HIGH = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

/** `clipQuote` is observable only through `onRefusedAfterClose`, which the hook
 *  calls with `clipQuote(quoteOf(pending))` when a post is refused after its
 *  box has closed. Drive exactly that path and capture the clipped quote. */
async function clipViaRefusal(quote: string): Promise<string> {
  let answer: (ok: boolean) => void = () => {}
  const submit = vi.fn(() => new Promise<boolean>(resolve => { answer = resolve }))
  const onRefusedAfterClose = vi.fn()
  const hook = renderHook(() => useSelectionComposerAnchor<Anchor>({
    resolveDomAnchor: () => ({ quote }),
    quoteOf: a => a.quote,
    quoteOnly: q => ({ quote: q }),
    submit,
    draftKey: 'mc-truncate-test',
    onRefusedAfterClose,
  }))
  act(() => { hook.result.current.selectionComposer.onOpen?.(quote) })
  const p = hook.result.current.selectionComposer.onSubmit('x', quote) as Promise<boolean>
  act(() => { hook.result.current.selectionComposer.onClose?.() })
  await act(async () => { answer(false); await p })
  const [clipped] = onRefusedAfterClose.mock.calls[0] as [string]
  return clipped
}

beforeEach(() => { window.sessionStorage.clear() })

// NOTICE_QUOTE_MAX = 48; clipQuote slices to 47 then appends '…'.
describe('useSelectionComposerAnchor clipQuote emoji truncation', () => {
  it('drops an emoji the 48-unit notice cap would cut in half', async () => {
    // '🎉' occupies UTF-16 units 46 and 47. The cut at 47 keeps the high
    // surrogate (46) and drops the low (47), so a plain slice leaves U+FFFD.
    const quote = 'a'.repeat(46) + '🎉' + 'z'.repeat(5)
    const clipped = await clipViaRefusal(quote)
    expect(clipped).toBe('a'.repeat(46) + '…')
    expect(clipped).not.toMatch(LONE_HIGH)
  })

  it('keeps an emoji whole when it ends exactly at the notice cap', async () => {
    // '🎉' occupies units 45 and 46, ending at index 47 — the exact cut point,
    // so the whole emoji is kept.
    const quote = 'a'.repeat(45) + '🎉' + 'z'.repeat(5)
    const clipped = await clipViaRefusal(quote)
    expect(clipped).toBe('a'.repeat(45) + '🎉' + '…')
    expect(clipped).not.toMatch(LONE_HIGH)
  })
})
