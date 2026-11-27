import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect } from 'vitest'

import { ACTIVITY_GLSL, addGraphActivity, cosmosGraphActivityPlugin } from '../../scripts/cosmos-graph-activity.mjs'
import { roundMarks } from '../../scripts/cosmos-rounded-marks.mjs'

const COSMOS = path.resolve(__dirname, '../../node_modules/@cosmos.gl/graph/dist/index.js')

describe('breathing and lit marks in cosmos.gl point shader', () => {
  it('adds the clock uniforms to the shader block and to every draw, once', () => {
    const code = readFileSync(COSMOS, 'utf8')
    const out = addGraphActivity(roundMarks(code))
    expect(out).toContain(ACTIVITY_GLSL)
    expect(out).not.toContain('kcBloom')
    expect(out).not.toContain('kcHalo')
    expect(out).toContain('setPointActivity(at, strength)')
    expect(ACTIVITY_GLSL).toContain('flash * 0.45')
    expect(out).toContain('  vec4 kcLight;\n} drawVertex;')
    expect(out).toContain('uniform vec4 kcClock;')
    expect(out).toContain('kcClock: "vec4<f32>"')
    expect(out).toContain('kcClock: t.kcActivity ? t.kcActivity.clock')
    expect(addGraphActivity(out)).toBe(out)
    // Either transform may run first.
    expect(roundMarks(addGraphActivity(code))).toBe(out)
  })

  it('fails the build when the shader it rewrites moved', () => {
    expect(() => addGraphActivity('void main() {}')).toThrow(/cosmos-graph-activity/)
  })

  it('touches only cosmos.gl', () => {
    const plugin = cosmosGraphActivityPlugin()
    expect(plugin.transform('x', '/repo/node_modules/other/dist/index.js')).toBeNull()
    expect(() => plugin.transform('x', '/repo/node_modules/@cosmos.gl/graph/dist/index.js')).toThrow()
  })
})
