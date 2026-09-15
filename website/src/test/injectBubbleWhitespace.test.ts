import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * The injected-note bubble must not preserve source whitespace while rendering
 * markdown: `white-space: pre-wrap` inherits, so the whitespace-only text nodes
 * the markdown renderer emits between blocks stop collapsing and each newline
 * becomes its own line box — measured at 2375px of dead space above a 17-row
 * table. Dropping it alone would run a plain multi-line notification's lines
 * together, which is why `softBreaks` is asserted with it.
 *
 * Source rather than render, and only for the transcript copy: it is inline JSX
 * inside a very large component with no seam that mounts an inject row alone.
 * The app-sdk row registry carries the same bubble and is already asserted by
 * real rendering against that live registry; the layout itself is measured in a
 * real browser by the matching capture runner.
 */
const SURFACE = 'src/pages/ChatPage.tsx'

/** The inject row is the only warn-tinted bubble, so this identifies it. */
const MARKER = 'bg-warn-subtle'

/** Collapse whitespace so a reflow across lines cannot hide a match. */
function flat(path: string): string {
  return readFileSync(resolve(__dirname, '..', '..', path), 'utf8').replace(/\s+/g, ' ')
}

function markerClassNames(src: string): string[] {
  return [...src.matchAll(/className="([^"]*)"/g)]
    .map(m => m[1])
    .filter(cls => cls.includes(MARKER))
}

describe('inject bubble: markdown layout is not subject to preserved whitespace', () => {
  const src = flat(SURFACE)
  const classNames = markerClassNames(src)

  // Positive control: without it the assertions below pass vacuously once the
  // markup is renamed or moved, reporting a fix for markup never found.
  it('finds exactly one warn-tinted bubble container to assert on', () => {
    expect(classNames).toHaveLength(1)
  })

  it('does not preserve source whitespace on that container', () => {
    expect(classNames[0]).not.toContain('whitespace-pre-wrap')
  })

  it('passes softBreaks to the markdown renderer in that bubble', () => {
    // Scoped to the call carrying `cleanContent` — the inject bubble's own body
    // variable — and to that one tag, since other rows also pass softBreaks.
    expect(src).toMatch(/<MarkdownRenderer content=\{cleanContent\}[^>]*\bsoftBreaks\b/)
  })
})
