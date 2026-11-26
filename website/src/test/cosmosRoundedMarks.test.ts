import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect } from 'vitest'

import { ROUNDED, cosmosRoundedMarksPlugin, roundMarks } from '../../scripts/cosmos-rounded-marks.mjs'

const COSMOS = path.resolve(__dirname, '../../node_modules/@cosmos.gl/graph/dist/index.js')

describe('rounded square and triangle marks in cosmos.gl', () => {
  it('rewrites the installed shader once, and leaves a patched one alone', () => {
    const code = readFileSync(COSMOS, 'utf8')
    const out = roundMarks(code)
    expect(out).toContain(ROUNDED)
    expect(out).not.toContain('if (shape == SQUARE) return squareDistance(p);')
    expect(roundMarks(out)).toBe(out)
  })

  it('fails the build when the shader it rewrites is gone', () => {
    expect(() => roundMarks('void main() {}')).toThrow(/cosmos-rounded-marks/)
  })

  it('touches only cosmos.gl', () => {
    const plugin = cosmosRoundedMarksPlugin()
    expect(plugin.transform('x', '/repo/node_modules/other/dist/index.js')).toBeNull()
    expect(() => plugin.transform('x', '/repo/node_modules/@cosmos.gl/graph/dist/index.js')).toThrow()
  })
})
