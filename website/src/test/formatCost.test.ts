import { describe, it, expect } from 'vitest'
import { formatCost } from '../utils/formatCost'

describe('formatCost', () => {
  it('renders two decimal places for normal amounts', () => {
    expect(formatCost(0.02)).toBe('$0.02')
    expect(formatCost(1.5)).toBe('$1.50')
    expect(formatCost(12.345)).toBe('$12.35')
  })

  it('floors non-zero dust to <$0.01 instead of leaking 4dp noise', () => {
    // 4dp dust like "$0.0004" / "$0.0023" is a precision no user decision needs.
    expect(formatCost(0.0004)).toBe('<$0.01')
    expect(formatCost(0.009)).toBe('<$0.01')
  })

  it('distinguishes an exact zero from too-small-to-show', () => {
    expect(formatCost(0)).toBe('~$0')
    expect(formatCost(0.0001)).toBe('<$0.01')
  })

  it('renders an em dash for missing or non-finite input, never $NaN', () => {
    expect(formatCost(null)).toBe('—')
    expect(formatCost(undefined)).toBe('—')
    expect(formatCost(NaN)).toBe('—')
    expect(formatCost(Infinity)).toBe('—')
  })

  it('surfaces a negative sign rather than flooring a bad value', () => {
    expect(formatCost(-2.5)).toBe('-$2.50')
    expect(formatCost(-0.001)).toBe('-<$0.01')
  })
})
