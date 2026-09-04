/**
 * Logs page: narrow-viewport reachability, and log-line break behaviour.
 *
 * Both pinned contracts were measured in a real browser at the width the log
 * card gets on a 390px phone, because happy-dom does no layout.
 *
 * Toolbar (WCAG 1.4.10 Reflow). The filter field is `flex-1` and the three
 * trailing toggles are `whitespace-nowrap`, so the row needs 154px more than the
 * 342px it has. Nothing in its ancestry scrolls, so `Wrap` ended up 74px past
 * the right edge and `Tail` 154px past it — off-screen and untappable, not
 * merely ugly. With `flex-wrap` the row occupies three lines and overflows by 0.
 * The level row directly above it already wrapped; this one was the outlier.
 *
 * Log lines. `word-break: break-all` breaks between letters whether or not
 * anything would overflow, so it split `allow-lists` into `allow-lis` + `ts` —
 * an identifier that no longer reads as itself, in a pane people copy from.
 * `overflow-wrap: break-word` breaks only where a line cannot otherwise fit, so
 * the same string breaks at its hyphen instead, and a 240-character base64 blob
 * is still contained with zero horizontal overflow.
 */
import { describe, it, expect } from 'vitest'

const SRC = new URL('../pages/LogsPage.tsx', import.meta.url)

async function source(): Promise<string> {
  const mod = await import('../pages/LogsPage.tsx?raw')
  return mod.default as string
}

describe('LogsPage narrow-viewport contract', () => {
  it('the filter/toggle row can wrap, so no toggle is pushed off-screen', async () => {
    const src = await source()
    // The row is identified by the toggles it holds, not by a line number.
    const row = src.match(/className=\{`flex gap-2[^`]*`\}/)
    expect(row, `expected a "flex gap-2" toolbar row in ${SRC.pathname}`).not.toBeNull()
    expect(row![0]).toContain('flex-wrap')
  })

  it('wrapped log lines break at word boundaries, never between letters', async () => {
    const src = await source()
    expect(src).toContain('whitespace-pre-wrap break-words')
    // `break-all` is the defect: it breaks every word, corrupting identifiers.
    expect(src).not.toContain('break-all')
  })

  it('the unwrapped mode still refuses to wrap, so Wrap: off keeps meaning something', async () => {
    const src = await source()
    expect(src).toContain("'whitespace-pre'")
  })
})
