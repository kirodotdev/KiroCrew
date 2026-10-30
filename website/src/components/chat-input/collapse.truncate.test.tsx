import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useComposerCollapse } from './collapse'

// The collapsed bar shows one line of the waiting draft, cut to 120 chars with
// an ellipsis. A plain UTF-16 slice(0, 120) through an emoji that straddles the
// 120th unit leaves a lone high surrogate, which renders as U+FFFD. These tests
// pin that the cut lands on a whole code point instead.

const CAP = 120
const CONFETTI = '🎉' // U+1F389 — one astral char, two UTF-16 units: \uD83C\uDF89
const LONE_HIGH_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

function draftLineFor(value: string): string {
  const { result } = renderHook(() =>
    useComposerCollapse({ collapsible: true, composerControl: () => null, value }),
  )
  return result.current.collapsedDraftLine
}

describe('useComposerCollapse collapsedDraftLine emoji truncation', () => {
  it('drops an emoji straddling the 120-char cap without leaving a lone surrogate', () => {
    // 119 filler chars put the emoji's high surrogate at index 119 and its low
    // surrogate at index 120, so a UTF-16 cut at 120 splits the pair.
    const value = 'a'.repeat(CAP - 1) + CONFETTI + 'tail'
    const line = draftLineFor(value)
    expect(line).toBe('a'.repeat(CAP - 1) + '…')
    expect(line).not.toMatch(LONE_HIGH_SURROGATE)
  })

  it('keeps an emoji whole when it ends exactly at the 120-char cap', () => {
    // 118 filler + emoji occupies units 0..119, ending exactly at the cap.
    const value = 'a'.repeat(CAP - 2) + CONFETTI + 'tail'
    const line = draftLineFor(value)
    expect(line).toBe('a'.repeat(CAP - 2) + CONFETTI + '…')
    expect(line).not.toMatch(LONE_HIGH_SURROGATE)
  })
})
