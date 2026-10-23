import { afterEach, describe, expect, it, vi } from 'vitest'
import { installSwConnectedProbe, PUSH_PROBE_MESSAGE } from './swConnectedProbe'

// The page half of the worker's push probe: it must answer from the connection
// state at the moment it is asked, so a tab whose socket dropped says false.

type Listener = (e: MessageEvent) => void

function fakeContainer() {
  const listeners = new Set<Listener>()
  const container = {
    addEventListener: (_kind: string, fn: Listener) => listeners.add(fn),
    removeEventListener: (_kind: string, fn: Listener) => listeners.delete(fn),
  }
  const send = (data: unknown) => {
    const port = { postMessage: vi.fn() }
    for (const fn of listeners) fn({ data, ports: [port] } as unknown as MessageEvent)
    return port
  }
  return { container, send, listeners }
}

const original = Object.getOwnPropertyDescriptor(globalThis.navigator, 'serviceWorker')

afterEach(() => {
  if (original) Object.defineProperty(globalThis.navigator, 'serviceWorker', original)
  else delete (globalThis.navigator as unknown as Record<string, unknown>).serviceWorker
})

describe('installSwConnectedProbe', () => {
  it('answers each probe with the live connection state', () => {
    const fake = fakeContainer()
    Object.defineProperty(globalThis.navigator, 'serviceWorker', { value: fake.container, configurable: true })
    let connected = true
    installSwConnectedProbe(() => connected)

    expect(fake.send({ type: PUSH_PROBE_MESSAGE }).postMessage).toHaveBeenCalledWith({ connected: true })
    connected = false
    expect(fake.send({ type: PUSH_PROBE_MESSAGE }).postMessage).toHaveBeenCalledWith({ connected: false })
  })

  it('answers false from the look-preview frame, which drops every socket frame', () => {
    const fake = fakeContainer()
    Object.defineProperty(globalThis.navigator, 'serviceWorker', { value: fake.container, configurable: true })
    const before = window.location.href
    window.history.replaceState(null, '', '/chat?look-preview=1')
    try {
      installSwConnectedProbe(() => true)
      expect(fake.send({ type: PUSH_PROBE_MESSAGE }).postMessage).toHaveBeenCalledWith({ connected: false })
    } finally {
      window.history.replaceState(null, '', before)
    }
  })

  it('ignores other worker messages', () => {
    const fake = fakeContainer()
    Object.defineProperty(globalThis.navigator, 'serviceWorker', { value: fake.container, configurable: true })
    installSwConnectedProbe(() => true)
    expect(fake.send({ type: 'something-else' }).postMessage).not.toHaveBeenCalled()
  })

  it('removes its listener when uninstalled', () => {
    const fake = fakeContainer()
    Object.defineProperty(globalThis.navigator, 'serviceWorker', { value: fake.container, configurable: true })
    const uninstall = installSwConnectedProbe(() => true)
    uninstall()
    expect(fake.listeners.size).toBe(0)
  })
})
