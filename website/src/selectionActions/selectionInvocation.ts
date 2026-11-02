/**
 * One selection-action invocation: its frame, its lifecycle and its host effects.
 *
 * A pick on an item opens exactly one invocation on the shared app-frame contract
 * (`appFrame/invocation.ts`). The host creates the item's sandboxed frame, and
 * once the module is `ready` the `HostInvocation` sends it `selection.invoke` with
 * its own `invocationId`, the selected text and the deadline. The contract then
 * owns offers, resize, close, error, deadline and cancel. The item's `effects`
 * are its `allowedKinds`, so an offer of any other kind is refused `not_allowed`;
 * a resize is clamped to the item's `popover.maxHeight`.
 *
 * The frame never writes the clipboard or opens a window: an accepted offer only
 * fills the host's own Copy or Open button, which acts on the user's click.
 *
 * @module selectionActions/selectionInvocation
 */
import {
  MAX_HOST_MESSAGE_BYTES,
  displaySafe,
  freshRate,
  messageBytes,
  readInvocationMessage,
  takeMessage,
  type InvocationMessage,
} from '../appFrame/frameProtocol'
import { HostInvocation } from '../appFrame/invocation'
import {
  createSandboxedFrame,
  loadMintedFrame,
  postToFrame,
  renewIntervalMs,
  type Minted,
} from '../appFrame/sandboxFrame'
import type { ResolvedSelectionAction } from './selectionActionRegistry'
import type { SelectedText } from './selectionSources'

export const SELECTION_POINT = 'selectionActions'
/** The point's invoke message on the shared contract. */
export const SELECTION_INVOKE = 'selection.invoke'
/** How long an invocation may stay open with no resize and no accepted offer. */
export const SELECTION_DEADLINE_MS = 120_000
/** Waiting for the module to activate; a frame that has not said `ready` by then failed. */
export const READY_TIMEOUT_MS = 10_000
export const MIN_FRAME_HEIGHT = 48
const DEFAULT_FRAME_HEIGHT = 160

export type InvocationPhase = 'loading' | 'open' | 'done' | 'failed' | 'cancelled'

export interface InvocationView {
  phase: InvocationPhase
  height: number
  /** A Copy value the frame offered and the host accepted. */
  copy: string | null
  /** An Open URL the frame offered and the host accepted. */
  open: URL | null
  /** Shown in host chrome: the frame never responded, or the app reported a failure. */
  notice: { kind: 'no_response' } | { kind: 'error'; message: string } | null
}

export interface InvocationOptions {
  item: ResolvedSelectionAction
  selected: SelectedText
  /** Where the frame is attached; the popover body. */
  box: HTMLElement
  mint(appName: string, point: string, pointId: string, frameId?: string): Promise<Minted>
  theme(): 'dark' | 'light'
  onView(view: InvocationView): void
  /** The invocation ended and its surface should close. */
  onClose(): void
}

export class SelectionInvocation {
  readonly el: HTMLIFrameElement
  private readonly opts: InvocationOptions
  private readonly contract: HostInvocation
  private view: InvocationView
  private readonly rate = freshRate()
  private frameId: string | undefined
  private readyTimer: number | undefined
  private renewTimer: number | undefined
  private ready = false

  constructor(opts: InvocationOptions) {
    this.opts = opts
    this.view = { phase: 'loading', height: DEFAULT_FRAME_HEIGHT, copy: null, open: null, notice: null }
    this.el = createSandboxedFrame(opts.item.appLabel)
    this.el.dataset.selectionActionFrame = opts.item.key
    Object.assign(this.el.style, { display: 'block', width: '100%', height: '100%', border: '0', background: 'transparent', colorScheme: 'normal' })
    this.contract = new HostInvocation(this.el, SELECTION_INVOKE, SELECTION_DEADLINE_MS, {
      onOffer: (kind, value) => {
        if (kind === 'copy') this.update({ copy: value })
        else this.update({ open: new URL(value) })
      },
      onResize: (_width, height) => {
        this.update({ height: Math.max(MIN_FRAME_HEIGHT, Math.min(Math.round(height), opts.item.maxHeight)) })
      },
      onClose: () => {
        // The frame ended it: settled, so no cancel is sent.
        this.contract.settle()
        this.update({ phase: 'done' })
        this.teardown()
        opts.onClose()
      },
      onError: (message) => this.fail({ kind: 'error', message: displaySafe(message) }),
      onDeadline: () => {
        this.update({ phase: 'cancelled' })
        this.teardown()
        opts.onClose()
      },
    }, {
      // The item's grant names the host buttons it may fill; any other kind is refused.
      allowedKinds: opts.item.effects,
    })
    this.onMessage = this.onMessage.bind(this)
  }

  /** The id of this invocation, minted by the shared contract. */
  get invocationId(): string {
    return this.contract.id
  }

  start(): void {
    window.addEventListener('message', this.onMessage)
    this.readyTimer = window.setTimeout(() => this.fail({ kind: 'no_response' }), READY_TIMEOUT_MS)
    const { item } = this.opts
    this.opts.mint(item.appName, SELECTION_POINT, item.id).then(
      (minted) => {
        if (this.view.phase !== 'loading') return
        this.frameId = minted.frameId
        // A second load is a self-navigation: its contents are no longer the gateway's.
        loadMintedFrame(this.el, this.opts.box, minted, { theme: this.opts.theme, onNavigatedAway: () => this.fail({ kind: 'no_response' }) })
        this.renewTimer = window.setInterval(() => { void this.renew() }, renewIntervalMs(minted.ttl))
      },
      () => this.fail({ kind: 'no_response' }),
    )
  }

  /** End the invocation from the host side and remove its frame. */
  dispose(): void {
    if (this.view.phase === 'open' || this.view.phase === 'loading') {
      this.contract.cancel('closed')
      this.update({ phase: 'cancelled' })
    }
    if (this.ready) postToFrame(this.el, { type: 'dispose' })
    this.teardown()
  }

  setTheme(theme: 'dark' | 'light'): void {
    if (this.ready) postToFrame(this.el, { type: 'theme', theme })
  }

  current(): InvocationView {
    return this.view
  }

  private teardown(): void {
    window.removeEventListener('message', this.onMessage)
    window.clearTimeout(this.readyTimer)
    window.clearInterval(this.renewTimer)
    this.el.remove()
  }

  private update(patch: Partial<InvocationView>): void {
    this.view = { ...this.view, ...patch }
    this.opts.onView(this.view)
  }

  private fail(notice: NonNullable<InvocationView['notice']>): void {
    if (this.view.phase !== 'loading' && this.view.phase !== 'open') return
    this.contract.cancel('closed')
    this.update({ phase: 'failed', notice, copy: null, open: null })
    this.teardown()
  }

  private async renew(): Promise<void> {
    try {
      const minted = await this.opts.mint(this.opts.item.appName, SELECTION_POINT, this.opts.item.id, this.frameId)
      if (this.el.isConnected) postToFrame(this.el, { type: 'capability', capability: minted.capability })
    } catch {
      // The frame retries on its next refusal; nothing to show here.
    }
  }

  private onMessage(e: MessageEvent): void {
    if (!e.source || e.source !== this.el.contentWindow) return
    // Bounded: past the burst allowance or over the size cap, the frame is torn down.
    if (!takeMessage(this.rate) || messageBytes(e.data) > MAX_HOST_MESSAGE_BYTES) {
      this.fail({ kind: 'no_response' })
      return
    }
    const msg = readInvocationMessage(e.data)
    if (msg) this.handle(msg)
  }

  /** Act on one message from the frame. Public for tests. */
  handle(msg: InvocationMessage): void {
    switch (msg.type) {
      case 'ready':
        if (this.ready || this.view.phase !== 'loading') return
        this.ready = true
        window.clearTimeout(this.readyTimer)
        this.update({ phase: 'open' })
        this.contract.start({
          text: this.opts.selected.text,
          truncated: this.opts.selected.truncated,
          source: this.opts.selected.source,
          theme: this.opts.theme(),
        })
        return
      case 'failed':
        this.fail({ kind: 'no_response' })
        return
      case 'capabilityExpired':
        void this.renew()
        return
      case 'escape':
        this.opts.onClose()
        return
      default:
        this.contract.handle(msg)
    }
  }
}
