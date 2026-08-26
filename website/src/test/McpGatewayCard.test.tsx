import { describe, it, expect } from 'vitest'
import { backendRowKey } from '../pages/McpGatewayCard'

describe('backendRowKey', () => {
  it('uses the pid when present', () => {
    expect(backendRowKey({ server: 'slack-mcp', pid: 1234 }, 0)).toBe('slack-mcp-1234')
  })

  it('falls back to the row index when pid is null so keys stay unique', () => {
    // Two just-spawned backends of the same server both have pid=null; keying on
    // pid alone would collide to "kirocrew-core-null" and trigger React key warnings.
    const a = backendRowKey({ server: 'kirocrew-core', pid: null }, 0)
    const b = backendRowKey({ server: 'kirocrew-core', pid: null }, 1)
    expect(a).toBe('kirocrew-core-i0')
    expect(b).toBe('kirocrew-core-i1')
    expect(a).not.toBe(b)
  })
})

import { formatKb } from '../pages/McpGatewayCard'

describe('formatKb', () => {
  it('returns em-dash for non-positive', () => {
    expect(formatKb(0)).toBe('—')
    expect(formatKb(-5)).toBe('—')
  })
  it('keeps KB and MB as integers', () => {
    expect(formatKb(512)).toBe('512 KB')
    expect(formatKb(231 * 1024)).toBe('231 MB')
  })
  it('promotes to GB/TB with one decimal', () => {
    expect(formatKb(2912 * 1024)).toBe('2.8 GB')   // Pool RAM
    expect(formatKb(6086 * 1024)).toBe('5.9 GB')   // Without gateway
    expect(formatKb(1024 * 1024 * 1024)).toBe('1.0 TB')
  })
  it('promotes when rounding would otherwise display 1024', () => {
    // 1023.999… MB rounds to "1024 MB" -> must promote to "1.0 GB"
    expect(formatKb(1024 * 1024 - 1)).toBe('1.0 GB')
  })
})
