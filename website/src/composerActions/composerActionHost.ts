/**
 * One composer-action frame for one click, on the shared app frame.
 *
 * The frame (`appFrame/sandboxFrame.ts`) is made on the click, loads the gateway's
 * document for one action, and runs one invocation (`appFrame/invocation.ts`,
 * `HostInvocation`) once the module says `ready`. This module adds only what the
 * point owns: the `composer.invoke` message (which carries nothing from the
 * composer), the `insert` kind, the ready timeout, the per-frame message budget,
 * capability renewal, and teardown on a self-navigation or a malformed message.
 *
 * @module composerActions/composerActionHost
 */
import {
  displaySafe,
  freshRate,
  MAX_HOST_MESSAGE_BYTES,
  messageBytes,
  readInvocationMessage,
  takeMessage,
  type OfferRefusal,
} from '../appFrame/frameProtocol'
import { HostInvocation } from '../appFrame/invocation'
import { visibleOnly } from './literalInsert'
import {
  createSandboxedFrame,
  loadMintedFrame,
  mintAppFrame,
  postToFrame,
  renewIntervalMs,
  type Minted,
} from '../appFrame/sandboxFrame'

export const COMPOSER_POINT = 'composerActions'
export const COMPOSER_INVOKE = 'composer.invoke'
/** A frame that has not said `ready` this long after it was minted failed. */
export const COMPOSER_READY_MS = 5_000
const INSERT_ONLY = new Set(['insert'] as const)

/** Why an invocation ended without inserting. */
export type EndReason = 'cancelled' | 'deadline' | 'failed' | 'app_error' | 'closed' | 'revoked'

export interface ComposerActionHostOptions {
  appName: string
  actionId: string
  container: HTMLElement
  theme(): 'dark' | 'light'
  deadlineMs: number
  /** Safe text the app offered. Return a code to refuse it; nothing to take it. */
  onText(text: string): void | OfferRefusal
  onResize?(height: number): void
  onEnd(reason: EndReason, detail?: string): void
  onEscape?(): void
  /** Overridable in tests. */
  mint?: (frameId?: string) => Promise<Minted>
}

export class ComposerActionHost {
  readonly el: HTMLIFrameElement
  private readonly opts: ComposerActionHostOptions
  private readonly rate = freshRate()
  private invocation: HostInvocation | null = null
  private frameId = ''
  private readyTimer: ReturnType<typeof setTimeout> | undefined
  private renewTimer: ReturnType<typeof setInterval> | undefined
  private ended = false
  private taken = false

  constructor(opts: ComposerActionHostOptions) {
    this.opts = opts
    this.el = createSandboxedFrame(opts.appName)
    this.el.dataset.composerActionFrame = `${opts.appName}:${opts.actionId}`
    Object.assign(this.el.style, { border: '0', width: '100%', height: '100%', background: 'transparent', colorScheme: 'normal', display: 'block' })
    this.onMessage = this.onMessage.bind(this)
  }

  private mint(frameId?: string): Promise<Minted> {
    return this.opts.mint ? this.opts.mint(frameId) : mintAppFrame(this.opts.appName, COMPOSER_POINT, this.opts.actionId, frameId)
  }

  start(): void {
    window.addEventListener('message', this.onMessage)
    this.readyTimer = setTimeout(() => this.end('failed', 'ready'), COMPOSER_READY_MS)
    this.mint().then(
      (minted) => {
        if (this.ended) return
        this.frameId = minted.frameId ?? ''
        loadMintedFrame(this.el, this.opts.container, minted, {
          theme: this.opts.theme,
          onNavigatedAway: () => this.end('failed', 'navigated'),
        })
        this.renewTimer = setInterval(() => { void this.renew() }, renewIntervalMs(minted.ttl))
      },
      () => this.end('failed', 'mint'),
    )
  }

  /** The person closed the surface, or the host ends it for its own reason. */
  cancel(reason: 'cancelled' | 'revoked' = 'cancelled'): void {
    if (this.ended) return
    this.invocation?.cancel(reason)
    this.end(reason)
  }

  private async renew(): Promise<void> {
    try {
      const minted = await this.mint(this.frameId)
      if (!this.ended) postToFrame(this.el, { type: 'capability', capability: minted.capability })
    } catch {
      // The frame asks again on its next refusal.
    }
  }

  private end(reason: EndReason, detail?: string): void {
    if (this.ended) return
    this.dispose()
    this.opts.onEnd(reason, detail)
  }

  /** Remove the frame. Safe to call more than once. */
  dispose(): void {
    if (this.ended) return
    this.ended = true
    clearTimeout(this.readyTimer)
    clearInterval(this.renewTimer)
    window.removeEventListener('message', this.onMessage)
    this.invocation?.settle()
    if (this.el.isConnected) {
      postToFrame(this.el, { type: 'dispose' })
      this.el.remove()
    }
  }

  private onMessage(e: MessageEvent): void {
    if (this.ended || !e.source || e.source !== this.el.contentWindow) return
    if (!takeMessage(this.rate)) { this.end('failed', 'rate'); return }
    if (messageBytes(e.data) > MAX_HOST_MESSAGE_BYTES) { this.end('failed', 'too_large'); return }
    const msg = readInvocationMessage(e.data)
    if (!msg) return
    switch (msg.type) {
      case 'ready':
        if (this.invocation) return
        clearTimeout(this.readyTimer)
        this.invocation = new HostInvocation(this.el, COMPOSER_INVOKE, this.opts.deadlineMs, {
          onOffer: (_kind, value) => {
            // One insert per click, whatever state `settle()` leaves the invocation in.
            if (this.taken) return 'stale'
            // Only what the person can see goes in: text that draws nothing is dropped.
            const text = visibleOnly(displaySafe(value.replace(/\r\n?/g, '\n')))
            if (!text.trim()) return 'not_allowed'
            const refused = this.opts.onText(text)
            if (refused) return refused
            this.taken = true
            // The result is in: stop the deadline and remove the frame after the
            // ack goes out. No `invoke.cancel` follows (dispose never sends one).
            this.invocation?.settle()
            queueMicrotask(() => this.dispose())
            return undefined
          },
          onResize: (_w, h) => this.opts.onResize?.(h),
          onClose: () => this.end('closed'),
          onError: (message) => this.end('app_error', message),
          onDeadline: () => this.end('deadline'),
        }, { allowedKinds: INSERT_ONLY })
        // Nothing from the composer: no draft, no selection, no session.
        this.invocation.start({})
        return
      case 'failed':
        this.end('failed', msg.message)
        return
      case 'capabilityExpired':
        void this.renew()
        return
      case 'escape':
        this.opts.onEscape?.()
        return
      default:
        this.invocation?.handle(msg)
    }
  }
}
