/**
 * The socket and audio fakes the UseWebSocketCoverage*.test.tsx files share.
 * Each test file stubs the WebSocket global with MockWebSocket and empties
 * WS_INSTANCES (and MockAudio.instances) in its own beforeEach.
 */
import { vi } from 'vitest'

export const ACTIVE = 'slot-a'
export const BACKGROUND = 'slot-b'

export const WS_INSTANCES: MockWebSocket[] = []

export class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })

  constructor(public url: string) { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }

  /** Raw frame delivery, for the malformed-payload guard. */
  simulateRaw(raw: string) {
    this.onmessage?.(new MessageEvent('message', { data: raw }))
  }
}

/** Minimal audio double: jsdom's HTMLAudioElement cannot actually play. */
export class MockAudio {
  static instances: MockAudio[] = []
  static playResult: () => Promise<void> = () => Promise.resolve()
  onended: (() => void) | null = null
  onerror: (() => void) | null = null
  pause = vi.fn()
  play = vi.fn(() => MockAudio.playResult())
  constructor(public src: string) { MockAudio.instances.push(this) }
}
