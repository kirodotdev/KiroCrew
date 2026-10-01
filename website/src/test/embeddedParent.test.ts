import { afterEach, describe, expect, it } from 'vitest'
import { relayPaneContext, stampPaneChannel } from '../lib/embeddedParent'
import { PANE_CHANNEL_FIELD } from '../lib/relayPaneBootstrap'

type WithCtx = { __kcRelayPaneContext?: unknown }

function setCtx(value: unknown): void {
  ;(window as unknown as WithCtx).__kcRelayPaneContext = value
}

afterEach(() => {
  delete (window as unknown as WithCtx).__kcRelayPaneContext
})

describe('relayPaneContext', () => {
  it('returns null when the bootstrap installed nothing (direct pane / top-level)', () => {
    expect(relayPaneContext()).toBeNull()
  })

  it('reads a well-formed context the pre-module bootstrap installed', () => {
    setCtx({ channel: 'ch-abc', parentOrigin: 'https://crew.example.ts.net', protocol: 1 })
    expect(relayPaneContext()).toEqual({
      channel: 'ch-abc',
      parentOrigin: 'https://crew.example.ts.net',
      protocol: 1,
    })
  })

  it('rejects a malformed/partial global rather than half-enabling relay', () => {
    for (const bad of [
      null,
      42,
      {},
      { channel: '', parentOrigin: 'https://p', protocol: 1 },
      { channel: 'c', parentOrigin: '', protocol: 1 },
      { channel: 'c', parentOrigin: 'https://p' }, // no protocol
      { channel: 'c', parentOrigin: 'https://p', protocol: '1' },
    ]) {
      setCtx(bad)
      expect(relayPaneContext()).toBeNull()
    }
  })
})

describe('stampPaneChannel', () => {
  it('is the identity in a direct pane (no context) — direct wire traffic unchanged', () => {
    const msg = { type: 'mc-embedded-ready', v: 1 }
    const out = stampPaneChannel(msg)
    expect(out).toBe(msg) // same reference: no clone, no added field
    expect(out).not.toHaveProperty(PANE_CHANNEL_FIELD)
  })

  it('stamps the per-pane channel in a relay pane, leaving the payload intact', () => {
    setCtx({ channel: 'ch-xyz', parentOrigin: 'https://p', protocol: 1 })
    const out = stampPaneChannel({ type: 'mc-unread-slots', count: 3 }) as Record<string, unknown>
    expect(out.type).toBe('mc-unread-slots')
    expect(out.count).toBe(3)
    expect(out[PANE_CHANNEL_FIELD]).toBe('ch-xyz')
  })
})
