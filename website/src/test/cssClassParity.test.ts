/**
 * Every `mc-*` class a component *styles itself with* must be defined in CSS.
 *
 * This exists because of a real, shipped regression: the artifact page's
 * anchored-comment overlay (`InlineCommentOverlay`) renders its highlight rects
 * and gutter bubbles as `mc-cmt-rect` / `mc-cmt-bubble` / `mc-cmt-overlay`
 * elements, but the stylesheet block defining those three classes was never
 * ported into this repo. The result was silent and total: the overlay mounted
 * the correct elements into the DOM with the correct geometry, every unit test
 * passed, and the feature looked completely absent in the browser — transparent,
 * zero-styled divs. `mc-art-toolbar` was missing the same way, leaving the
 * artifact toolbar's controls unaligned.
 *
 * A DOM-level test cannot catch this (jsdom applies no stylesheet), so the guard
 * has to be static: scan for class names used in `className=` and assert each
 * has a definition. Only `mc-`-prefixed classes are checked — the project also
 * uses that prefix for localStorage keys and window-event names, which
 * legitimately have no styling, so the scan is restricted to class positions.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, extname, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// `new URL('..', import.meta.url).pathname` yields a URL path, not a filesystem
// path — resolve through fileURLToPath so this works regardless of cwd.
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === 'test') continue
    const full = join(dir, name)
    if (statSync(full).isDirectory()) walk(full, out)
    else out.push(full)
  }
  return out
}

/**
 * Pre-existing unstyled classes, baselined so this guard gates NEW regressions
 * instead of demanding a restyle of unrelated components. Each is applied to an
 * element that therefore renders with no styling — their owners should either add
 * the rule or drop the class. Do not add to this list to silence a new failure.
 */
const KNOWN_UNSTYLED = new Set([
  'mc-comment-tooltip',   // components/MarkdownPanel.tsx
  'mc-fe-tab-file',       // apps/file-explorer/TabStrip.tsx
  'mc-fe-tab-folder',     // apps/file-explorer/TabStrip.tsx
])

const files = walk(SRC)
const sources = files.filter(f => ['.ts', '.tsx'].includes(extname(f)))
const styles = files.filter(f => extname(f) === '.css')

/** Class names appearing inside a `className=` / `class=` attribute value. */
function usedClasses(): Map<string, string> {
  const found = new Map<string, string>()
  // className={...} or className="..." — capture the whole attribute value,
  // template literals included, then pull mc-* tokens out of it.
  const attr = /class(?:Name)?\s*=\s*(?:"([^"]*)"|'([^']*)'|\{([^}]*)\})/g
  for (const f of sources) {
    const text = readFileSync(f, 'utf8')
    for (const m of text.matchAll(attr)) {
      const value = m[1] ?? m[2] ?? m[3] ?? ''
      for (const cls of value.matchAll(/\bmc-[a-z0-9-]+/g)) {
        if (!found.has(cls[0])) found.set(cls[0], f.slice(SRC.length))
      }
    }
  }
  return found
}

/**
 * Selector definitions, from .css AND from CSS-in-TS (some app bundles ship their
 * stylesheet as an exported template string injected into a <style> block, e.g.
 * `apps/file-explorer/styles.ts`). A dotted kebab-case name cannot be a JS member
 * expression, so `.mc-a-b` is unambiguously a selector in either file type — no
 * need to parse.
 */
function definedClasses(): Set<string> {
  const defined = new Set<string>()
  for (const f of [...styles, ...sources]) {
    for (const m of readFileSync(f, 'utf8').matchAll(/\.(mc-[a-z0-9-]*[a-z0-9])(?=[\s,:{.[)])/g)) {
      defined.add(m[1])
    }
  }
  return defined
}

describe('mc-* class parity between components and stylesheets', () => {
  it('defines every mc-* class used in a className attribute', () => {
    const used = usedClasses()
    const defined = definedClasses()
    const missing = [...used.entries()]
      .filter(([cls]) => !defined.has(cls) && !KNOWN_UNSTYLED.has(cls))
      .map(([cls, file]) => `${cls} (used in ${file})`)
      .sort()
    expect(
      missing,
      `${missing.length} mc-* class(es) are applied to elements but have no CSS rule. ` +
        'An unstyled class renders an invisible element and no DOM test will notice:\n  ' +
        missing.join('\n  '),
    ).toEqual([])
  })

  it('keeps the pre-existing-unstyled baseline honest', () => {
    // If someone styles one of these, the entry is stale and should be deleted so
    // the baseline never silently grows into a permanent exemption list.
    const defined = definedClasses()
    const nowStyled = [...KNOWN_UNSTYLED].filter(c => defined.has(c))
    expect(nowStyled, `now styled — remove from KNOWN_UNSTYLED: ${nowStyled.join(', ')}`).toEqual([])
  })

  it('finds the classes it is supposed to be scanning (guards the scanner itself)', () => {
    // A regex that silently stops matching would make this suite vacuously green,
    // so pin two classes that must always be present.
    const used = usedClasses()
    expect([...used.keys()]).toContain('mc-cmt-rect')
    expect([...used.keys()]).toContain('mc-art-toolbar')
  })
})
