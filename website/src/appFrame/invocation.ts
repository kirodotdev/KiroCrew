/**
 * The host side of one invocation: one user act, one answer.
 *
 * A click-driven point (the user presses something the host drew) opens one
 * invocation per act. This class owns its state machine so every such point
 * behaves the same:
 *
 *   start(fields)                  -> OPEN       posts `<invokeType> { invocationId, deadlineMs, ...fields }`
 *   result.offer (current, seq up) -> OPEN       answered `result.ack`; `onOffer(kind, value)`
 *   result.offer (anything else)   -> unchanged  answered `result.error { code }`
 *   invocation.resize / .close / .error (current only) -> the callbacks
 *   deadline (no resize or accepted offer for `deadlineMs`) -> CANCELLED, `onDeadline()`
 *   cancel(reason)                 -> CANCELLED  posts `invoke.cancel { invocationId, reason }`
 *
 * The id is minted here (`crypto.randomUUID`); the frame cannot make one, and the
 * frame runtime freezes each invocation's controls with its id
 * (`apps/app_frame.py`, `_INVOCATION`), so a late call from an earlier act names
 * an id that is no longer current and is refused. Offers are accepted once per
 * `seq`, in rising order. The host acts on an accepted offer only through its own
 * buttons (Copy, Open), on the user's click.
 *
 * @module appFrame/invocation
 */
import { offerRefusal, type InvocationMessage, type OfferKind } from './frameProtocol'
import { postToFrame } from './sandboxFrame'

export type InvocationState = 'idle' | 'open' | 'done'

export interface InvocationCallbacks {
  onOffer(kind: OfferKind, value: string): void
  onResize?(width: number, height: number): void
  onClose?(): void
  onError?(message: string): void
  /** The deadline passed; the host should close its surface. */
  onDeadline?(): void
}

export function newInvocationId(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

export class HostInvocation {
  readonly id = newInvocationId()
  state: InvocationState = 'idle'
  private lastSeq = 0
  private timer: ReturnType<typeof setTimeout> | undefined

  constructor(
    private readonly frame: HTMLIFrameElement,
    /** The point's own invoke message type, e.g. `message.invoke`. */
    private readonly invokeType: string,
    private readonly deadlineMs: number,
    private readonly cb: InvocationCallbacks,
  ) {}

  /** Open the invocation: send the point's invoke message with this id and the deadline. */
  start(fields: Record<string, unknown>): void {
    if (this.state !== 'idle') return
    this.state = 'open'
    postToFrame(this.frame, { ...fields, type: this.invokeType, invocationId: this.id, deadlineMs: this.deadlineMs })
    this.arm()
  }

  /** End it without a result. Safe to call in any state. */
  cancel(reason: string): void {
    const wasOpen = this.state === 'open'
    this.finish()
    if (wasOpen) postToFrame(this.frame, { type: 'invoke.cancel', invocationId: this.id, reason })
  }

  /** Answer one frame message. Returns true when it belonged to the invocation contract. */
  handle(msg: InvocationMessage): boolean {
    const current = msg.type !== 'ready' && msg.type !== 'failed' && msg.type !== 'capabilityExpired' && msg.type !== 'escape'
      && msg.invocationId === this.id && this.state === 'open'
    switch (msg.type) {
      case 'result.offer': {
        if (msg.seq < 0) return true
        const base = { invocationId: msg.invocationId, seq: msg.seq }
        if (!current || msg.seq <= this.lastSeq) {
          postToFrame(this.frame, { type: 'result.error', ...base, code: 'stale' })
          return true
        }
        this.lastSeq = msg.seq
        const refusal = offerRefusal(msg.kind, msg.value)
        if (refusal || !msg.kind) {
          postToFrame(this.frame, { type: 'result.error', ...base, code: refusal ?? 'not_allowed' })
          return true
        }
        this.cb.onOffer(msg.kind, msg.value)
        postToFrame(this.frame, { type: 'result.ack', ...base })
        this.arm()
        return true
      }
      case 'invocation.resize':
        if (current) {
          this.cb.onResize?.(msg.width, msg.height)
          this.arm()
        }
        return true
      case 'invocation.close':
        if (current) this.cb.onClose?.()
        return true
      case 'invocation.error':
        if (current) {
          this.finish()
          this.cb.onError?.(msg.message)
        }
        return true
      default:
        return false
    }
  }

  private arm(): void {
    if (this.timer !== undefined) clearTimeout(this.timer)
    this.timer = setTimeout(() => {
      if (this.state !== 'open') return
      this.cancel('deadline')
      this.cb.onDeadline?.()
    }, this.deadlineMs)
  }

  private finish(): void {
    if (this.timer !== undefined) clearTimeout(this.timer)
    this.timer = undefined
    this.state = 'done'
  }
}
