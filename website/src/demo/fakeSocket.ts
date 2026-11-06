/**
 * An in-page stand-in for the dashboard socket. The dashboard's own `/api/ws`
 * opens and receives the frames the demo routes push through `pushFrame`; any
 * other socket (terminals, app bridges) opens and stays silent.
 */

type Listener = (ev: Event) => void

const openDashboardSockets = new Set<FakeDashboardSocket>()

/** Deliver one `/api/ws` envelope to every open dashboard socket. */
export function pushFrame(type: string, data: Record<string, unknown>): void {
  const raw = JSON.stringify({ type, data })
  for (const s of openDashboardSockets) s.deliver(raw)
}

export class FakeDashboardSocket extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readonly CONNECTING = 0
  readonly OPEN = 1
  readonly CLOSING = 2
  readonly CLOSED = 3

  readyState = 0
  protocol = ''
  extensions = ''
  bufferedAmount = 0
  binaryType: BinaryType = 'blob'
  onopen: Listener | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: Listener | null = null

  constructor(readonly url: string, private readonly dashboard: boolean) {
    super()
    // Open on a later task, as a real socket does, so the caller has attached
    // its handlers first.
    setTimeout(() => {
      if (this.readyState !== 0) return
      this.readyState = 1
      if (this.dashboard) openDashboardSockets.add(this)
      this.fire(new Event('open'), this.onopen)
    }, 0)
  }

  deliver(raw: string): void {
    if (this.readyState !== 1) return
    this.fire(new MessageEvent('message', { data: raw }), this.onmessage as Listener | null)
  }

  send(): void {
    // Client-to-server frames (focus, read marks, subscriptions) need no answer.
  }

  close(code = 1000, reason = ''): void {
    if (this.readyState >= 2) return
    this.readyState = 3
    openDashboardSockets.delete(this)
    this.fire(new CloseEvent('close', { code, reason, wasClean: true }), this.onclose as Listener | null)
  }

  private fire(ev: Event, handler: Listener | null): void {
    handler?.call(this, ev)
    this.dispatchEvent(ev)
  }
}
