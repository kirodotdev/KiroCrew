import { describe, it, expect } from 'vitest'
import {
  popoutWindowName,
  buildPopoutUrl,
  hasFreshBeacon,
  TERMINAL_POPOUT_ID,
  TERMINAL_POPOUT_CHANNEL,
} from '../utils/terminalPopout'
import { CHAT_POPOUT_CHANNEL } from '../utils/chatPopout'
import { ARTIFACT_POPOUT_CHANNEL } from '../utils/artifactPopout'

/**
 * Pure-logic tests for the terminal-popout specialization. The
 * BroadcastChannel/heartbeat engine is shared via popoutController and covered
 * by the chat/artifact popout suites — these pin what is terminal-specific:
 * the singleton entity id, the fixed window name (dedupe to ONE terminal
 * window), the URL shape, and channel isolation from the other features.
 */
describe('terminalPopout identity', () => {
  it('uses a fixed singleton entity id (the panel pops out as one unit)', () => {
    expect(TERMINAL_POPOUT_ID).toBe('terminal-panel')
  })

  it('uses a fixed window name so window.open dedupes to a single terminal window', () => {
    expect(popoutWindowName()).toBe('mc-popout-terminal')
    // Stable across calls — dedupe depends on it never varying.
    expect(popoutWindowName()).toBe(popoutWindowName())
  })

  it('builds the popout URL at /popout/terminal on the current origin', () => {
    expect(buildPopoutUrl()).toBe(`${window.location.origin}/popout/terminal`)
  })
})

describe('terminalPopout channel isolation', () => {
  it('does not share a BroadcastChannel with chat or artifact popouts', () => {
    expect(TERMINAL_POPOUT_CHANNEL).not.toBe(CHAT_POPOUT_CHANNEL)
    expect(TERMINAL_POPOUT_CHANNEL).not.toBe(ARTIFACT_POPOUT_CHANNEL)
  })
})

describe('terminalPopout liveness beacon', () => {
  // The beacon is what a freshly RELOADED main window reads SYNCHRONOUSLY to
  // know the popout owns the PTY sockets -- before the BroadcastChannel
  // handshake completes. A stale/garbage beacon must read as "no popout"
  // (crashed popout window), or the docked panel could never come back.
  const KEY = 'mc-terminal-popout-alive'
  it('is fresh for a recent heartbeat', () => {
    localStorage.setItem(KEY, String(Date.now()))
    expect(hasFreshBeacon()).toBe(true)
  })
  it('expires past the TTL (crashed popout)', () => {
    localStorage.setItem(KEY, String(Date.now() - 60_000))
    expect(hasFreshBeacon()).toBe(false)
  })
  it('is false when absent or garbage', () => {
    localStorage.removeItem(KEY)
    expect(hasFreshBeacon()).toBe(false)
    localStorage.setItem(KEY, 'not-a-number')
    expect(hasFreshBeacon()).toBe(false)
  })
})
