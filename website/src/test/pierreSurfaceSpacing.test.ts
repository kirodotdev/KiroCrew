/**
 * Pierre surfaces pin letter- and word-spacing to `normal`.
 *
 * Pierre's editor places its caret, selections and hit-testing at
 * column x canvas measureText('0') in the editable root's font, and that
 * measurement carries no spacing. Any tracking the surface inherits from its
 * host (the Files app root sets `letter-spacing: -.02em`) renders the text
 * narrower than that grid, so the caret lands further right of the real
 * insertion point the longer the line is.
 *
 * Asserted against SOURCE TEXT: jsdom never loads index.css, so a computed
 * style read would see nothing.
 */
import { describe, it, expect } from 'vitest'
import { readIndexCss } from './indexCssSource'

const CSS = readIndexCss().replace(/\/\*[\s\S]*?\*\//g, '')

/** Declarations of every top-level `.pierre-surface{...}` block. */
function surfaceDeclarations(): string {
  const blocks = [...CSS.matchAll(/(^|[}\s])\.pierre-surface\s*\{([^}]*)\}/g)].map(m => m[2])
  return blocks.join(';')
}

describe('.pierre-surface spacing', () => {
  it('pins letter-spacing to normal so the caret grid matches the rendered text', () => {
    expect(surfaceDeclarations()).toMatch(/letter-spacing\s*:\s*normal/)
  })

  it('pins word-spacing to normal for the same reason', () => {
    expect(surfaceDeclarations()).toMatch(/word-spacing\s*:\s*normal/)
  })
})
