/**
 * Regression + contract tests for the relay-pane bootstrap envelope and the
 * child boot step (src/lib/relayPaneBootstrap.ts).
 *
 * The parent seeds a versioned envelope into the pane iframe's `window.name`
 * before the document loads. The child, BEFORE any app module can read storage,
 * parses that envelope, installs synchronous localStorage/sessionStorage shims
 * from its snapshot, clears `window.name`, and records the channel + parent
 * origin the centralized messaging layer binds to.
 *
 * Observable contract this owns (test-audit authoring gate):
 *   1. Round-trip: `parseBootstrapEnvelope(buildBootstrapEnvelope(e))` === e.
 *   2. Strictness: a non-envelope `window.name` (a popout name, empty, garbage,
 *      wrong version, wrong shape) parses to `null` — the child stays in native
 *      (direct) mode and does not shim.
 *   3. Boot: on a valid envelope the child defines `localStorage` /
 *      `sessionStorage` seeded from the snapshot, CLEARS `window.name`, and
 *      routes a mutation up to `window.parent` stamped with the channel and
 *      addressed to the parent origin. No envelope ⇒ no shim, name untouched.
 *   4. No native storage: after boot the shims are what reads/writes hit — the
 *      opaque frame never touches the throwing native `Storage`.
 */
import { describe, it, expect, vi } from 'vitest'
import {
  RELAY_ENVELOPE_VERSION,
  PANE_CHANNEL_FIELD,
  RELAY_STORAGE_MESSAGE,
  RELAY_BOOTSTRAP_REQUEST,
  RELAY_BOOTSTRAP_REPLY,
  buildBootstrapEnvelope,
  parseBootstrapEnvelope,
  bootstrapRelayPane,
  type RelayBootstrapEnvelope,
} from '../lib/relayPaneBootstrap'

const envelope: RelayBootstrapEnvelope = {
  v: RELAY_ENVELOPE_VERSION,
  channel: 'chan-abc',
  parentOrigin: 'https://crew.example.ts.net',
  protocol: 1,
  storage: { local: { theme: 'dark' }, session: { nonce: 'xyz' } },
}

describe('envelope round-trip + strictness', () => {
  it('round-trips through build/parse', () => {
    expect(parseBootstrapEnvelope(buildBootstrapEnvelope(envelope))).toEqual(envelope)
  })

  it('returns null for an empty or non-JSON window.name', () => {
    expect(parseBootstrapEnvelope('')).toBeNull()
    expect(parseBootstrapEnvelope('not json')).toBeNull()
    expect(parseBootstrapEnvelope('123')).toBeNull()
  })

  it('returns null for an unrelated JSON window.name (a popout name is not an envelope)', () => {
    expect(parseBootstrapEnvelope(JSON.stringify({ popout: 'artifact', id: 'x' }))).toBeNull()
  })

  it('returns null on a version mismatch', () => {
    const wrong = buildBootstrapEnvelope(envelope).replace(
      `"v":${RELAY_ENVELOPE_VERSION}`,
      '"v":999',
    )
    expect(parseBootstrapEnvelope(wrong)).toBeNull()
  })

  it('returns null when a required field is missing or the wrong type', () => {
    const bad = { ...envelope, channel: 123 as unknown as string }
    expect(parseBootstrapEnvelope(buildBootstrapEnvelope(envelope).replace('"chan-abc"', '123'))).toBeNull()
    // storage of the wrong shape
    expect(
      parseBootstrapEnvelope(JSON.stringify({ __kcRelayPane: RELAY_ENVELOPE_VERSION, ...bad, storage: 5 })),
    ).toBeNull()
  })
})

/**
 * A fake window good enough for the boot step: a real `EventTarget` (so the
 * downstream listener and the port→window bridge share one dispatch surface), a
 * `parent.postMessage` spy that captures the TRANSFERRED port, a settable
 * `name`, a `location.pathname`, and real `crypto`. `MessageChannel` /
 * `MessageEvent` are happy-dom globals with working delivery, so the test can act
 * as the parent by replying on the captured port.
 */
function relayWindow(opts: { name?: string; pathname?: string }) {
  const post =
    vi.fn<(message: unknown, targetOrigin: string, transfer?: Transferable[]) => void>()
  const et = new EventTarget()
  const parent = { postMessage: post } as unknown as Window
  const win = {
    name: opts.name ?? '',
    parent,
    location: { pathname: opts.pathname ?? '/instance-pane/CAP/' } as Location,
    crypto: globalThis.crypto,
    addEventListener: et.addEventListener.bind(et),
    removeEventListener: et.removeEventListener.bind(et),
    dispatchEvent: et.dispatchEvent.bind(et),
  } as unknown as Window & typeof globalThis
  /** The port the child TRANSFERRED to the parent (the parent's reply channel).
   *  Duck-typed (has `postMessage`) rather than `instanceof MessagePort` — the
   *  happy-dom port class is not always identical to the global. */
  const transferredPort = (): MessagePort | undefined => {
    for (let i = post.mock.calls.length - 1; i >= 0; i--) {
      const transfer = post.mock.calls[i][2]
      const p = Array.isArray(transfer) ? transfer[0] : undefined
      if (p && typeof (p as { postMessage?: unknown }).postMessage === 'function') return p as MessagePort
    }
    return undefined
  }
  /** The last bootstrap-request the child posted (documentPath + nonce). */
  const lastRequest = (): { documentPath?: string; nonce?: string } | undefined => {
    for (let i = post.mock.calls.length - 1; i >= 0; i--) {
      const m = post.mock.calls[i][0] as { type?: string; documentPath?: string; nonce?: string }
      if (m?.type === RELAY_BOOTSTRAP_REQUEST) return m
    }
    return undefined
  }
  return { win, post, parent, transferredPort, lastRequest }
}

/** Reply as the parent over the port the child transferred, and let delivery run. */
async function replyOnPort(
  port: MessagePort,
  reply: Record<string, unknown>,
): Promise<void> {
  port.postMessage(reply)
  // happy-dom delivers a MessagePort message on a later task.
  await new Promise((r) => setTimeout(r, 0))
  await new Promise((r) => setTimeout(r, 0))
}

describe('bootstrapRelayPane (child boot: unified port handshake)', () => {
  it('not a relay document (no envelope, no capability path) ⇒ null, name untouched, no shim', () => {
    const { win } = relayWindow({ name: '', pathname: '/chat' })
    expect(bootstrapRelayPane(win)).toBeNull()
    expect(win.name).toBe('')
    expect(Object.getOwnPropertyDescriptor(win, 'localStorage')).toBeUndefined()
  })

  it('a relay document installs shims, clears window.name, and TRANSFERS a port with its request', () => {
    const { win, transferredPort, lastRequest } = relayWindow({
      name: buildBootstrapEnvelope(envelope),
      pathname: '/instance-pane/K_cap/',
    })
    const handle = bootstrapRelayPane(win, () => {})
    expect(handle).not.toBeNull()
    // window.name cleared BEFORE the app starts — the seed must not linger.
    expect(win.name).toBe('')
    // Shims installed and pre-seeded from the first-load envelope for a warm paint.
    expect(win.localStorage.getItem('theme')).toBe('dark')
    expect(win.sessionStorage.getItem('nonce')).toBe('xyz')
    // A port was transferred, and the request names the capability documentPath.
    expect(transferredPort()).toBeTruthy()
    const req = lastRequest()
    expect(req?.documentPath).toBe('/instance-pane/K_cap/')
    expect(typeof req?.nonce).toBe('string')
    // FAIL CLOSED: no context and no release before an authenticated reply.
    expect(handle!.context()).toBeNull()
    handle!.cancel()
  })

  it('a subsequent document (capability path, NO envelope) still boots via the port', () => {
    const { win, transferredPort } = relayWindow({ name: '', pathname: '/instance-pane/K_cap/chat' })
    const handle = bootstrapRelayPane(win, () => {})
    expect(handle).not.toBeNull()
    // Native storage replaced by shims even with no first-load envelope.
    expect(() => win.localStorage.getItem('x')).not.toThrow()
    expect(transferredPort()).toBeTruthy()
    handle!.cancel()
  })

  it('on the authenticated port reply it resolves the context, publishes it, and releases ONCE', async () => {
    const { win, transferredPort, lastRequest } = relayWindow({
      name: '',
      pathname: '/instance-pane/K_cap/',
    })
    let releases = 0
    const handle = bootstrapRelayPane(win, () => { releases++ })
    const port = transferredPort()!
    const nonce = lastRequest()!.nonce!
    await replyOnPort(port, {
      type: RELAY_BOOTSTRAP_REPLY,
      nonce,
      channel: 'chan-xyz',
      parentOrigin: 'https://crew.example.ts.net',
      protocol: 1,
      storage: { local: { theme: 'dark' }, session: {} },
    })
    const ctx = handle!.context()
    expect(ctx).not.toBeNull()
    expect(ctx!.channel).toBe('chan-xyz')
    expect(ctx!.parentOrigin).toBe('https://crew.example.ts.net')
    // The context the app's upward-messaging layer reads is published on window.
    expect((win as unknown as { __kcRelayPaneContext: { channel: string } }).__kcRelayPaneContext.channel).toBe(
      'chan-xyz',
    )
    // The authoritative bank from the reply seeded the shims.
    expect(win.localStorage.getItem('theme')).toBe('dark')
    // Released exactly once.
    expect(releases).toBe(1)
    handle!.cancel()
  })

  it('after the reply a shim write routes a channel-stamped message to the parent origin', async () => {
    const { win, post, transferredPort, lastRequest } = relayWindow({
      name: '',
      pathname: '/instance-pane/K_cap/',
    })
    bootstrapRelayPane(win, () => {})
    await replyOnPort(transferredPort()!, {
      type: RELAY_BOOTSTRAP_REPLY,
      nonce: lastRequest()!.nonce,
      channel: 'chan-xyz',
      parentOrigin: 'https://crew.example.ts.net',
      protocol: 1,
      storage: { local: {}, session: {} },
    })
    post.mockClear()
    win.localStorage.setItem('theme', 'light')
    const call = post.mock.calls.find((c) => (c[0] as { type?: string }).type === RELAY_STORAGE_MESSAGE)
    expect(call, 'a storage mutation was reported upward').toBeTruthy()
    const [msg, target] = call!
    expect(target).toBe('https://crew.example.ts.net')
    expect((msg as { area?: string }).area).toBe('local')
    expect((msg as Record<string, unknown>)[PANE_CHANNEL_FIELD]).toBe('chan-xyz')
  })

  it('a reply with the WRONG nonce is ignored — no release, fail closed', async () => {
    const { win, transferredPort } = relayWindow({ name: '', pathname: '/instance-pane/K_cap/' })
    let releases = 0
    const handle = bootstrapRelayPane(win, () => { releases++ })
    await replyOnPort(transferredPort()!, {
      type: RELAY_BOOTSTRAP_REPLY,
      nonce: 'not-the-nonce',
      channel: 'chan-xyz',
      parentOrigin: 'https://crew.example.ts.net',
      protocol: 1,
      storage: { local: {}, session: {} },
    })
    expect(handle!.context()).toBeNull()
    expect(releases).toBe(0)
    handle!.cancel()
  })

  it('NO reply ⇒ never resolves and never releases (the parent watchdog owns recovery)', async () => {
    const { win } = relayWindow({ name: '', pathname: '/instance-pane/K_cap/' })
    let releases = 0
    const handle = bootstrapRelayPane(win, () => { releases++ })
    await new Promise((r) => setTimeout(r, 0))
    expect(handle!.context()).toBeNull()
    expect(releases).toBe(0)
    handle!.cancel()
  })

  it('a seeded read never throws even though a real opaque frame would', () => {
    const { win } = relayWindow({ name: buildBootstrapEnvelope(envelope), pathname: '/instance-pane/K_cap/' })
    const handle = bootstrapRelayPane(win, () => {})
    expect(() => win.localStorage.getItem('anything')).not.toThrow()
    expect(win.localStorage.getItem('anything')).toBeNull()
    handle!.cancel()
  })
})
