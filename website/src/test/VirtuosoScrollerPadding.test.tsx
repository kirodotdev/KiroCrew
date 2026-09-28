// Guard: no <Virtuoso> scroller may carry its own padding.
//
// react-virtuoso renders Scroller > Viewport > List > Item, and gives the
// viewport `{ position: 'absolute', top: 0, height: '100%', width: '100%' }`
// (`Jt` in react-virtuoso/dist/index.mjs) inside a `position: relative`
// scroller. Two consequences, both measured in a browser:
//
//   • Horizontal — the viewport sets no `left`, so it falls back to the static
//     position and DOES respect `padding-left`, while `width: 100%` resolves
//     against the scroller's PADDING box. A `px-2` scroller therefore renders
//     rows 16px wider than the visible column, shifted 8px right: on a 390px
//     pane a row spans [8, 398]. The left border still lines up, so the symptom
//     reads as "the card's right border is missing", not as a padding bug — and
//     it only appears once a list crosses its virtualization threshold, so the
//     non-virtualized branch above it looks correct.
//   • Vertical — the viewport pins `top: 0`, so scroller `pt-*` / `py-*`
//     renders zero pixels. It is not a smaller gap, it is no gap.
//
// Put the inset on the row (`itemContent`) or on the List component instead.
// `LogsPage` is the reference: padding lives on the row, scroller has none.
//
// This is a source-level guard on purpose. Every test in this repo mocks
// react-virtuoso away (it measures 0 height without real layout), so no
// rendering test in this suite can observe the real viewport element — and the
// defect is a property of the call site's className, which is statically
// visible. It covers call sites that do not exist yet, which is the point.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = join(__dirname, '..')

/** Tailwind utilities that put HORIZONTAL padding on an element, plus the
 *  vertical ones the pinned `top: 0` silently discards. `p-<n>` covers both
 *  axes. Arbitrary values (`px-[10px]`) are included via the `\[` alternative. */
const PADDING_UTILITY = /(?:^|\s)(?:p|px|pl|pr|pt|pb|py)-(?:\d|\[)/

function tsxFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir)) {
    // The test directory itself mocks Virtuoso, so its className strings are
    // not real call sites.
    if (entry === 'test' || entry === 'node_modules') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...tsxFiles(full))
    else if (full.endsWith('.tsx')) out.push(full)
  }
  return out
}

/** Every `<Virtuoso …>` opening tag in `source`, as raw text. */
function virtuosoTags(source: string): string[] {
  const tags: string[] = []
  // `<Virtuoso` followed by a non-identifier char, so <VirtuosoMasonry> (a
  // different component, with a different DOM) is not matched.
  const re = /<Virtuoso(?![A-Za-z])/g
  let m: RegExpExecArray | null
  while ((m = re.exec(source)) !== null) {
    const end = source.indexOf('>', m.index)
    tags.push(source.slice(m.index, end === -1 ? source.length : end))
  }
  return tags
}

/** The `className="…"` literal on a tag, or '' when it has none. */
function classNameOf(tag: string): string {
  return /className="([^"]*)"/.exec(tag)?.[1] ?? ''
}

describe('Virtuoso scroller padding', () => {
  const files = tsxFiles(SRC)

  it('finds the known call sites, so a broken matcher cannot pass vacuously', () => {
    const sites = files.flatMap((f) => virtuosoTags(readFileSync(f, 'utf8')))
    expect(sites.length).toBeGreaterThanOrEqual(4)
  })

  it('never puts padding on a Virtuoso scroller', () => {
    const offenders: string[] = []
    for (const file of files) {
      for (const tag of virtuosoTags(readFileSync(file, 'utf8'))) {
        const cls = classNameOf(tag)
        if (PADDING_UTILITY.test(cls)) {
          offenders.push(`${file.slice(SRC.length + 1)}: className="${cls}"`)
        }
      }
    }
    expect(offenders).toEqual([])
  })
})
