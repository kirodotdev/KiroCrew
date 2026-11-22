import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * CLI UI mode draws a `>` prompt glyph absolutely at the input wrapper's left
 * edge. On the one-row composer shape (`data-inline-toolbar="true"`, the
 * crewmate DM composer) the attach cluster is the wrapper's first flex item
 * and sits on that same edge, so the glyph landed on top of `+`.
 *
 * jsdom does not paint pseudo-elements or run the cascade for this scope, so
 * the guard is at the source level, like the other cli-mode tests; the capture
 * harness (scripts/capture-crew-composer-one-row.mjs) measures the result.
 */
const here = dirname(fileURLToPath(import.meta.url))
const css = readFileSync(resolve(here, '../styles/cli-mode.css'), 'utf-8')

describe('cli-mode.css keeps the prompt glyph off the one-row attach button', () => {
  it('draws no wrapper-edge glyph on the one-row shape', () => {
    // Every `input-wrapper ... ::before` selector must exclude the one-row shape,
    // or end on a descendant of it (the text column).
    const selectors = css.match(/[^{}]*\[data-testid="input-wrapper"\][^{}]*::before[^{]*\{/g) ?? []
    expect(selectors.length).toBeGreaterThan(0)
    for (const group of selectors) {
      for (const sel of group.replace(/\{$/, '').split(',')) {
        if (!sel.includes('::before')) continue
        const onWrapper = /\[data-testid="input-wrapper"\](?:\[[^\]]*\]|:[\w-]+\([^)]*\))*::before/.test(sel)
        if (onWrapper) expect(sel).toContain(':not([data-inline-toolbar="true"])')
      }
    }
  })

  it('puts the glyph on the text column of the one-row shape', () => {
    expect(css).toMatch(/\[data-testid="input-wrapper"\]\[data-inline-toolbar="true"\]\s*>\s*:has\(\[data-composer-input\]\)::before/)
  })

  it('still targets what ChatInput emits for the one-row shape', () => {
    // Load-bearing only while the wrapper carries the attribute and the attach
    // cluster is ordered first on that row.
    const tsx = readFileSync(resolve(here, '../components/ChatInput.tsx'), 'utf-8')
    expect(tsx).toContain(`data-inline-toolbar={inlineToolbar ? 'true' : undefined}`)
    expect(tsx).toContain(`inlineToolbar ? ' order-first`)
  })
})
