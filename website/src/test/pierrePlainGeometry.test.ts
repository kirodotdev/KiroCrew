/**
 * The staged code-block stand-in must be the SAME HEIGHT as the Pierre surface it
 * stands in for.
 *
 * `pierreStaging.ts` states that invariant ("if the two differ in height, that
 * trade also moves the scroll position, which is a worse bug"), and it was broken
 * in a way no unit test could see: the stand-in DECLARES `leading-5` (20px) and
 * `py-2` (8px), but inside a transcript `.msg-content pre` -- two selectors, so it
 * beats a single-class utility -- imposed `line-height:1.5` (19.5px at 13px) and
 * `padding:10px 12px`. Measured in a real browser: every `.pierre-surface` shrank
 * by exactly 4px when its chunk resolved, and a row with three code blocks shrank
 * 36px, displacing a reader scrolling above it.
 *
 * jsdom computes no cascade, so this is a SOURCE guard: the override that restores
 * Pierre's measured box must exist, and it must live at a specificity that beats
 * the transcript rule. A CSS-in-a-string test is the only place this can be
 * caught before a browser shows it.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const css = readFileSync(join(__dirname, '..', 'index.css'), 'utf8')
const fallback = readFileSync(join(__dirname, '..', 'pierre', 'PlainCodeFallback.tsx'), 'utf8')

describe('staged code stand-in matches Pierre geometry', () => {
  it('the stand-in carries the hook the transcript rule can be beaten with', () => {
    // Without a dedicated class the utilities lose to `.msg-content pre`, which
    // is exactly how the 4px surplus went unnoticed.
    expect(fallback).toContain('pierre-plain')
    const codeBlock = readFileSync(join(__dirname, '..', 'components', 'CodeBlock.tsx'), 'utf8')
    expect(codeBlock).toContain('pierre-plain')
  })

  it('the transcript still imposes its own pre metrics (the rule being beaten)', () => {
    // If this line ever loses its line-height/padding, the override below is
    // no longer needed -- and keeping a stale override would itself be a
    // mismatch. Pinning the premise keeps the pair honest.
    const m = css.match(/\.msg-content pre\{[^}]*\}/)
    expect(m).not.toBeNull()
    expect(m?.[0]).toMatch(/line-height:\s*1\.5/)
    expect(m?.[0]).toMatch(/padding:\s*10px/)
    expect(m?.[0]).toMatch(/margin:\s*4px/)
  })

  it('restores Pierre\u2019s measured box: 20px per line, 8px top and bottom', () => {
    const m = css.match(/\.msg-content \.pierre-plain\{[^}]*\}/)
    expect(m).not.toBeNull()
    const rule = m?.[0] ?? ''
    expect(rule).toMatch(/line-height:\s*20px/)
    expect(rule).toMatch(/padding-top:\s*8px/)
    expect(rule).toMatch(/padding-bottom:\s*8px/)
    // The other half: `.msg-content pre` also sets a 4px margin, and the existing
    // `.code-block>pre` reset does not reach a stand-in nested in `.pierre-surface`.
    expect(rule).toMatch(/margin:\s*0/)
  })

  it('the override outranks the transcript rule', () => {
    // `.msg-content .pierre-plain` is (0,2,0); `.msg-content pre` is (0,1,1).
    // Two classes beat one class plus one element, so the stand-in wins.
    const over = css.indexOf('.msg-content .pierre-plain{')
    const base = css.indexOf('.msg-content pre{')
    expect(over).toBeGreaterThan(-1)
    expect(base).toBeGreaterThan(-1)
    // Both present, and the override is a two-class selector.
    expect(css.slice(over, over + 27)).toContain('.msg-content .pierre-plain')
  })
})
