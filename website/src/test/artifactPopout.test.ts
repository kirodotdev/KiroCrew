import { describe, it, expect } from 'vitest'
import {
  applyMessage,
  pruneStale,
  popoutWindowName,
  buildPopoutUrl,
  type PopoutMap,
} from '../utils/artifactPopout'

/**
 * Pure-logic tests for the artifact-popout coordination helpers. The
 * BroadcastChannel/heartbeat wiring is intentionally not exercised here (it's
 * shared with the chat popout via popoutController and covered there) — these
 * pin the artifact-specific window-name + URL shape and the state math the main
 * window relies on to track live popouts.
 */
describe('artifactPopout.applyMessage', () => {
  it('adds an artifact on open with the current timestamp', () => {
    const next = applyMessage({}, { t: 'open', id: 'cr-queue' }, 1000)
    expect(next).toEqual({ 'cr-queue': 1000 })
  })

  it('refreshes lastSeen on pong', () => {
    const next = applyMessage({ 'cr-queue': 1000 }, { t: 'pong', id: 'cr-queue' }, 5000)
    expect(next['cr-queue']).toBe(5000)
  })

  it('removes an artifact on close', () => {
    const next = applyMessage({ 'cr-queue': 1000, 'pipeline-health': 1000 }, { t: 'close', id: 'cr-queue' }, 2000)
    expect(next).toEqual({ 'pipeline-health': 1000 })
  })

  it('returns the same reference for close of an unknown artifact (no churn)', () => {
    const map: PopoutMap = { 'cr-queue': 1000 }
    expect(applyMessage(map, { t: 'close', id: 'ghost' }, 2000)).toBe(map)
  })

  it('ignores control/heartbeat messages', () => {
    const map: PopoutMap = { 'cr-queue': 1000 }
    expect(applyMessage(map, { t: 'ping' }, 2000)).toBe(map)
    expect(applyMessage(map, { t: 'focus', id: 'cr-queue' }, 2000)).toBe(map)
    expect(applyMessage(map, { t: 'bring-back', id: 'cr-queue' }, 2000)).toBe(map)
  })
})

describe('artifactPopout.pruneStale', () => {
  it('drops entries older than the stale window', () => {
    const map: PopoutMap = { fresh: 10_000, stale: 1_000 }
    const next = pruneStale(map, 20_000, 12_000)
    expect(next).toEqual({ fresh: 10_000 })
  })

  it('keeps the same reference when nothing is stale (identity-stable)', () => {
    const map: PopoutMap = { a: 19_000, b: 20_000 }
    expect(pruneStale(map, 20_000, 12_000)).toBe(map)
  })

  it('treats an entry exactly at the boundary as still alive', () => {
    const map: PopoutMap = { edge: 8_000 }
    expect(pruneStale(map, 20_000, 12_000)).toBe(map)
  })
})

describe('artifactPopout.popoutWindowName', () => {
  it('is stable and namespaced for an artifact slug', () => {
    expect(popoutWindowName('cr-queue')).toBe('mc-artifact-popout-cr-queue')
  })

  it('sanitizes characters that are invalid in a window name', () => {
    expect(popoutWindowName('my artifact/v2')).toBe('mc-artifact-popout-my_artifact_v2')
  })
})

describe('artifactPopout.buildPopoutUrl', () => {
  const origin = window.location.origin

  it('puts the slug directly in the path (no query string)', () => {
    expect(buildPopoutUrl('cr-queue')).toBe(`${origin}/popout/artifact/cr-queue`)
  })
})
