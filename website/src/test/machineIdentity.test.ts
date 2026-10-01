import { describe, it, expect } from 'vitest'
// Imported from `utils/machineIdentity`, NOT from `../App`, so this pure test
// does not pull the app root's eager graph (see metricColor.test.ts).
import { machineIdentity, osDisplayName, shortHostname } from '../utils/machineIdentity'

describe('shortHostname', () => {
  it('keeps the first DNS label of a fully qualified name', () => {
    expect(shortHostname('dev-dsk-alice-1a-0123abcd.eu-west-1.example.com')).toBe('dev-dsk-alice-1a-0123abcd')
    expect(shortHostname('laptop.local')).toBe('laptop')
  })

  it('returns a single-label name unchanged', () => {
    expect(shortHostname('98dd605d9db4')).toBe('98dd605d9db4')
  })

  it('returns an IP literal whole, since its first octet names nothing', () => {
    expect(shortHostname('10.0.12.7')).toBe('10.0.12.7')
    expect(shortHostname('fe80::1')).toBe('fe80::1')
  })

  it('returns an empty name as empty', () => {
    expect(shortHostname('')).toBe('')
    expect(shortHostname('   ')).toBe('')
  })
})

describe('osDisplayName', () => {
  it('names the OS family and drops the release the backend appends', () => {
    expect(osDisplayName('Darwin 25.0.0')).toBe('macOS')
    expect(osDisplayName('Linux 6.1.112-122.189.amzn2023.x86_64')).toBe('Linux')
    expect(osDisplayName('Windows 11')).toBe('Windows')
  })

  it('passes an unmapped family through and an empty one as empty', () => {
    expect(osDisplayName('FreeBSD 14.1-RELEASE')).toBe('FreeBSD')
    expect(osDisplayName('')).toBe('')
  })
})

describe('machineIdentity', () => {
  it('derives the short host, OS name and core count from an ordinary frame', () => {
    expect(machineIdentity('host-a.eu-west-1.example.com', 'Linux 6.1.0', 96)).toEqual({
      host: 'host-a',
      fullHost: 'host-a.eu-west-1.example.com',
      os: 'Linux',
      cores: 96,
    })
  })

  it('drops a field the untyped payload does not prove, keeping the rest', () => {
    expect(machineIdentity(undefined, 'Darwin 25.0.0', 10)).toEqual({ host: '', fullHost: '', os: 'macOS', cores: 10 })
    expect(machineIdentity('box', 42, 0)).toEqual({ host: 'box', fullHost: 'box', os: '', cores: 0 })
    expect(machineIdentity('box', 'Linux 6', 2.5)).toEqual({ host: 'box', fullHost: 'box', os: 'Linux', cores: 0 })
    expect(machineIdentity('box', 'Linux 6', '8')).toEqual({ host: 'box', fullHost: 'box', os: 'Linux', cores: 0 })
  })

  it('returns null when the frame names nothing at all', () => {
    expect(machineIdentity(undefined, undefined, undefined)).toBeNull()
    expect(machineIdentity('', '', 0)).toBeNull()
  })
})
