/**
 * Regression + contract tests for the R3 pane messaging + endpoint authority
 * (src/lib/paneChannel.ts).
 *
 * This is the single place that decides, for a Remote Crew pane: which access
 * mode the parent's origin dictates, how a resolved endpoint is turned into an
 * iframe `src`, how a downward postMessage is addressed, and how an inbound
 * message is attributed to an instance. Two transports share the seam:
 *
 *   - **direct-loopback** (desktop / localhost): the historical per-port model,
 *     unchanged — an exact loopback origin both addresses and validates.
 *   - **same-origin-relay** (published HTTPS): the pane is a sandboxed opaque
 *     iframe under the hub's own origin at a capability `documentPath`. Its
 *     `event.origin` is the string `'null'` and is NEVER used as authentication;
 *     a message is bound to the exact `contentWindow` AND the random per-pane
 *     `channel`. A downward post targets the exact frame with `'*'` (an opaque
 *     origin never matches a concrete target string) and is channel-stamped.
 *
 * Observable contract this owns (test-audit authoring gate):
 *   1. Endpoint variant parsing: the issuer JSON parses to exactly one typed
 *      `PaneEndpoint`, or `null`; a relay endpoint never carries a port/token.
 *   2. Relay `src` is the same-origin `documentPath` — never a `host:port` URL
 *      and never the remote token (incident kc-46d84a stays fixed).
 *   3. Direct behaviour is byte-identical to the pre-R3 authority.
 *   4. Attribution: relay binds source+channel for EVERY message family and
 *      rejects a stale/foreign frame or a stale channel; `event.origin==='null'`
 *      is never trusted. Direct still validates by exact loopback origin.
 *   5. Downward envelope: relay stamps the channel and targets '*'; direct
 *      targets the exact origin and adds nothing.
 */
import { describe, it, expect } from 'vitest'
import { PANE_CHANNEL_FIELD } from '../lib/relayPaneBootstrap'
import {
  resolvePaneMode,
  paneAccessFor,
  parsePaneEndpoint,
  paneEndpointSrc,
  paneMessageEnvelope,
  relayRenewDelayMs,
  RELAY_LEASE_RENEW_MARGIN_MS,
  resolvePaneMessage,
  resolvePaneBootstrapRequest,
  sameEndpoint,
  type PaneEndpoint,
  type PaneMode,
} from '../lib/paneChannel'

const directMode: PaneMode = resolvePaneMode({
  protocol: 'http:',
  hostname: 'localhost',
  origin: 'http://localhost:4517',
})
const relayMode: PaneMode = resolvePaneMode({
  protocol: 'https:',
  hostname: 'crew.example.ts.net',
  origin: 'https://crew.example.ts.net',
})

const directEndpoint: PaneEndpoint = { kind: 'direct-loopback', port: 7778, token: 'tok en/+=' }
const relayEndpoint: PaneEndpoint = {
  kind: 'same-origin-relay',
  documentPath: '/instance-pane/K_cap-9f3a/',
  channel: 'chan-abc',
  protocol: 1,
  leaseExpiresAtEpochMs: 4_000_000_000_000,
}

describe('resolvePaneMode + paneAccessFor', () => {
  it('is direct-loopback for plain http on loopback', () => {
    expect(directMode.kind).toBe('direct-loopback')
    expect(paneAccessFor(directMode)).toBe('direct-loopback')
  })
  it('is same-origin-relay for a published HTTPS parent, carrying the parent origin', () => {
    expect(relayMode).toEqual({ kind: 'same-origin-relay', origin: 'https://crew.example.ts.net' })
    expect(paneAccessFor(relayMode)).toBe('same-origin-relay')
  })
  it('is same-origin-relay for https even on localhost (no mixed-content downgrade)', () => {
    const m = resolvePaneMode({ protocol: 'https:', hostname: 'localhost', origin: 'https://localhost' })
    expect(m.kind).toBe('same-origin-relay')
  })
  it('is same-origin-relay for a non-loopback http host (forward port is unreachable there)', () => {
    const m = resolvePaneMode({ protocol: 'http:', hostname: 'crew.example.ts.net', origin: 'http://crew.example.ts.net' })
    expect(m.kind).toBe('same-origin-relay')
  })
})

describe('parsePaneEndpoint (issuer response → typed endpoint)', () => {
  it('parses a direct-loopback endpoint', () => {
    expect(
      parsePaneEndpoint({ kind: 'direct-loopback', instance_id: 'cd-1', local_port: 7778, token: 'T' }),
    ).toEqual({ kind: 'direct-loopback', port: 7778, token: 'T' })
  })
  it('parses a same-origin-relay endpoint', () => {
    expect(
      parsePaneEndpoint({
        kind: 'same-origin-relay',
        instance_id: 'cd-1',
        documentPath: '/instance-pane/CAP/',
        channel: 'chan',
        protocol: 1,
        leaseExpiresAtEpochMs: 123,
      }),
    ).toEqual({
      kind: 'same-origin-relay',
      documentPath: '/instance-pane/CAP/',
      channel: 'chan',
      protocol: 1,
      leaseExpiresAtEpochMs: 123,
    })
  })
  it('rejects a relay endpoint whose documentPath is not under the relay prefix', () => {
    expect(
      parsePaneEndpoint({
        kind: 'same-origin-relay',
        documentPath: '/evil/',
        channel: 'c',
        protocol: 1,
        leaseExpiresAtEpochMs: 123,
      }),
    ).toBeNull()
  })
  it('rejects a relay endpoint with a missing or non-finite lease expiry (renewal needs it)', () => {
    const base = { kind: 'same-origin-relay', documentPath: '/instance-pane/CAP/', channel: 'chan', protocol: 1 }
    expect(parsePaneEndpoint(base)).toBeNull() // no leaseExpiresAtEpochMs
    expect(parsePaneEndpoint({ ...base, leaseExpiresAtEpochMs: 'soon' })).toBeNull()
    expect(parsePaneEndpoint({ ...base, leaseExpiresAtEpochMs: Number.NaN })).toBeNull()
  })
  it('rejects a direct endpoint missing a port or token', () => {
    expect(parsePaneEndpoint({ kind: 'direct-loopback', local_port: 0, token: 'T' })).toBeNull()
    expect(parsePaneEndpoint({ kind: 'direct-loopback', local_port: 7778 })).toBeNull()
  })
  it('rejects an unknown kind or non-object', () => {
    expect(parsePaneEndpoint({ kind: 'nonsense' })).toBeNull()
    expect(parsePaneEndpoint(null)).toBeNull()
    expect(parsePaneEndpoint('x')).toBeNull()
  })
})

describe('paneEndpointSrc', () => {
  it('builds the exact historical loopback src+origin in direct mode', () => {
    expect(paneEndpointSrc(directMode, directEndpoint)).toEqual({
      src: 'http://localhost:7778/?token=tok%20en%2F%2B%3D',
      origin: 'http://localhost:7778',
    })
  })
  it('relay src is the same-origin documentPath — NO host:port and NO token', () => {
    const addr = paneEndpointSrc(relayMode, relayEndpoint)
    expect(addr).toEqual({ src: '/instance-pane/K_cap-9f3a/', origin: 'https://crew.example.ts.net' })
    // The incident regression, restated for R3: the relay src must never be a
    // host:port URL and must never carry the remote token.
    expect(addr!.src).not.toMatch(/:\d/)
    expect(addr!.src).not.toContain('token')
  })
  it('returns null with no endpoint, or a mode/endpoint mismatch', () => {
    expect(paneEndpointSrc(directMode, undefined)).toBeNull()
    expect(paneEndpointSrc(directMode, relayEndpoint)).toBeNull()
    expect(paneEndpointSrc(relayMode, directEndpoint)).toBeNull()
  })
})

describe('paneMessageEnvelope (downward addressing)', () => {
  it('direct targets the exact origin and adds nothing to the message', () => {
    const env = paneMessageEnvelope(directMode, directEndpoint, { type: 'mc-host-model', v: 1 })
    expect(env).toEqual({ message: { type: 'mc-host-model', v: 1 }, targetOrigin: 'http://localhost:7778' })
    expect(PANE_CHANNEL_FIELD in (env!.message as object)).toBe(false)
  })
  it('relay stamps the channel and targets "*" (opaque frame cannot match a concrete origin)', () => {
    const env = paneMessageEnvelope(relayMode, relayEndpoint, { type: 'mc-host-model', v: 1 })
    expect(env!.targetOrigin).toBe('*')
    expect(env!.message).toEqual({ type: 'mc-host-model', v: 1, [PANE_CHANNEL_FIELD]: 'chan-abc' })
  })
  it('returns null on no endpoint or a mode/endpoint mismatch', () => {
    expect(paneMessageEnvelope(directMode, undefined, {})).toBeNull()
    expect(paneMessageEnvelope(relayMode, directEndpoint, {})).toBeNull()
  })
})

describe('resolvePaneMessage (inbound attribution)', () => {
  const portToId = new Map<number, string>([[7778, 'cd-1']])

  describe('direct mode (byte-identical to the pre-R3 authority)', () => {
    it('maps a known loopback origin to its instance id', () => {
      const id = resolvePaneMessage(
        { source: {} as MessageEventSource, origin: 'http://127.0.0.1:7778', data: { type: 'mc-unread-slots', count: 2 } },
        directMode,
        { portToId },
      )
      expect(id).toBe('cd-1')
    })
    it('rejects an unknown port and a non-loopback origin', () => {
      expect(
        resolvePaneMessage({ source: null, origin: 'http://127.0.0.1:9999', data: {} }, directMode, { portToId }),
      ).toBeNull()
      expect(
        resolvePaneMessage({ source: null, origin: 'https://127.0.0.1:7778', data: {} }, directMode, { portToId }),
      ).toBeNull()
    })
  })

  describe('relay mode (source + channel; origin "null" is never auth)', () => {
    const frameA = { tag: 'A' } as unknown as Window
    const frameB = { tag: 'B' } as unknown as Window
    const endpoints = new Map<string, PaneEndpoint>([
      ['cd-1', relayEndpoint],
      [
        'cd-2',
        {
          kind: 'same-origin-relay',
          documentPath: '/instance-pane/OTHER/',
          channel: 'chan-2',
          protocol: 1,
          leaseExpiresAtEpochMs: 4_000_000_000_000,
        },
      ],
    ])
    const frames = new Map<string, Window>([['cd-1', frameA], ['cd-2', frameB]])

    // Every existing parent-action family funnels through this one resolver, so
    // attribution is asserted type-agnostically across a representative set.
    const FAMILIES = [
      'mc-unread-slots', 'mc-native-notify', 'mc-auth-expired', 'mc-switch-instance',
      'mc-chained-crew', 'mc-set-crew-pin', 'mc-set-stable-order', 'mc-set-focus-mode',
      'mc-focus-chrome', 'mc-cursor-away-watch', 'mc-cursor-away-cancel',
      'mc-embedded-boot', 'mc-embedded-ready', 'mc-drag-gaps',
    ]

    it('attributes EVERY family to its instance on exact source + channel', () => {
      for (const type of FAMILIES) {
        const id = resolvePaneMessage(
          { source: frameA, origin: 'null', data: { type, [PANE_CHANNEL_FIELD]: 'chan-abc' } },
          relayMode,
          { endpoints, frames },
        )
        expect(id, `family ${type}`).toBe('cd-1')
      }
    })

    it('never trusts event.origin — "null" is fine, a spoofed hub origin is ignored', () => {
      for (const origin of ['null', 'https://crew.example.ts.net', 'https://evil.example']) {
        const id = resolvePaneMessage(
          { source: frameA, origin, data: { type: 'mc-unread-slots', [PANE_CHANNEL_FIELD]: 'chan-abc' } },
          relayMode,
          { endpoints, frames },
        )
        expect(id).toBe('cd-1') // decided purely by source+channel
      }
    })

    it('rejects a STALE channel (retry/rebuild minted a new one) even from the right frame', () => {
      const id = resolvePaneMessage(
        { source: frameA, origin: 'null', data: { type: 'mc-unread-slots', [PANE_CHANNEL_FIELD]: 'OLD-channel' } },
        relayMode,
        { endpoints, frames },
      )
      expect(id).toBeNull()
    })

    it('rejects a FOREIGN frame carrying a valid channel (source must be the exact contentWindow)', () => {
      const id = resolvePaneMessage(
        { source: frameB, origin: 'null', data: { type: 'mc-unread-slots', [PANE_CHANNEL_FIELD]: 'chan-abc' } },
        relayMode,
        { endpoints, frames },
      )
      expect(id).toBeNull()
    })

    it('rejects a message that carries no channel, or a non-object payload', () => {
      expect(
        resolvePaneMessage({ source: frameA, origin: 'null', data: { type: 'mc-unread-slots' } }, relayMode, { endpoints, frames }),
      ).toBeNull()
      expect(
        resolvePaneMessage({ source: frameA, origin: 'null', data: 'string' }, relayMode, { endpoints, frames }),
      ).toBeNull()
    })

    it('rejects when the attributed frame is no longer mounted (stale after eviction)', () => {
      const id = resolvePaneMessage(
        { source: frameA, origin: 'null', data: { type: 'mc-unread-slots', [PANE_CHANNEL_FIELD]: 'chan-abc' } },
        relayMode,
        { endpoints, frames: new Map() }, // frame gone
      )
      expect(id).toBeNull()
    })
  })
})

describe('sameEndpoint', () => {
  const direct = (port: number, token: string): PaneEndpoint => ({ kind: 'direct-loopback', port, token })
  const relay = (documentPath: string, channel: string): PaneEndpoint => ({
    kind: 'same-origin-relay',
    documentPath,
    channel,
    protocol: 1,
    leaseExpiresAtEpochMs: 4_000_000_000_000,
  })

  it('treats undefined like a value: two undefineds are the same, one is not', () => {
    expect(sameEndpoint(undefined, undefined)).toBe(true)
    expect(sameEndpoint(undefined, direct(7778, 't'))).toBe(false)
    expect(sameEndpoint(direct(7778, 't'), undefined)).toBe(false)
  })

  it('direct: a fresh port OR a fresh token is a new load', () => {
    expect(sameEndpoint(direct(7778, 't'), direct(7778, 't'))).toBe(true)
    expect(sameEndpoint(direct(7778, 't'), direct(7779, 't'))).toBe(false)
    expect(sameEndpoint(direct(7778, 't'), direct(7778, 'other'))).toBe(false)
  })

  it('relay: a fresh capability OR a fresh channel is a new load', () => {
    expect(sameEndpoint(relay('/instance-pane/c/', 'ch'), relay('/instance-pane/c/', 'ch'))).toBe(true)
    expect(sameEndpoint(relay('/instance-pane/c/', 'ch'), relay('/instance-pane/d/', 'ch'))).toBe(false)
    expect(sameEndpoint(relay('/instance-pane/c/', 'ch'), relay('/instance-pane/c/', 'ch2'))).toBe(false)
  })

  it('a transport switch is never the same load', () => {
    expect(sameEndpoint(direct(7778, 't'), relay('/instance-pane/c/', 'ch'))).toBe(false)
  })
})

describe('relayRenewDelayMs (lease renewal timing)', () => {
  const now = 1_000_000

  it('schedules a bounded margin (~2 min) before a normal 15-minute lease', () => {
    const ttl = 15 * 60 * 1000
    const delay = relayRenewDelayMs(now, now + ttl)
    // margin = min(2min, ttl/3=5min) = 2min → renew 13 minutes in.
    expect(delay).toBe(ttl - RELAY_LEASE_RENEW_MARGIN_MS)
  })

  it('renews proportionally early for a SHORT lease instead of thrashing', () => {
    // A 3s lease: margin caps at ttl/3 = 1s, so it renews at 2s — real headroom,
    // not the full 2-minute constant (which would fire before the lease issued).
    const delay = relayRenewDelayMs(now, now + 3000)
    expect(delay).toBe(2000)
  })

  it('renews immediately only once the deadline is reached or past', () => {
    expect(relayRenewDelayMs(now, now - 5000)).toBe(0) // already expired
    expect(relayRenewDelayMs(now, now)).toBe(0) // exactly at the deadline
  })

  it('keeps proportional headroom even when the margin exceeds the whole lease', () => {
    // With maxMargin larger than the remaining lease, the margin is still capped
    // at remaining/3, so a pane never renews the instant it is issued — it always
    // keeps ~1/3 of the lease as headroom.
    expect(relayRenewDelayMs(now, now + 1000, /* margin */ 5000)).toBeCloseTo(666.67, 1)
  })

  it('never returns a negative delay', () => {
    for (const expiresIn of [-10_000, -1, 0, 1, 500, 3000, 900_000]) {
      expect(relayRenewDelayMs(now, now + expiresIn)).toBeGreaterThanOrEqual(0)
    }
  })
})

describe('resolvePaneBootstrapRequest (subsequent-document reseed, parent side)', () => {
  // A pane that navigated within its capability lost its window.name envelope and
  // asks the parent to reseed. The request carries NO channel, so it is bound by
  // the exact sender frame AND the capability documentPath the parent issued —
  // never event.origin (opaque 'null'). These pin the negative paths the review
  // asked for: wrong frame, wrong/unknown capability, and a stale/oversized nonce.
  const frameA = { tag: 'A' } as unknown as Window
  const frameB = { tag: 'B' } as unknown as Window
  const foreign = { tag: 'evil' } as unknown as Window
  const endpoints = new Map<string, PaneEndpoint>([
    ['cd-1', relayEndpoint], // documentPath /instance-pane/K_cap-9f3a/, channel chan-abc
    [
      'cd-2',
      {
        kind: 'same-origin-relay',
        documentPath: '/instance-pane/OTHER/',
        channel: 'chan-2',
        protocol: 1,
        leaseExpiresAtEpochMs: 4_000_000_000_000,
      },
    ],
    ['direct', directEndpoint], // a direct endpoint must never be matched
  ])
  const frames = new Map<string, Window>([['cd-1', frameA], ['cd-2', frameB]])

  it('grants the channel + protocol + nonce for the exact frame and its issued documentPath', () => {
    const grant = resolvePaneBootstrapRequest(
      { source: frameA, documentPath: '/instance-pane/K_cap-9f3a/', nonce: 'n-123' },
      { endpoints, frames },
    )
    expect(grant).toEqual({ id: 'cd-1', channel: 'chan-abc', protocol: 1, nonce: 'n-123' })
  })

  it('refuses a request whose frame is not the one the capability was issued to', () => {
    // frameB holds cd-2, not cd-1: the documentPath names cd-1 but the source is wrong.
    expect(
      resolvePaneBootstrapRequest(
        { source: frameB, documentPath: '/instance-pane/K_cap-9f3a/', nonce: 'n' },
        { endpoints, frames },
      ),
    ).toBeNull()
    // A wholly foreign window (e.g. the frame navigated to an attacker origin)
    // supplying a real documentPath is refused for the same reason.
    expect(
      resolvePaneBootstrapRequest(
        { source: foreign, documentPath: '/instance-pane/K_cap-9f3a/', nonce: 'n' },
        { endpoints, frames },
      ),
    ).toBeNull()
  })

  it('refuses an unknown capability documentPath, and one that names a DIFFERENT frame', () => {
    // Unknown capability: no endpoint matches.
    expect(
      resolvePaneBootstrapRequest(
        { source: frameA, documentPath: '/instance-pane/UNKNOWN/', nonce: 'n' },
        { endpoints, frames },
      ),
    ).toBeNull()
    // cd-2's documentPath from cd-1's frame: capability + frame must agree.
    expect(
      resolvePaneBootstrapRequest(
        { source: frameA, documentPath: '/instance-pane/OTHER/', nonce: 'n' },
        { endpoints, frames },
      ),
    ).toBeNull()
  })

  it('refuses a missing source, a missing/blank documentPath, and a missing/oversized nonce', () => {
    const ctx = { endpoints, frames }
    expect(resolvePaneBootstrapRequest({ source: null, documentPath: '/instance-pane/K_cap-9f3a/', nonce: 'n' }, ctx)).toBeNull()
    expect(resolvePaneBootstrapRequest({ source: frameA, documentPath: '', nonce: 'n' }, ctx)).toBeNull()
    expect(resolvePaneBootstrapRequest({ source: frameA, documentPath: 42, nonce: 'n' }, ctx)).toBeNull()
    expect(resolvePaneBootstrapRequest({ source: frameA, documentPath: '/instance-pane/K_cap-9f3a/', nonce: '' }, ctx)).toBeNull()
    expect(resolvePaneBootstrapRequest({ source: frameA, documentPath: '/instance-pane/K_cap-9f3a/', nonce: 123 }, ctx)).toBeNull()
    expect(
      resolvePaneBootstrapRequest(
        { source: frameA, documentPath: '/instance-pane/K_cap-9f3a/', nonce: 'x'.repeat(257) },
        ctx,
      ),
    ).toBeNull()
  })

  it('never matches a direct-loopback endpoint, and refuses when the ctx has no frames/endpoints', () => {
    // A direct endpoint has no documentPath/channel; a relay request must not bind to it.
    expect(
      resolvePaneBootstrapRequest(
        { source: frameA, documentPath: '/instance-pane/K_cap-9f3a/', nonce: 'n' },
        { endpoints: new Map([['direct', directEndpoint]]), frames: new Map([['direct', frameA]]) },
      ),
    ).toBeNull()
    expect(
      resolvePaneBootstrapRequest(
        { source: frameA, documentPath: '/instance-pane/K_cap-9f3a/', nonce: 'n' },
        {},
      ),
    ).toBeNull()
  })
})
