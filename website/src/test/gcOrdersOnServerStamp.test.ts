import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const source = readFileSync(resolve(__dirname, '../App.tsx'), 'utf-8')

describe('the storage-GC ordering reads the server stamp', () => {
  it('never pairs the local churn counter with the server epoch', () => {
    expect(source).not.toMatch(/generation: d\.slotsGeneration/)
  })

  it('passes the server-stamped generation at both order sources', () => {
    const uses = source.match(/generation: d\.lastSlotsGeneration/g) ?? []

    expect(uses.length).toBe(2)
  })
})
