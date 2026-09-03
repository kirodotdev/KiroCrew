import { describe, it, expect, beforeEach } from 'vitest'
import type { InstanceView } from '../api/client'
import type { WarmConn } from '../store/instancesSlice'
import {
  AUTO_CONNECT_KEY,
  AUTO_CONNECT_EXCLUDE_KEY,
  autoConnectEnabled,
  readAutoConnectExcludes,
  selectAutoConnectTargets,
} from './useAutoConnectInstances'

/** Minimal InstanceView factory — only the fields the picker reads matter. */
function inst(id: string, state?: string): InstanceView {
  return {
    id,
    name: id,
    ssh_host: id,
    remote_port: 0,
    local_port: 0,
    ttl: '',
    remote_bin: '',
    connection_method: 'ssh',
    ssm_target: '',
    aws_profile: '',
    aws_region: '',
    ssm_run_as: '',
    was_connected: false,
    status: { state } as InstanceView['status'],
  }
}

const warmOf = (...ids: string[]): Record<string, WarmConn> =>
  Object.fromEntries(ids.map(id => [id, { port: 1, token: 't' }]))

describe('selectAutoConnectTargets', () => {
  it('targets every crew when none is live', () => {
    const list = [inst('a'), inst('b'), inst('c')]
    expect(selectAutoConnectTargets(list, {}, new Set(), 5)).toEqual(['a', 'b', 'c'])
  })

  it('skips a crew that is warm AND polled connected (live)', () => {
    const list = [inst('a', 'connected'), inst('b', 'disconnected')]
    expect(selectAutoConnectTargets(list, warmOf('a'), new Set(), 5)).toEqual(['b'])
  })

  it('retries a warm-but-dropped tunnel (warm entry, status not connected)', () => {
    // A mid-session drop flips status but leaves the stale warm entry — mirror
    // useSelectInstance and re-attempt, so a dead tunnel comes back.
    const list = [inst('a', 'error')]
    expect(selectAutoConnectTargets(list, warmOf('a'), new Set(), 5)).toEqual(['a'])
  })

  it('retries connected-but-not-warm (status says connected, no token yet)', () => {
    const list = [inst('a', 'connected')]
    expect(selectAutoConnectTargets(list, {}, new Set(), 5)).toEqual(['a'])
  })

  it('honors the per-instance opt-out', () => {
    const list = [inst('a'), inst('b')]
    expect(selectAutoConnectTargets(list, {}, new Set(['a']), 5)).toEqual(['b'])
  })

  it('never exceeds the warm-set cap', () => {
    const list = [inst('a'), inst('b'), inst('c'), inst('d')]
    expect(selectAutoConnectTargets(list, {}, new Set(), 2)).toEqual(['a', 'b'])
  })

  it('subtracts already-live panes from the cap budget', () => {
    // 2 live (a, b) against cap 3 leaves budget 1 -> only the first non-live.
    const list = [inst('a', 'connected'), inst('b', 'connected'), inst('c'), inst('d')]
    expect(selectAutoConnectTargets(list, warmOf('a', 'b'), new Set(), 3)).toEqual(['c'])
  })

  it('targets nothing when live panes already fill the cap', () => {
    const list = [inst('a', 'connected'), inst('b', 'connected'), inst('c')]
    expect(selectAutoConnectTargets(list, warmOf('a', 'b'), new Set(), 2)).toEqual([])
  })
})

describe('autoConnectEnabled', () => {
  beforeEach(() => localStorage.clear())

  it('defaults to on when the key is absent', () => {
    expect(autoConnectEnabled()).toBe(true)
  })

  it('is off only for the explicit "0" value', () => {
    localStorage.setItem(AUTO_CONNECT_KEY, '0')
    expect(autoConnectEnabled()).toBe(false)
    localStorage.setItem(AUTO_CONNECT_KEY, '1')
    expect(autoConnectEnabled()).toBe(true)
  })
})

describe('readAutoConnectExcludes', () => {
  beforeEach(() => localStorage.clear())

  it('is empty when unset', () => {
    expect(readAutoConnectExcludes().size).toBe(0)
  })

  it('parses a JSON string array of ids', () => {
    localStorage.setItem(AUTO_CONNECT_EXCLUDE_KEY, JSON.stringify(['x', 'y']))
    const s = readAutoConnectExcludes()
    expect(s.has('x')).toBe(true)
    expect(s.has('y')).toBe(true)
  })

  it('tolerates corrupt / non-array values without throwing', () => {
    localStorage.setItem(AUTO_CONNECT_EXCLUDE_KEY, '{not json')
    expect(readAutoConnectExcludes().size).toBe(0)
    localStorage.setItem(AUTO_CONNECT_EXCLUDE_KEY, '{"a":1}')
    expect(readAutoConnectExcludes().size).toBe(0)
  })
})
