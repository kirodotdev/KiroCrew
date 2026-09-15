/**
 * `narrow-viewport-required`: every menu that hosts an app-contributed row must stay
 * inside a 320px viewport.
 *
 * The row's text is app-owned and bounded only by `MAX_LABEL` (120 characters for the
 * label, and again for the app identifier), because `KEBAB_RE` constrains an app name's
 * alphabet and not its length. An uncapped menu therefore grows to its content and pushes
 * its own rows off-screen — the actions become unreachable rather than merely ugly.
 *
 * Asserted across ALL THREE hosts in one place on purpose: the same defect was fixed in
 * one host and shipped in the others, twice. A new surface that renders
 * `FileMenuItemLabel` should be added to this list.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const read = (...p: string[]) => readFileSync(join(__dirname, '..', ...p), 'utf-8')

// The cap the repo already uses for a content-sized dropdown (`RejectDropdown`).
const CAP = 'max-w-[min(420px,calc(100vw-2rem))]'

const HOSTS: Array<[string, string]> = [
  ['the folder-row overflow menu', read('apps', 'fileMenuContributions.tsx')],
  ['the MarkdownPanel overflow menu', read('components', 'MarkdownPanel.tsx')],
  ['the Pierre tree context menu', read('pierre', 'PierreWorkspaceTreeImpl.tsx')],
]

describe('contributed menu rows fit a narrow viewport', () => {
  for (const [name, src] of HOSTS) {
    it(`caps ${name}`, () => {
      expect(src).toContain(CAP)
    })
  }

  it('lets both halves of a contributed row shrink and truncate', () => {
    const src = read('apps', 'fileMenuContributions.tsx')
    const fn = src.slice(src.indexOf('export function FileMenuItemLabel'))
    const body = fn.slice(0, fn.indexOf('\n}'))
    // A flex child's default `min-width: auto` refuses to shrink below its content, so
    // `truncate` without `min-w-0` never engages and the text sets the row's width.
    const spans = [...body.matchAll(/className="([^"]*)"/g)].map(m => m[1])
    expect(spans.length).toBeGreaterThanOrEqual(2)
    for (const cls of spans) {
      expect(cls, cls).toContain('truncate')
      expect(cls, cls).toContain('min-w-0')
      // And the attribution must not be pinned at full width. Asserted on the extracted
      // classes rather than the whole body, which also mentions `shrink-0` in prose.
      expect(cls, cls).not.toContain('shrink-0')
    }
  })

  it('lets a capped MarkdownPanel row clip rather than spill', () => {
    const src = read('components', 'MarkdownPanel.tsx')
    const row = /const menuRowCls = '([^']*)'/.exec(src)
    expect(row, 'menuRowCls not found').not.toBeNull()
    expect(row![1]).toContain('min-w-0')
    expect(row![1]).toContain('overflow-hidden')
  })
})
