import { describe, it, expect, vi } from 'vitest'
import { paneRelayCrews, parsePaneRelayCrews, pickRelayFromPane } from './paneRelayCrews'
import type { RelayCrewEntry } from '../api/client'

const entry = (over: Partial<RelayCrewEntry> = {}): RelayCrewEntry => ({
  endpointId: 'ep-1', name: 'Lab', crewType: 'TEAM', presence: 'CONNECTED', relayConnectionStatus: 'CONNECTED',
  crewUrl: 'https://crew.example/', unseen: true, ...over,
})

describe('paneRelayCrews', () => {
  it('sends each row its state and never the Crew URL', () => {
    const rows = paneRelayCrews([entry(), entry({ endpointId: 'ep-2', name: 'Build' })], [{ id: 'ep-2', name: 'Build' }], 'ep-2')
    expect(rows).toEqual([
      { endpointId: 'ep-1', name: 'Lab', crewType: 'TEAM', presence: 'CONNECTED', relayConnectionStatus: 'CONNECTED', lastSeenAt: null, open: false, current: false },
      { endpointId: 'ep-2', name: 'Build', crewType: 'TEAM', presence: 'CONNECTED', relayConnectionStatus: 'CONNECTED', lastSeenAt: null, open: true, current: true },
    ])
  })

  it('narrows an untrusted payload element by element', () => {
    expect(parsePaneRelayCrews(undefined)).toEqual([])
    expect(parsePaneRelayCrews([null, 3, { name: 'no id' }, { endpointId: 'ep-1', name: 7, open: 'yes', lastSeenAt: 'x' }])).toEqual([
      { endpointId: 'ep-1', name: '', crewType: '', presence: '', relayConnectionStatus: '', lastSeenAt: null, open: false, current: false },
    ])
  })

  it('opens only a listed Crew, brings an open tab forward, and ignores other ids', () => {
    const dispatch = vi.fn()
    const open = vi.fn()
    expect(pickRelayFromPane('cd-1', [entry()], [], dispatch, open)).toBe(false)
    expect(pickRelayFromPane(null, [entry()], [], dispatch, open)).toBe(false)
    expect(pickRelayFromPane('relay:ghost', [entry()], [], dispatch, open)).toBe(true)
    expect(open).not.toHaveBeenCalled()
    pickRelayFromPane('relay:ep-1', [entry()], [], dispatch, open)
    expect(open).toHaveBeenCalledWith('ep-1')
    expect(dispatch).not.toHaveBeenCalled()
    pickRelayFromPane('relay:ep-2', [], [{ id: 'ep-2', name: 'Build' }], dispatch, open)
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ payload: { id: 'ep-2', name: 'Build' } }))
    expect(open).toHaveBeenCalledTimes(1)
  })
})
