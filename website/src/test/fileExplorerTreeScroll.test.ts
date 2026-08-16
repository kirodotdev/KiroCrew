import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const raw = () => readFile(join(__dirname, '..', 'apps', 'file-explorer', 'styles.ts'), 'utf8')
// Strip comments: the rules are explained in prose that quotes the same selectors.
const css = async () => (await raw()).replace(/\/\*[\s\S]*?\*\//g, '')

describe('file-explorer tree pane while stacked', () => {
  it('fills the column and can shrink, so its own overflow-y engages', async () => {
    const s = await css()
    // `flex-shrink:0` (its row default) makes the pane take CONTENT height in a
    // column -- measured 1553px inside a 718px split -- so `overflow-y:auto`
    // never engages and the split's `overflow:hidden` clips the rest.
    expect(s).toMatch(/\.mc-fe-split\.is-stacked > \.mc-fe-left \{ flex:1 1 0%; min-height:0; \}/)
  })

  it('keeps the pane its own scroll container', async () => {
    const s = await css()
    expect(s).toMatch(/\.mc-fe-left \{ flex-shrink:0; min-height:0; overflow-y:auto/)
  })

  it('still turns the divider with the axis', async () => {
    const s = await css()
    expect(s).toMatch(/is-stacked > \.mc-fe-left \{ border-right:none; border-bottom:/)
  })
})
