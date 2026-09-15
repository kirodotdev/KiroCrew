import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * CLI mode paints a `>` prompt glyph at `left: 8px` over the input wrapper and
 * indents the composer text by 24px so the glyph does not sit on the first
 * typed characters. The indent rule used to be qualified on
 * `textarea[data-composer-input]`; with the Lexical composer default on
 * fine-pointer devices the input is a contenteditable `div[data-composer-input]`,
 * so a `textarea`-qualified rule matched nothing while the glyph still painted.
 * The same indent must also reach the `[data-composer-placeholder]` overlay
 * (its own 16px `px-4` padding otherwise leaves the placeholder text under the
 * glyph). jsdom cannot compute the cascade for the attribute scope, so this
 * guards the rule at the source level.
 */
describe('cli-mode.css indents the composer regardless of its element type', () => {
  const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../styles/cli-mode.css'),
    'utf-8',
  )

  // Every `[data-ui="cli"]`-scoped block whose body sets `padding-left: 24px`.
  const indentBlocks = () => {
    const out: string[] = []
    const re = /(\[data-ui="cli"\][^{}]*)\{([^}]*)\}/g
    for (const m of css.matchAll(re)) {
      if (/padding-left:\s*24px\s*!important/.test(m[2])) out.push(m[1])
    }
    return out
  }

  it('does not tie the 24px prompt indent to a <textarea> element', () => {
    const selectors = indentBlocks()
    expect(selectors.length).toBeGreaterThan(0)
    for (const sel of selectors) {
      expect(sel).not.toMatch(/textarea\s*\[data-composer-input\]/)
    }
    // Element-agnostic hook: the attribute stands alone (preceded by whitespace
    // or a combinator), so the contenteditable div is covered too.
    expect(selectors.some(sel => /(^|[\s>+~])\[data-composer-input\]/.test(sel))).toBe(true)
  })

  it('indents the placeholder overlay by the same 24px', () => {
    const selectors = indentBlocks()
    expect(selectors.some(sel => /\[data-composer-placeholder\]/.test(sel))).toBe(true)
  })

  it('keeps the accent caret on the composer input itself', () => {
    const rule = /\[data-ui="cli"\][^{}]*(^|[\s>+~])\[data-composer-input\]\s*\{[^}]*caret-color:\s*var\(--accent\)/m
    expect(css).toMatch(rule)
  })
})
