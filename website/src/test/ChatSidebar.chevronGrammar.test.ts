import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

/**
 * The sessions sidebar teaches ONE disclosure grammar (#2887): a ChevronRight
 * that rotates 90° when its section is open — animated at one shared
 * duration — and sits unrotated when closed. That grammar is enforced BY
 * CONSTRUCTION: every stateful disclosure renders the shared
 * `DisclosureChevron` component, so glyph swaps, counter-rotation,
 * inline-style rotation and divergent durations cannot drift in per call
 * site. This ratchet covers only what the component cannot: (a) the
 * component itself carrying the canonical shape, and (b) no SECOND rotation
 * grammar appearing beside it in this file. Two banned modes, for the
 * record:
 *
 * - the Right/Down GLYPH SWAP (`x ? <ChevronRight/> : <ChevronDown/>`, or the
 *   alias spelling `x ? ChevronRight : ChevronDown`): same resting pixels,
 *   no rotation transition — adjacent rows animate differently;
 * - the COUNTER-ROTATION (`open ? 'rotate-90' : '-rotate-90'`): points the
 *   closed chevron UP (removed by #2884, pinned closed here).
 *
 * Position is the one deliberate asymmetry and is NOT asserted: the Older
 * Sessions section header trails with its chevron (a #2884 design-owner
 * decision) while row-level disclosures lead like tree rows everywhere else.
 * The rationale lives in comments at the component and the header call site.
 */

const SRC = join(__dirname, '..', 'pages', 'ChatSidebar.tsx')

// Flattened FIRST (same hardening as filterInputFont.test.tsx): a line-by-line
// scan misses an offending element the moment a reformat splits it across
// lines. Flattening makes every element a single matchable run.
const flat = readFileSync(SRC, 'utf8').replace(/\s+/g, ' ')

describe('sidebar disclosure-chevron grammar (#2887)', () => {
  it('the shared DisclosureChevron carries the canonical shape', () => {
    const decl = flat.match(/function DisclosureChevron\b.*?<ChevronRight\b[^>]*\/>/)?.[0]
    expect(decl).toBeDefined()
    // ChevronRight, animated at the one shared duration, rotated 90° only
    // when open, never counter-rotated, never via inline style.
    expect(decl).toContain('transition-transform')
    expect(decl).toContain('duration-200')
    expect(decl).toMatch(/open \? 'rotate-90' : ''/)
    expect(decl).not.toContain('-rotate-90')
    expect(decl).not.toMatch(/style=\{\{/)
  })

  it('all four disclosure sites render DisclosureChevron', () => {
    const uses = [...flat.matchAll(/<DisclosureChevron\b/g)]
    // Older Sessions header, history group headers, hidden-folders reveal,
    // folders filter row. Adding a fifth disclosure is fine — bump this count
    // in the same commit so the addition is a decision, not drift.
    expect(uses).toHaveLength(4)
  })

  it('no second rotation grammar appears beside the component', () => {
    // rotate-90 may appear ONLY inside DisclosureChevron's own declaration —
    // a call site re-rolling its own rotated chevron is the drift #2887
    // removed. (Other elements legitimately rotate at other angles; only the
    // disclosure grammar's own token is reserved.)
    const outside = flat.replace(/function DisclosureChevron\b.*?\/> }/, '')
    expect(outside).not.toContain('rotate-90')
    // No chevron glyph-swap in either spelling, anywhere in the file.
    expect(flat).not.toMatch(/\?\s*<Chevron(?:Right|Down)\b[^>]*\/>\s*:\s*<Chevron(?:Right|Down)\b/)
    expect(flat).not.toMatch(/\?\s*Chevron(?:Right|Down)\b\s*:\s*Chevron(?:Right|Down)\b/)
    // No chevron rotated through an inline style.
    for (const el of flat.matchAll(/<Chevron(?:Right|Down)\b[^>]*\/>/g)) {
      expect(el[0]).not.toMatch(/style=\{\{[^}]*rotate/)
    }
  })
})
