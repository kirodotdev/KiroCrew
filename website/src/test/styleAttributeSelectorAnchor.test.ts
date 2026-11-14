import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * No stylesheet may continue a selector past a `[style…]` attribute match.
 *
 * Chrome turns `[style*="x"] .y` into an invalidation set keyed on the `style`
 * attribute, so every inline style write on ANY element (each frame of a
 * Framer animation, a drawer drag, a virtualizer spacer) restyles that
 * element's whole subtree, whatever the rest of the selector says. With the
 * chat rules of cli-mode.css anchored that way, each frame of the phone
 * sessions drawer drag restyles ~590 sidebar elements. A selector that ENDS at the
 * `[style…]` match only restyles the element whose style changed, which is
 * allowed. Source-level because happy-dom has no style invalidation.
 */
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function cssFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out.push(...cssFiles(path))
    else if (name.endsWith('.css')) out.push(path)
  }
  return out
}

/** Selector preludes of every rule, comments removed. */
function selectors(css: string): string[] {
  const source = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const out: string[] = []
  for (const m of source.matchAll(/([^{}]*)\{/g)) {
    const prelude = (m[1].split(';').pop() ?? '').trim()
    if (prelude && !prelude.startsWith('@')) out.push(prelude)
  }
  return out
}

/** A `[style…]` match followed by a combinator and another compound. */
const CONTINUES_PAST_STYLE = /\[style[^\]]*\][^\s,>+~]*\s*[\s>+~]\s*[^\s,{]/

describe('stylesheets never anchor a descendant on the style attribute', () => {
  it('finds no selector that continues past a [style…] match', () => {
    const offenders: string[] = []
    for (const file of cssFiles(SRC)) {
      for (const sel of selectors(readFileSync(file, 'utf-8'))) {
        if (CONTINUES_PAST_STYLE.test(sel)) offenders.push(`${relative(SRC, file)}: ${sel}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('still recognises the shape it guards against', () => {
    expect(CONTINUES_PAST_STYLE.test('[data-ui="cli"] [style*="--mc-content-width"] .msg-content pre')).toBe(true)
    expect(CONTINUES_PAST_STYLE.test('[data-ui="cli"] [style*="--mc-content-width"] > .x')).toBe(true)
    expect(CONTINUES_PAST_STYLE.test('[data-ui="cli"] [style*="var(--mc-content-width"]')).toBe(false)
  })
})
