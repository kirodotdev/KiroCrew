import { describe, it, expect } from 'vitest'
import { withCreatedReceipt } from './MembersPage'

const receipt = (name: string) => ({ name, goal: 'Watch the queue', schedule: 'none' as const })

describe('withCreatedReceipt', () => {
  it('stores a receipt for an origin named __proto__ as its own entry', () => {
    const next = withCreatedReceipt(Object.create(null), '__proto__', receipt('Scout'))
    expect(Object.prototype.hasOwnProperty.call(next, '__proto__')).toBe(true)
    expect(next['__proto__']).toEqual(receipt('Scout'))
    expect(Object.keys(next)).toEqual(['__proto__'])
  })

  it('moves an existing origin to the newest slot and leaves the input untouched', () => {
    const prev = withCreatedReceipt(withCreatedReceipt(Object.create(null), 'a', receipt('A')), 'b', receipt('B'))
    const next = withCreatedReceipt(prev, 'a', receipt('A2'))
    expect(Object.keys(next)).toEqual(['b', 'a'])
    expect(next.a.name).toBe('A2')
    expect(prev.a.name).toBe('A')
  })
})
