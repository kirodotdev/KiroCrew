import { describe, it, expect, beforeEach } from 'vitest'
import type { InstanceView } from '../api/client'
import type { WarmConn } from '../store/instancesSlice'
import {
  AUTO_CONNECT_KEY,
  autoConnectEnabled,
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
    expect(selectAutoConnectTargets(list, {}, 5)).toEqual(['a', 'b', 'c'])
  })

  it('skips a crew that is warm AND polled connected (live)', () => {
    const list = [inst('a', 'connected'), inst('b', 'disconnected')]
    expect(selectAutoConnectTargets(list, warmOf('a'), 5)).toEqual(['b'])
  })

  it('retries a warm-but-dropped tunnel (warm entry, status not connected)', () => {
    // A mid-session drop flips status but leaves the stale warm entry — mirror
    // useSelectInstance and re-attempt, so a dead tunnel comes back.
    const list = [inst('a', 'error')]
    expect(selectAutoConnectTargets(list, warmOf('a'), 5)).toEqual(['a'])
  })

  it('retries connected-but-not-warm (status says connected, no token yet)', () => {
    const list = [inst('a', 'connected')]
    expect(selectAutoConnectTargets(list, {}, 5)).toEqual(['a'])
  })

  it('never exceeds the warm-set cap', () => {
    const list = [inst('a'), inst('b'), inst('c'), inst('d')]
    expect(selectAutoConnectTargets(list, {}, 2)).toEqual(['a', 'b'])
  })

  it('subtracts already-live panes from the cap budget', () => {
    // 2 live (a, b) against cap 3 leaves budget 1 -> only the first non-live.
    const list = [inst('a', 'connected'), inst('b', 'connected'), inst('c'), inst('d')]
    expect(selectAutoConnectTargets(list, warmOf('a', 'b'), 3)).toEqual(['c'])
  })

  it('targets nothing when live panes already fill the cap', () => {
    const list = [inst('a', 'connected'), inst('b', 'connected'), inst('c')]
    expect(selectAutoConnectTargets(list, warmOf('a', 'b'), 2)).toEqual([])
  })

  it('skips a fargate crew: no pane to warm, so no connect to spend on it', () => {
    // Same list shape as the all-target case, so the only thing that removes
    // 'f' is its connection method -- not its position, status, or the cap.
    const fargate = { ...inst('f'), connection_method: 'fargate' as const }
    const list = [inst('a'), fargate, inst('c')]
    expect(selectAutoConnectTargets(list, {}, 5)).toEqual(['a', 'c'])
    // Nor does it occupy a budget slot: cap 2 still reaches both real panes.
    expect(selectAutoConnectTargets(list, {}, 2)).toEqual(['a', 'c'])
  })
})

describe('selectAutoConnectTargets with a disabled crew', () => {
  it('never targets a crew the owner turned off', () => {
    const list = [{ ...inst('a', 'disconnected'), disabled: true }, inst('b', 'disconnected')]
    expect(selectAutoConnectTargets(list, {}, 5)).toEqual(['b'])
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
