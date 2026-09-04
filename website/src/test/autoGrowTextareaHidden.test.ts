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
    // ResizeObserver would feed back -- `measure` sets the height it would observe.
    expect(s).not.toMatch(/new ResizeObserver/)
  })

  it('keeps one implementation of the measurement', async () => {
    const s = await src()
    // Both effects route through `measure`, so the guard cannot be present in one
    // path and missing from the other.
    expect((s.match(/el\.style\.height = 'auto'/g) || []).length).toBe(1)
    expect((s.match(/measure\(el, maxH\)/g) || []).length).toBe(2)
  })
})
