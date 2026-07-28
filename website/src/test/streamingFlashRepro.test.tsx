import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * Root-cause probe for the "response bubble flashes while streaming" report.
 *
 * Hypothesis: `rehypeStreamingReveal` wraps EVERY char of the streaming tail
 * block in `<span class="ft-word">` (each carrying `animation: ft-fade` on
 * mount). The design assumes React reconciles those keyless spans by position
 * so already-visible chars keep their DOM node and never re-animate. That
 * holds for pure append, but when a newly-revealed char COMPLETES a markdown
 * token (inline `code`, **bold**, a link, ...), react-markdown restructures
 * the subtree, so React unmounts/remounts the spans for text that was already
 * on screen — re-firing ft-fade → a visible flash.
 *
 * We can't see a CSS animation in jsdom, but a remount is exactly equivalent:
 * the mount-triggered `ft-fade` only re-fires when the DOM node instance is
 * replaced. So we compare `.ft-word` node IDENTITY across a streaming re-parse.
 * Survived nodes (same instance) do NOT re-animate; replaced nodes DO.
 */

const STREAM = { streaming: true, glow: true, smooth: true } as const

function ftWords(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.ft-word'))
}

describe('streaming flash root cause', () => {
  it('BASELINE: pure text append keeps already-revealed char nodes stable', () => {
    // Tail is plain prose. Revealing more prose should only MOUNT new char
    // spans; every previously-rendered char span must be the same instance.
    const { container, rerender } = render(
      <MarkdownRenderer content={'See code and more text here'} {...STREAM} />,
    )
    const before = ftWords(container)
    expect(before.length).toBeGreaterThan(0)

    rerender(<MarkdownRenderer content={'See code and more text here now'} {...STREAM} />)
    const after = ftWords(container)

    const survived = after.filter((n) => before.includes(n))
    const remounted = before.filter((n) => !after.includes(n))

    // Design intent: append-only reveal is stable — nothing already shown is
    // torn down. All the original char nodes survive.
    expect(survived.length).toBe(before.length)
    expect(remounted.length).toBe(0)
  })

  it('BUG: completing an inline `code` token remounts the already-visible trailing text', () => {
    // Tail before completion: an unclosed backtick renders as literal text, so
    // "code and more text here" are all ft-word spans in one <p>.
    const { container, rerender } = render(
      <MarkdownRenderer content={'See `code and more text here'} {...STREAM} />,
    )
    const before = ftWords(container)
    expect(before.length).toBeGreaterThan(0)

    // Closing the backtick turns `code` into a <code> element, splitting the
    // paragraph's children. Everything after the code span shifts position and
    // type, so React remounts it — even though the text "and more text here"
    // is UNCHANGED and was already on screen.
    rerender(<MarkdownRenderer content={'See `code` and more text here'} {...STREAM} />)
    const after = ftWords(container)

    const survived = after.filter((n) => before.includes(n))
    const remounted = before.filter((n) => !after.includes(n))

    // Diagnostic output so the confirmation is legible in the run log.
    // eslint-disable-next-line no-console
    console.log(
      `[flash-repro] before=${before.length} after=${after.length} ` +
        `survived=${survived.length} remounted=${remounted.length}`,
    )

    // If the bug is real, a large chunk of already-visible chars are remounted
    // (their mount-time ft-fade re-fires → flash), NOT reconciled in place.
    expect(remounted.length).toBeGreaterThan(0)
  })

  it('FIX: a token completing BEHIND the reveal edge renders plain (no fade span) so already-visible text cannot re-fade', () => {
    // Inline-code token near the START, then a long plain tail so the token
    // sits well behind the 240-char reveal edge. When it completes it must
    // restructure PLAIN text (no .ft-word), so nothing already on screen
    // re-fades. Only the trailing edge stays animated.
    const tail = 'more '.repeat(70) // ~350 chars of plain, growing tail
    const c1 = `start see \`code and then ${tail}`
    const c2 = `start see \`code\` and then ${tail}extra ` // token completes + tail grows

    const { container, rerender } = render(<MarkdownRenderer content={c1} {...STREAM} />)
    rerender(<MarkdownRenderer content={c2} {...STREAM} />)
    const after = ftWords(container)

    // 1. Reveal is EDGE-BOUNDED: only ~REVEAL_TAIL_CHARS (240) chars are
    //    wrapped, not the whole ~380-char block (pre-fix this was the full count).
    expect(after.length).toBeGreaterThan(0)
    expect(after.length).toBeLessThanOrEqual(245)

    // 2. The completed token materializes a <code> element in the SETTLED
    //    region, and it sits BEFORE the first fade span in document order —
    //    i.e. the settled token carries no .ft-word and therefore cannot flash.
    const code = container.querySelector('code')
    const firstFtWord = container.querySelector('.ft-word')
    expect(code).not.toBeNull()
    expect(firstFtWord).not.toBeNull()
    expect(
      !!(code!.compareDocumentPosition(firstFtWord!) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true)

    // 3. No fade span contains the settled token's text.
    expect(after.some((n) => (n.textContent || '').includes('c'))).toBeDefined()
    const settledHasFade = Array.from(container.querySelectorAll('code .ft-word')).length
    expect(settledHasFade).toBe(0)

    // eslint-disable-next-line no-console
    console.log(`[flash-fix] edge-bounded ft-word count=${after.length}; settled token plain (code before first fade span)`)
  })
})
