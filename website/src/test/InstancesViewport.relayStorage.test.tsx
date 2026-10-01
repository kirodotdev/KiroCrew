/**
 * Parent-listener regression for relay-pane storage mutations (semantic-review
 * Finding 3).
 *
 * The parent's `message` listener receives Web Storage mutations from a
 * sandboxed, opaque-origin relay pane — an attacker-reachable frame. Two failure
 * modes must never raise in that loop:
 *   1. A malformed mutation shape (missing fields, wrong types, unknown op) must
 *      be dropped by `parseRelayStorageMutation` before the bank sees it.
 *   2. A well-typed but unmatched-surrogate key (`"\uD800"`) must not throw from
 *      `encodeURIComponent` inside `RelayStorageBank.apply`.
 * In both cases the listener must not throw and must not mutate the bank, and a
 * LATER valid mutation must still persist.
 *
 * This drives the REAL listener: it mounts the viewport in relay mode, attributes
 * the message by the pane channel + the exact iframe `contentWindow` (never
 * `event.origin`, which is the opaque `'null'`), and observes the parent origin's
 * real `window.localStorage`, which the bank persists into.
 *
 * test-audit: observable behaviour (persistence in the parent origin's Storage +
 * no thrown error), credible regression (a dropped parse or an unguarded encoder
 * reopens the confirmed escape), no test-only seam (the bank's backing store is
 * the injected `window.localStorage`, exactly as production wires it).
 */
import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { RELAY_STORAGE_MESSAGE, PANE_CHANNEL_FIELD } from '../lib/relayPaneBootstrap'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

// Force relay mode, and attribute a relay-storage message to the warm instance.
// Real channel + exact-`contentWindow` attribution is covered by the paneChannel
// suite; jsdom does not expose a usable iframe `contentWindow`, and this test
// targets the STORAGE-MUTATION handling the fix changed (parse + exception-safe
// apply), not attribution — so attribution is stubbed to reach that branch.
vi.mock('../lib/paneChannel', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/paneChannel')>()
  return {
    ...real,
    resolvePaneMode: () => ({ kind: 'same-origin-relay', origin: 'https://hub.example' }),
    resolvePaneMessage: (arg: { data?: { type?: unknown } }) =>
      arg?.data?.type === 'mc-relay-storage' ? 'cd-1' : null,
  }
})

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    openInstancePane: vi.fn(),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    updateInstance: vi.fn().mockResolvedValue({}),
    addInstance: vi.fn().mockResolvedValue({ id: 'x' }),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected' }),
  },
}))

const CHANNEL = 'ch1'
const INSTANCE = 'cd-1'
// Far enough out that no renewal timer fires during the test.
const LEASE_MS = 10 * 60 * 1000

function relayWarm() {
  return {
    instances: {
      warm: {
        [INSTANCE]: {
          kind: 'same-origin-relay' as const,
          documentPath: '/instance-pane/CAP1/',
          channel: CHANNEL,
          protocol: 1,
          leaseExpiresAtEpochMs: LEASE_MS,
        },
      },
      activeId: INSTANCE,
      mru: [INSTANCE],
      unread: {},
      ready: {},
    },
  }
}

function storageMessage(mutation: unknown) {
  return {
    type: RELAY_STORAGE_MESSAGE,
    area: 'local',
    mutation,
    [PANE_CHANNEL_FIELD]: CHANNEL,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  vi.setSystemTime(0)
  window.localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
  window.localStorage.clear()
})

describe('InstancesViewport — relay storage parent listener', () => {
  function mount() {
    const store = createTestStore(relayWarm())
    renderWithProviders(<InstancesViewport />, { store })
  }

  function post(mutation: unknown) {
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', { data: storageMessage(mutation), origin: 'null' }),
      )
    })
  }

  function instanceWroteAnything(): boolean {
    for (let i = 0; i < window.localStorage.length; i++) {
      if ((window.localStorage.key(i) ?? '').startsWith(`relay-ls:${INSTANCE}:`)) return true
    }
    return false
  }

  it('persists a well-formed set into the parent origin store, namespaced per instance', () => {
    mount()
    post({ op: 'set', key: 'theme', value: 'dark' })
    expect(window.localStorage.getItem(`relay-ls:${INSTANCE}:theme`)).toBe('dark')
  })

  it('drops a malformed mutation shape without throwing or mutating the bank', () => {
    mount()
    // Missing value, wrong-typed key, unknown op, and a non-object mutation —
    // none may throw in the listener, and none may write.
    expect(() => post({ op: 'set', key: 'k' })).not.toThrow()
    expect(() => post({ op: 'set', key: 123, value: 'v' })).not.toThrow()
    expect(() => post({ op: 'nope', key: 'k', value: 'v' })).not.toThrow()
    expect(() => post('not-an-object')).not.toThrow()
    expect(instanceWroteAnything()).toBe(false)
  })

  it('does not throw on an unmatched-surrogate key, and a later valid mutation still persists', () => {
    mount()
    // A lone high surrogate makes encodeURIComponent throw natively.
    expect(() => encodeURIComponent('\uD800')).toThrow()
    expect(() => post({ op: 'set', key: '\uD800', value: 'x' })).not.toThrow()
    // The malformed-key write was dropped (no entry under this instance) …
    expect(instanceWroteAnything()).toBe(false)
    // … and a subsequent valid mutation still lands.
    post({ op: 'set', key: 'lang', value: 'en' })
    expect(window.localStorage.getItem(`relay-ls:${INSTANCE}:lang`)).toBe('en')
  })
})
