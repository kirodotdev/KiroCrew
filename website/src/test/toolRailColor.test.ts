import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { toolRailColor, type ToolRailFlags } from '../utils/toolRailColor'

const none: ToolRailFlags = { isRejected: false, isAutoDenied: false, isDone: false, hasPendingPerm: false }
const mix = (v: string) => `color-mix(in srgb, var(--${v}) 70%, transparent)`

describe('toolRailColor', () => {
  it('maps each single status to its theme colour at a 70% mix', () => {
    expect(toolRailColor({ ...none, isRejected: true })).toBe(mix('danger'))
    expect(toolRailColor({ ...none, isAutoDenied: true })).toBe(mix('warn'))
    expect(toolRailColor({ ...none, isDone: true })).toBe(mix('ok'))
    expect(toolRailColor({ ...none, hasPendingPerm: true })).toBe(mix('warn'))
    expect(toolRailColor(none)).toBe(mix('accent'))
  })

  it('applies rejected > auto-denied > done > awaiting approval > running when flags overlap', () => {
    const all: ToolRailFlags = { isRejected: true, isAutoDenied: true, isDone: true, hasPendingPerm: true }
    expect(toolRailColor(all)).toBe(mix('danger'))
    expect(toolRailColor({ ...all, isRejected: false })).toBe(mix('warn'))
    // Done outranks a pending approval: a finished row is green, not amber.
    expect(toolRailColor({ ...none, isDone: true, hasPendingPerm: true })).toBe(mix('ok'))
    // Rejected outranks done, and is not gated on it: gating is the host's call.
    expect(toolRailColor({ ...none, isRejected: true, isDone: false })).toBe(mix('danger'))
    expect(toolRailColor({ ...none, isAutoDenied: true, isDone: true })).toBe(mix('warn'))
  })
})

// Both tool rows must take the rail colour from the one helper. A copied
// mapping at either call site is exactly the drift the helper exists to stop,
// so this reddens if either file builds its own `color-mix` for the rail again.
describe('tool row hosts share the rail mapping', () => {
  const read = (...p: string[]) => readFileSync(join(__dirname, '..', ...p), 'utf8')
  const hosts: [string, string][] = [
    ['app-sdk/messageRenderers.tsx', read('app-sdk', 'messageRenderers.tsx')],
    ['pages/chat/ToolCallLine.tsx', read('pages', 'chat', 'ToolCallLine.tsx')],
  ]

  it.each(hosts)('%s calls toolRailColor and keeps no copy of the mix', (_name, src) => {
    expect(src).toMatch(/import \{ toolRailColor \} from '(\.\.\/)+utils\/toolRailColor'/)
    expect(src).toMatch(/toolRailColor\(\{/)
    expect(src).not.toMatch(/color-mix\(in srgb,[^)]*70%/)
  })

  it('the helper stays free of store and router imports', () => {
    const src = read('utils', 'toolRailColor.ts')
    expect(src).not.toMatch(/^\s*import\s/m)
  })
})
