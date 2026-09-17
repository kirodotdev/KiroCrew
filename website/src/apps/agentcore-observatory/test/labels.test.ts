import { describe, it, expect } from 'vitest'

import { rowBadges, rowKey, rowName } from '../labels'

/**
 * Rows with the shape a real `list-agent-runtime-versions` response returns, with
 * the account id and runtime name replaced by placeholders.
 *
 * Two properties of this shape are what the row helpers exist to survive, and both
 * were observed live rather than read off the API reference: every version repeats
 * the SAME `agentRuntimeName`, and every version repeats the SAME `agentRuntimeArn`
 * with no `:version` suffix — even though the reference documents that suffix on
 * the field. Only `agentRuntimeVersion` differs.
 */
const VERSION_ROWS: Record<string, unknown>[] = [
  {
    agentRuntimeArn:
      'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/orders_agent-AbCdEf1234',
    agentRuntimeId: 'orders_agent-AbCdEf1234',
    agentRuntimeVersion: '13',
    agentRuntimeName: 'orders_agent',
    status: 'READY',
  },
  {
    agentRuntimeArn:
      'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/orders_agent-AbCdEf1234',
    agentRuntimeId: 'orders_agent-AbCdEf1234',
    agentRuntimeVersion: '12',
    agentRuntimeName: 'orders_agent',
    status: 'READY',
  },
  {
    agentRuntimeArn:
      'arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/orders_agent-AbCdEf1234',
    agentRuntimeId: 'orders_agent-AbCdEf1234',
    agentRuntimeVersion: '11',
    agentRuntimeName: 'orders_agent',
    status: 'READY',
  },
]

describe('rowKey', () => {
  it('gives every version of one runtime a distinct key', () => {
    const keys = VERSION_ROWS.map((row, i) => rowKey(row, i))
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('does not collapse rows that share a name and an ARN', () => {
    // The regression this pins: keying on `agentRuntimeArn` returned one identity
    // for all thirteen versions, so expanding one row expanded every row.
    const a = rowKey(VERSION_ROWS[0], 0)
    const b = rowKey(VERSION_ROWS[1], 1)
    expect(a).not.toBe(b)
  })

  it('stays unique when a row carries no name and no badges', () => {
    const rows = [{}, {}, {}]
    const keys = rows.map((row, i) => rowKey(row, i))
    expect(new Set(keys).size).toBe(3)
  })
})

describe('rowBadges', () => {
  it('prefixes a version so a bare number cannot read as a count', () => {
    expect(rowBadges(VERSION_ROWS[0])).toContain('v13')
  })

  it('keeps a non-version badge unprefixed', () => {
    expect(rowBadges(VERSION_ROWS[0])).toContain('READY')
  })

  it('returns nothing for a row with no badge fields', () => {
    expect(rowBadges({ agentRuntimeName: 'x' })).toEqual([])
  })

  it('skips a badge field that is present but not a string', () => {
    expect(rowBadges({ agentRuntimeVersion: 13 })).toEqual([])
  })
})

describe('rowName', () => {
  it('reads the name a version row carries', () => {
    expect(rowName(VERSION_ROWS[0])).toBe('orders_agent')
  })

  it('returns empty when the row carries none of the candidate fields', () => {
    expect(rowName({ unrelated: 'x' })).toBe('')
  })
})
