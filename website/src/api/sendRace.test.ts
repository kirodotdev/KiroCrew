/** The tab's send revision only moves up from a frame or a read, and a
 *  reconnect read that lands late never moves it back. A conflict answer is the
 *  host's current revision and replaces it. */
import { beforeEach, describe, expect, it } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { applySharedCrewFrame, sharedCrewKeys } from '../hooks/sharedCrew/sharedCrewSocket'
import {
  baseRevisionFor,
  clearSendRaceNotice,
  isSendRaceConflict,
  noteSendRevision,
  resetSendRaceForTests,
  sendIdempotencyKey,
  sendRaceNoticeFor,
  setSendRaceNotice,
} from './sendRace'
import type { SendRaceConflictBody, SendState } from './sharedCrew'

const winner = { revision: 2, principal: { id: 'p-a', kind: 'human' as const, display: { name: 'a', login: '', captured_at: null } }, message_id: 'm', accepted_at: 0 }

beforeEach(() => resetSendRaceForTests())

describe('send revision', () => {
  it('is unknown until learned and only moves up', () => {
    expect(baseRevisionFor('s')).toBeUndefined()
    noteSendRevision('s', 3)
    noteSendRevision('s', 1)
    noteSendRevision('s', -1)
    noteSendRevision('s', 2.5)
    expect(baseRevisionFor('s')).toBe(3)
    noteSendRevision('s', 0)
    expect(baseRevisionFor('s')).toBe(3)
  })

  it('follows the send-state frame, which never lowers the cached read', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, { type: 'shared_crew.send_state', schema_version: 1, session_key: 's', revision: 5, last: winner })
    applySharedCrewFrame(qc, { type: 'shared_crew.send_state', schema_version: 1, session_key: 's', revision: 4, last: null })
    expect(baseRevisionFor('s')).toBe(5)
    expect(qc.getQueryData<SendState>(sharedCrewKeys.sendState('s'))?.revision).toBe(5)
  })

  it('a conflict records the current revision and the notice until cleared', () => {
    const body: SendRaceConflictBody = { error: 'x', code: 'send_race', session_key: 's', current_revision: 7, winner }
    expect(isSendRaceConflict(body)).toBe(true)
    expect(isSendRaceConflict({ code: 'principal_active_limit' })).toBe(false)
    setSendRaceNotice('s', body)
    expect(baseRevisionFor('s')).toBe(7)
    expect(sendRaceNoticeFor('s')?.winner.message_id).toBe('m')
    clearSendRaceNotice('s')
    expect(sendRaceNoticeFor('s')).toBeNull()
  })

  it('a conflict answer lowers a revision from before the host started again', () => {
    noteSendRevision('s', 10)
    const body: SendRaceConflictBody = { error: 'x', code: 'send_race', session_key: 's', current_revision: 1, winner }
    setSendRaceNotice('s', body)
    // Send now reads this at the press, so it goes at the host's revision.
    expect(baseRevisionFor('s')).toBe(1)
    // A late frame or read still only moves it up from there.
    noteSendRevision('s', 0)
    expect(baseRevisionFor('s')).toBe(1)
  })

  it.each([false, true])('discards only the reset session cache (revision recorded=%s)', recorded => {
    const client = new QueryClient()
    const state: SendState = { schema_version: 1, session_key: 's', revision: 10, last: winner }
    try {
      client.setQueryData(sharedCrewKeys.sendState('s'), state)
      client.setQueryData(sharedCrewKeys.sendState('other'), { ...state, session_key: 'other' })
      if (recorded) noteSendRevision('s', 10)
      setSendRaceNotice('s', { error: 'x', code: 'send_race', session_key: 's', current_revision: 1, winner }, client)
      expect(client.getQueryData(sharedCrewKeys.sendState('s'))).toBeUndefined()
      expect(client.getQueryData(sharedCrewKeys.sendState('other'))).toMatchObject({ revision: 10 })
      applySharedCrewFrame(client, { type: 'shared_crew.send_state', ...state, revision: 2 })
      clearSendRaceNotice('s')
      expect(client.getQueryData(sharedCrewKeys.sendState('s'))).toMatchObject({ revision: 2 })
      expect(baseRevisionFor('s')).toBe(2)
    } finally {
      client.clear()
    }
  })

  it('names each send with a key the host accepts', () => {
    expect(sendIdempotencyKey('s-abc-123')).toMatch(/^[A-Za-z0-9._:-]{8,128}$/)
  })
})
