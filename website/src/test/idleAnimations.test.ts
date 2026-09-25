import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/** Regression (#13729): an infinite CSS animation on an always-visible
 *  indicator keeps Chromium producing frames at the display refresh rate
 *  (~74 frames/s measured on an idle dashboard), which keeps the renderer, GPU
 *  and Electron main processes awake and drains the battery. The resource
 *  posture badge sits in the top bar for as long as the host is `tight`, which
 *  on a loaded laptop is most of the time; the Developer rail dot shows for as
 *  long as developer mode is on and the page has not been visited; the rail's
 *  agent-count glyph shows for as long as a count does; the gateway dot pulses
 *  for as long as the dashboard cannot reach its gateway. None of them may carry
 *  an unbounded animation: a pulse that draws the eye is bounded to a few
 *  cycles with an `!important` `animation-iteration-count` longhand. The
 *  important flag is what makes the bound independent of the order in which
 *  Tailwind emits `.animate-pulse` and the arbitrary property: without it, the
 *  shorthand's `infinite` wins whenever the pulse rule comes later. */

const SRC = join(__dirname, '..')
const APP = readFileSync(join(SRC, 'App.tsx'), 'utf8')

const INFINITE_UTILITIES = /\banimate-(pulse|ping|bounce)\b/
const BOUNDED = '[animation-iteration-count:3]!'

function sliceAfter(marker: string, length: number): string {
  const at = APP.indexOf(marker)
  expect(at, `marker not found in App.tsx: ${marker}`).toBeGreaterThan(-1)
  return APP.slice(at, at + length)
}

/** Every `animate-pulse` in `block` must sit in a class string that also
 *  carries the bounded iteration count. */
function expectPulsesBounded(block: string) {
  const classStrings = block.match(/'[^']*\banimate-pulse\b[^']*'/g) ?? []
  expect(classStrings.length, `no animate-pulse class string found in: ${block.slice(0, 120)}`).toBeGreaterThan(0)
  for (const cls of classStrings) expect(cls, `unbounded pulse: ${cls}`).toContain(BOUNDED)
}

describe('always-visible indicators do not animate forever', () => {
  it('resource posture badge: static for tight, bounded pulse for critical', () => {
    const block = sliceAfter('key="resource-health"', 900)
    expect(block).toContain("'bg-danger animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none' : 'bg-warn'")
    expectPulsesBounded(block)
  })

  it('gateway connection dot pulses a bounded number of times while offline', () => {
    const block = sliceAfter('key="conn"', 900)
    expect(block).toContain("offline ? 'bg-danger animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none'")
    expectPulsesBounded(block)
  })

  it('Developer rail dot pulses a bounded number of times in both rail widths', () => {
    // Both rail-width variants carry `animate-pulse`; the one element that
    // renders either of them appends the bound.
    const block = sliceAfter('const dotClass = effectiveCollapsed', 900)
    expect(block.match(/\banimate-pulse\b/g)).toHaveLength(2)
    expect(block).toContain('<span className={`${dotClass} [animation-iteration-count:3]!`} />')
    expect(block.match(/className=\{dotClass\}/g)).toBeNull()
  })

  it('the bound compiles to an !important longhand, so emission order cannot undo it', async () => {
    const { compile } = await import('@tailwindcss/node')
    const compiler = await compile('@import "tailwindcss";', { base: join(SRC, '..'), onDependency: () => {} })
    const css = compiler.build(['animate-pulse', BOUNDED])
    expect(css).toMatch(/\.animate-pulse\s*\{\s*animation:\s*var\(--animate-pulse\);/)
    expect(css).toMatch(/animation-iteration-count:\s*3\s*!important;/)
  })

  it('rail agent-count glyph is static', () => {
    expect(APP).toContain('<Bot size={11} aria-hidden />')
    expect(APP).not.toMatch(/<Bot size=\{11\} className="animate-pulse"/)
  })

  it('no always-visible indicator carries an unbounded infinite animation', () => {
    for (const marker of ['key="conn"', 'key="resource-health"', 'function ActivityIndicator(']) {
      const block = sliceAfter(marker, 1200)
      for (const cls of block.match(/'[^']*'|"[^"]*"/g) ?? []) {
        if (INFINITE_UTILITIES.test(cls)) expect(cls, `unbounded infinite animation near ${marker}: ${cls}`).toContain(BOUNDED)
      }
    }
  })
})
