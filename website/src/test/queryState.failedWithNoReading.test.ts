import { failedWithNoReading } from '../api/queryState'

describe('failedWithNoReading', () => {
  it('nothing read and no error yet: not failed', () => {
    expect(failedWithNoReading({ data: undefined, dataUpdatedAt: 0, errorUpdatedAt: 0 })).toBe(false)
  })

  it('a cold-cache null then an error: failed', () => {
    expect(failedWithNoReading({ data: null, dataUpdatedAt: 100, errorUpdatedAt: 200 })).toBe(true)
  })

  it('an error then a cold-cache null: not failed, the null is the latest outcome', () => {
    expect(failedWithNoReading({ data: null, dataUpdatedAt: 200, errorUpdatedAt: 100 })).toBe(false)
  })

  it('a real reading held: not failed, even after a later error', () => {
    expect(failedWithNoReading({ data: { email: 'ada@example.com' }, dataUpdatedAt: 100, errorUpdatedAt: 200 })).toBe(false)
  })
})
