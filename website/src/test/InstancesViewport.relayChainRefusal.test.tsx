/*
 * Chained-crew refusal over the authenticated document port (relay mode).
 *
 * The refusal is the one downward message the parent produces ASYNCHRONOUSLY:
 * `adoptChainedCrew` awaits the owner-side add/connect before it can answer the
 * announcing pane, and by then the announcing document may have navigated away.
 * A wildcard `contentWindow.postMessage(refusal, '*')` would hand the live
 * channel to whatever document now occupies the frame. In relay mode the
 * refusal must ride the MessageChannel port the authenticated document
 * transferred, which a replacement never held.
 *
 * Relay mode is forced by overriding resolvePaneMode; attribution is stubbed to
 * reach the handler branches (jsdom exposes no usable iframe `contentWindow`;
 * exact-frame + capability binding is the paneChannel suite's job). The real
 * viewport listener, store, adoption flow and port send drive the assertions.
 *
 * test-audit: observable behaviour (the refusal arrives on the child's end of
 * the port, channel-stamped), credible regression (a frame-targeted post never
 * reaches the port, so the assertion fails), no test-only seam.
 */
import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { RELAY_BOOTSTRAP_REQUEST, RELAY_ENVELOPE_VERSION } from '../lib/relayPaneBootstrap'
import { CHAINED_CREW_MESSAGE, CHAINED_CREW_REFUSED_MESSAGE } from '../lib/chainAnnounce'
import { api } from '../api/client'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

const CHANNEL = 'ch1'
const INSTANCE = 'cd-1'
const DOC_PATH = '/instance-pane/CAP1/'

vi.mock('../lib/paneChannel', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/paneChannel')>()
  return {
    ...real,
    resolvePaneMode: () => ({ kind: 'same-origin-relay', origin: 'https://hub.example' }),
    resolvePaneBootstrapRequest: (req: { documentPath?: unknown; nonce?: unknown }) =>
      req?.documentPath === DOC_PATH
        ? { id: INSTANCE, channel: CHANNEL, protocol: 1, nonce: String(req.nonce) }
        : null,
    resolvePaneMessage: (arg: { data?: { type?: unknown } }) =>
      arg?.data?.type === CHAINED_CREW_MESSAGE ? INSTANCE : null,
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
    addInstance: vi.fn(),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected' }),
  },
}))

const LEASE_MS = 10 * 60 * 1000

function readyRelayWarm() {
  return {
    instances: {
      warm: {
        [INSTANCE]: {
          kind: 'same-origin-relay' as const,
          documentPath: DOC_PATH,
          channel: CHANNEL,
          protocol: 1,
          leaseExpiresAtEpochMs: Date.now() + LEASE_MS,
        },
      },
      activeId: INSTANCE,
      mru: [INSTANCE],
      unread: {},
      ready: { [INSTANCE]: true },
    },
  }
}

/** The child's end of a port the parent adopted for this document. */
function bootstrapPort(nonce: string): MessagePort {
  const mc = new MessageChannel()
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: RELAY_BOOTSTRAP_REQUEST, documentPath: DOC_PATH, nonce, v: RELAY_ENVELOPE_VERSION },
        origin: 'null',
        ports: [mc.port2],
      }),
    )
  })
  return mc.port1
}

function nextMessageOfType(port: MessagePort, type: string): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`no ${type} on the port within 2s`)), 2000)
    port.onmessage = (ev: MessageEvent) => {
      const data = ev.data as Record<string, unknown>
      if (data?.type !== type) return
      clearTimeout(timer)
      resolve(data)
    }
  })
}

function announceFromPane() {
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', {
        data: {
          type: CHAINED_CREW_MESSAGE,
          v: 1,
          id: 'child-1',
          name: 'Child crew',
          sshHost: 'unused.example',
          remotePort: 7777,
          port: 12345,
          mcPaneChannel: CHANNEL,
        },
        origin: 'null',
      }),
    )
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.spyOn(console, 'info').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('InstancesViewport — chained-crew refusal in relay mode', () => {
  it('delivers the refusal over the authenticated document port, channel-stamped, once the gateway answers', async () => {
    let refuse: (err: unknown) => void = () => {}
    vi.mocked(api.addInstance).mockImplementation(
      () => new Promise((_resolve, reject) => { refuse = reject }),
    )
    const store = createTestStore(readyRelayWarm())
    renderWithProviders(<InstancesViewport />, { store })

    const childEnd = bootstrapPort('nonce-A')
    const refusal = nextMessageOfType(childEnd, CHAINED_CREW_REFUSED_MESSAGE)

    announceFromPane()
    await vi.waitFor(() => expect(api.addInstance).toHaveBeenCalledTimes(1))

    // The gateway answers only now — after the announcing document could have
    // navigated. The answer must still reach only the port that authenticated.
    const err = Object.assign(new Error('chain too deep'), {
      body: JSON.stringify({ error: 'chain too deep', code: 'chain_depth' }),
    })
    await act(async () => { refuse(err) })

    const received = await refusal
    expect(received).toMatchObject({
      type: CHAINED_CREW_REFUSED_MESSAGE,
      v: 1,
      id: 'child-1',
      reason: 'chain too deep',
      mcPaneChannel: CHANNEL,
    })
    childEnd.close()
  })
})
