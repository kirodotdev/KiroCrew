import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * Regression guard for the "response bubble flashes while streaming" report.
 *
 * Root cause: `rehypeStreamingReveal` wraps the streaming tail in
 * `<span class="ft-word">`. react-markdown re-parses the whole tail every
 * frame, and when a newly-revealed char COMPLETES a markdown token (inline
 * `code`, **bold**, a link, ...) the subtree restructures, so React
 * unmounts/remounts the spans for text that was ALREADY on screen. The former
 * approach fired a mount-triggered CSS keyframe (`ft-char-fade`) on those
 * spans, so every such remount re-ran the fade → a visible flash right at the
 * active edge.
 *
 * The fix does NOT try to prevent the remount (that is inherent to
 * react-markdown). Instead each span's opacity is a pure function of its
 * distance to the streaming tip (`--ft-o`, see revealOpacity), so a remounted
 * span re-appears at the IDENTICAL opacity and cannot re-fade. These tests
 * assert that invariant: remounts still happen, but the per-char opacity read
 * from the DOM is stable across a token completion.
 */

const STREAM = { streaming: true, glow: true, smooth: true } as const

function ftWords(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.ft-word'))
}

/** Opacities of the `.ft-word` spans, read from the injected `--ft-o` custom
 *  property, in reverse document order (index 0 = the tip / newest char). */
function tailOpacities(container: HTMLElement): number[] {
  return ftWords(container)
    .map(n => parseFloat(n.style.getPropertyValue('--ft-o') || '1'))
    .reverse()
}

describe('streaming flash regression', () => {
  it('injects a position-derived --ft-o opacity on edge spans (tip dimmest, ramping to 1)', () => {
    const { container } = render(
      <MarkdownRenderer content={'the quick brown fox jumps over'} {...STREAM} />,
    )
    const spans = ftWords(container)
    expect(spans.length).toBeGreaterThan(0)
    // Every edge span carries a numeric --ft-o (the fix's mechanism). If this
    // ever regresses to empty, the reveal is no longer position-driven.
    for (const s of spans) {
      const v = s.style.getPropertyValue('--ft-o')
      expect(v).not.toBe('')
      const n = parseFloat(v)
      expect(n).toBeGreaterThanOrEqual(0.6)
      expect(n).toBeLessThanOrEqual(1)
    }
    // Tip (last char) is the dimmest; opacity is monotonic non-decreasing as we
    // walk backward from the tip.
    const rev = tailOpacities(container)
    expect(rev[0]).toBeCloseTo(0.6, 5)
    for (let i = 1; i < rev.length; i++) expect(rev[i]).toBeGreaterThanOrEqual(rev[i - 1])
  })

  it('the reveal is EDGE-BOUNDED: only the trailing chars are wrapped, not the whole block', () => {
    const long = 'word '.repeat(80) // ~400 chars
    const { container } = render(<MarkdownRenderer content={long} {...STREAM} />)
    // Bounded to REVEAL_FADE_CHARS (32) — settled text behind the edge is plain.
    expect(ftWords(container).length).toBeLessThanOrEqual(32)
  })

  it('PREMISE: completing an inline `code` token still remounts already-visible spans', () => {
    // This documents that the remount is inherent to react-markdown — the fix
    // makes it harmless rather than preventing it.
    const { container, rerender } = render(
      <MarkdownRenderer content={'see `code and more here'} {...STREAM} />,
    )
    const before = ftWords(container)
    expect(before.length).toBeGreaterThan(0)
    rerender(<MarkdownRenderer content={'see `code` and more here'} {...STREAM} />)
    const after = ftWords(container)
    const remounted = before.filter(n => !after.includes(n))
    // eslint-disable-next-line no-console
    console.log(`[flash-repro] before=${before.length} after=${after.length} remounted=${remounted.length}`)
    expect(remounted.length).toBeGreaterThan(0)
  })

  it('FIX: a token completing WITHIN the edge does not re-fade already-visible text (opacity is position-stable)', () => {
    // Unclosed backtick renders literally; the tail "and more text here" is all
    // ft-word spans. Closing the backtick turns `code` into <code>, remounting
    // the trailing spans (see PREMISE). The trailing text and its distance to
    // the tip are UNCHANGED, so its per-char opacity must be identical — proving
    // the remount cannot cause a flash.
    const { container, rerender } = render(
      <MarkdownRenderer content={'see `code and more text here'} {...STREAM} />,
    )
    const before = tailOpacities(container)

    rerender(<MarkdownRenderer content={'see `code` and more text here'} {...STREAM} />)
    const after = tailOpacities(container)

    // The visible trailing text "and more text here" (18 chars) is unchanged and
    // sits at the tip in both renders. Compare that overlapping suffix window:
    // every char's opacity must match across the remount.
    const overlap = Math.min(before.length, after.length, 18)
    expect(overlap).toBeGreaterThan(0)
    for (let i = 0; i < overlap; i++) {
      expect(after[i]).toBeCloseTo(before[i], 5)
    }
    // eslint-disable-next-line no-console
    console.log(`[flash-fix] position-stable opacity across token completion; overlap=${overlap} tip=${after[0]}`)
  })
})
