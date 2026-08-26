import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'
import { SPINE_PHASES } from '../lib/fabric'
import { colVbX, TRACK_PAD_L, TRACK_PAD_R } from './PipelineView'

// ─────────────────────────────────────────────────────────────────────────────
// COORDINATE-SPACE FIX (SVG text horizontal-stretch defect).
//
// The lane-track SVG uses preserveAspectRatio="none" so the phase columns span
// the FULL track width (no dead space on the right). With a FIXED viewBox width
// and width="100%", the X scale becomes container_px / viewBox_width (~2x) while
// the Y scale stays 1, which horizontally stretches every glyph inside the SVG.
//
// The fix makes the viewBox width EQUAL the measured layout width in CSS pixels,
// so the X scale is exactly 1 (a glyph's on-screen aspect ratio equals its aspect
// ratio in the font) at ANY container width — while columns still span the full
// width because their x's are computed FROM that same width.
//
// These tests pin the MECHANISM, not the pixels.
// ─────────────────────────────────────────────────────────────────────────────

const SRC = readFileSync(
  'src/apps/auto-triage-pipeline/views/PipelineView.tsx',
  'utf8',
)

describe('lane-track coordinate space', () => {
  it('columns span the full track width less the symmetric end inset, at any width', () => {
    const last = SPINE_PHASES.length - 1
    for (const width of [320, 720, 1500, 2600]) {
      // first column sits at the left inset, last column at width minus the right
      // inset — i.e. the columns fill the whole width. This is what lets the
      // viewBox width equal the layout width (X scale 1) without letterboxing.
      expect(colVbX(0, width)).toBeCloseTo(TRACK_PAD_L, 6)
      expect(colVbX(last, width)).toBeCloseTo(width - TRACK_PAD_R, 6)
      // strictly increasing across columns
      for (let i = 1; i <= last; i++) {
        expect(colVbX(i, width)).toBeGreaterThan(colVbX(i - 1, width))
      }
    }
  })

  it('column positions scale WITH the width — a wider track spreads columns wider', () => {
    // The span between first and last column must grow linearly with width. If a
    // fixed viewBox width were reintroduced, this span would be constant (and the
    // SVG X scale would diverge from 1, stretching glyphs).
    const last = SPINE_PHASES.length - 1
    const span = (w: number) => colVbX(last, w) - colVbX(0, w)
    expect(span(1500)).toBeCloseTo(1500 - TRACK_PAD_L - TRACK_PAD_R, 6)
    expect(span(3000) - span(1500)).toBeCloseTo(1500, 6)
  })

  it('every SVG that uses preserveAspectRatio="none" has a width-parameterised viewBox, never a fixed constant', () => {
    // The distortion only happens when preserveAspectRatio="none" is paired with a
    // viewBox width that differs from the SVG's layout width. Both stretched SVGs
    // must derive their viewBox width from the measured `trackW`, so the layout
    // width and the viewBox width are the same number and the X scale is 1.
    const svgOpenTags = SRC.match(/<svg\b[\s\S]*?>/g) ?? []
    const stretched = svgOpenTags.filter((t) => /preserveAspectRatio\s*=\s*"none"/.test(t))
    // there ARE stretched SVGs (the track + the header ticks) — the fix does not
    // remove them, it makes their viewBox width match layout width.
    expect(stretched.length).toBeGreaterThan(0)
    for (const tag of stretched) {
      const vb = tag.match(/viewBox=\{`0 0 \$\{([^}]+)\}[^`]*`\}/)
      expect(vb, `viewBox must be a template using the measured width: ${tag}`).toBeTruthy()
      // the width expression must reference the runtime-measured trackW, not a
      // fixed module constant / numeric literal.
      expect(vb![1]).toMatch(/trackW/)
    }
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// QUEUE-SUMMARY INTERPOLATION.
//
// `longestWait` carries BOTH the lane's number and its phase, and the tooltip key
// is worded "Waiting in {{phase}}" — so passing the number into `phase` renders
// "Waiting in #5071", which asserts that an issue number is a phase. It read as
// merely terse while the English string was the fragment "in {{phase}}"; the gate
// that forced a complete sentence is what made the lie legible.
//
// Source-level like the tests above: the view has no render harness here, and the
// mechanism worth pinning is which FIELD reaches the placeholder.
// ─────────────────────────────────────────────────────────────────────────────

describe('queue summary interpolation', () => {
  it("feeds the longest-wait tooltip a phase, never an item number", () => {
    const call = SRC.match(/summary_longest_wait_in',\s*\{([^}]*)\}/)
    expect(call, 'the longest-wait tooltip must interpolate its placeholder').not.toBeNull()
    const args = call![1]
    expect(args).toMatch(/phase:\s*longest\.phase\b/)
    expect(args).not.toMatch(/longest\.number/)
  })
})
