import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const src = async () => {
  const raw = await readFile(join(__dirname, '..', 'hooks', 'useAutoGrowTextarea.ts'), 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

describe('useAutoGrowTextarea', () => {
  it('declines to measure an element with no layout box', async () => {
    const s = await src()
    // Every call site of this hook is exposed: a composer mounted inside a hidden
    // pane reads scrollHeight 0, and writing that back leaves a sliver that the
    // value-keyed effect can never recover, because becoming visible is not a value
    // change. Measured on one such site: inline height 0px at 390px, 36px at 1280px.
    expect(s).toMatch(/if \(el\.scrollHeight === 0 \|\| !el\.offsetParent\) return/)
  })

  it('re-measures when the element gains a layout box', async () => {
    const s = await src()
    expect(s).toMatch(/new IntersectionObserver/)
    expect(s).toMatch(/e\.isIntersecting\)\) measure\(el, maxH\)/)
  })

  it('re-measures on a width change, and only a width change (#9979)', async () => {
    const s = await src()
    // A window resize or a pane folding beside the field changes the column the
    // text wraps in with no value change, so the value-keyed effect never runs.
    // A ResizeObserver covers it -- but `measure` writes the height that observer
    // also sees, so the callback must bail unless the WIDTH moved, or it loops.
    expect(s).toMatch(/new ResizeObserver/)
    expect(s).toMatch(/if \(width === lastWidth\) return/)
  })

  it('keeps one implementation of the measurement', async () => {
    const s = await src()
    // Both effects route through `measure`, so the guard cannot be present in one
    // path and missing from the other.
    expect((s.match(/el\.style\.height = 'auto'/g) || []).length).toBe(1)
    expect((s.match(/measure\(el, maxH\)/g) || []).length).toBe(3)
  })
})
