/**
 * Tests for the postMessage origin guard (src/lib/tunnelOrigin.ts) — the
 * security-critical filter that decides whether an embedded frame's unread
 * relay is trusted (§5.4). Pins down that only loopback origins
 * (127.0.0.1 / localhost / *.localhost) on a known warm port are accepted.
 */
import { describe, it, expect } from 'vitest'
import { parseLoopbackOriginPort, resolveTunnelOrigin } from '../lib/tunnelOrigin'

describe('parseLoopbackOriginPort', () => {
  it('accepts loopback http origins with valid ports', () => {
    expect(parseLoopbackOriginPort('http://127.0.0.1:7778')).toBe(7778)
    expect(parseLoopbackOriginPort('http://127.0.0.1:65535')).toBe(65535)
    expect(parseLoopbackOriginPort('http://127.0.0.1:1')).toBe(1)
    expect(parseLoopbackOriginPort('http://localhost:7778')).toBe(7778)
    expect(parseLoopbackOriginPort('http://kirocrew.localhost:7778')).toBe(7778)
  })

  it('rejects https, non-loopback hosts, paths, and out-of-range ports', () => {
    for (const bad of [
      'https://127.0.0.1:7778',
      'https://localhost:7778',
      'http://localhost.evil.com:7778',
      'http://evil.localhost.com:7778',
      'http://127.0.0.1:7778/x',
      'http://127.0.0.1:99999',
      'http://127.0.0.1:0',
      'http://127.0.0.1',
      'http://evil.com:7778',
      'http://127.0.0.1:7778 ',
      '',
      'null',
    ]) {
      expect(parseLoopbackOriginPort(bad)).toBeNull()
    }
  })

  // non-string input is defensively rejected
  it('rejects non-string input', () => {
    // @ts-expect-error intentional bad input
    expect(parseLoopbackOriginPort(undefined)).toBeNull()
  })
})

describe('resolveTunnelOrigin', () => {
  const portToId = new Map<number, string>([
    [7778, 'cd-1'],
    [7779, 'cd-2'],
  ])

  it('maps a known loopback origin to its instance id', () => {
    expect(resolveTunnelOrigin('http://127.0.0.1:7778', portToId)).toBe('cd-1')
    expect(resolveTunnelOrigin('http://127.0.0.1:7779', portToId)).toBe('cd-2')
  })

  it('maps loopback hostnames (localhost / *.localhost) to their instance id', () => {
    expect(resolveTunnelOrigin('http://localhost:7778', portToId)).toBe('cd-1')
    expect(resolveTunnelOrigin('http://kirocrew.localhost:7779', portToId)).toBe('cd-2')
  })

  it('returns null for a valid loopback port we do not own', () => {
    expect(resolveTunnelOrigin('http://127.0.0.1:9999', portToId)).toBeNull()
  })

  it('returns null for a non-loopback origin', () => {
    expect(resolveTunnelOrigin('https://127.0.0.1:7778', portToId)).toBeNull()
    expect(resolveTunnelOrigin('http://evil.com:7778', portToId)).toBeNull()
  })
})
