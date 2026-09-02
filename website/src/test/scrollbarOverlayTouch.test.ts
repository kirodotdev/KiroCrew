/**
 * A hover-revealed scrollbar thumb must still show where `:hover` cannot fire.
 *
 * Two utilities share the pattern: `.scroll-fade` (horizontal, code and diff
 * blocks) and `.scrollbar-overlay` (vertical). Both hide the thumb on BOTH
 * engines in their base rules — Firefox via `scrollbar-color`, WebKit/Blink via
 * `::-webkit-scrollbar-thumb` — and paint it only under `:hover`, so a finger
 * gets no affordance at all.
 *
 * `.scrollbar-overlay`'s branch and its chosen predicate/token are pinned by
 * ChatPage.composerChromeOcclusion.test.tsx; those assertions are deliberately
 * NOT repeated here. What this file adds is the PATTERN: the members are derived
 * from the stylesheet, so a utility with a hover-only thumb and no touch branch
 * fails here by construction rather than by someone remembering to add a case.
 *
 * Asserted against SOURCE TEXT, as that spec and noPageZoom.test.ts already do
 * for this stylesheet: jsdom never loads index.css, and a media query has no
 * computed representation to read even if it did.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const CSS = readFileSync(resolve(__dirname, '../index.css'), 'utf8')

/** Every class whose thumb is revealed ONLY by `:hover` — the pattern's members. */
const HOVER_ONLY = [...CSS.matchAll(/\.([a-zA-Z0-9_-]+):hover::-webkit-scrollbar-thumb\{/g)].map(m => m[1])
/** The `:hover` reveal — anchors the premise that the hover-only pattern exists. */
const HOVER_THUMB = /\.scroll-fade:hover::-webkit-scrollbar-thumb\{background:([^}]+)\}/.exec(CSS)
/** Bodies of every `@media (pointer: coarse)` block, concatenated. */
const COARSE = [...CSS.matchAll(/@media \(pointer: coarse\) \{\n([\s\S]*?)\n\}/g)].map(m => m[1]).join('\n')

describe('a hover-revealed thumb keeps an affordance where hover cannot fire', () => {
  it('anchors the premise: the pattern has members and a coarse-pointer branch exists', () => {
    // A regex that silently stopped matching would make the cases below pass on
    // an empty haystack.
    expect(HOVER_ONLY.length, 'no hover-only thumb rules found in index.css').toBeGreaterThan(1)
    expect(HOVER_THUMB, '.scroll-fade :hover thumb rule not found').not.toBeNull()
    expect(COARSE, 'no @media (pointer: coarse) body found').not.toBe('')
  })

  it('covers EVERY hover-only thumb in the stylesheet, not just the one reported', () => {
    // The pattern, not the utility, is the unit: `.scroll-fade` was missed when
    // the overlay utility got its branch, and a third member would be missed the
    // same way. Deriving the list is what makes that impossible.
    for (const cls of HOVER_ONLY) {
      expect(COARSE, `.${cls} has a hover-only thumb but no coarse-pointer branch`)
        .toContain(`.${cls}::-webkit-scrollbar-thumb{`)
      expect(COARSE, `.${cls} has no coarse-pointer scrollbar-color`).toContain(`.${cls}{scrollbar-color:`)
    }
  })

  it('names one token across both engines and both utilities, pinned to the contrast-safe rung', () => {
    // Both engines and both utilities still name ONE token, so the two coarse
    // branches cannot drift apart the next time either is retuned. That token is
    // deliberately NOT the hover reveal's (--border): on touch this thumb is the
    // sole scroll affordance, so it must clear the WCAG 1.4.11 3:1 non-text
    // contrast floor against --bg-elevated in every theme, and --muted is the
    // only existing token that does — scrollbarThumbContrast.test.ts measures
    // that per theme. The exact pin here keeps a retune from silently swapping in
    // a token whose contrast nobody measured.
    const decls = [...COARSE.matchAll(/\{(?:background|scrollbar-color):(var\([^)]+\))/g)].map(m => m[1])
    expect(decls.length).toBe(HOVER_ONLY.length * 2)
    for (const d of decls) expect(d).toBe('var(--muted)')
  })

  it('declares each branch after the base rule it overrides, which makes it win', () => {
    // A media query adds no specificity, so a branch TIES its utility's
    // transparent base rule and wins on source order alone; hoisted above it the
    // branch is inert while the stylesheet still reads as though it were present.
    for (const cls of HOVER_ONLY) {
      const base = CSS.indexOf(`.${cls}::-webkit-scrollbar-thumb{background:transparent`)
      expect(base, `.${cls} base thumb rule not found`).toBeGreaterThan(-1)
      const branch = CSS.indexOf(`.${cls}::-webkit-scrollbar-thumb{background:var(`, base)
      expect(branch, `.${cls} coarse-pointer thumb rule not found after its base rule`).toBeGreaterThan(base)
    }
  })

  it('changes colour only, so no call site can reflow', () => {
    // The reserved gutter comes from `::-webkit-scrollbar{width|height}` and
    // `scrollbar-width`; re-declaring any of them here would be a layout change.
    expect(COARSE).not.toMatch(/scrollbar-width/)
    expect(COARSE).not.toMatch(/::-webkit-scrollbar\{/)
  })
})
