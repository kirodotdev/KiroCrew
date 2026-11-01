/**
 * The iframes app cards run in: creating, placing, sizing and dismissing them.
 *
 * One hover frame per provider shows the card for the link under the pointer. A
 * card the app pins is detached from hovering: it keeps its own frame, stays when
 * the pointer leaves or the chat scrolls, and the next hover gets a fresh frame.
 * Each frame is `sandbox="allow-scripts"` (no `allow-same-origin`) and loads the
 * gateway's frame document, so the app's code never runs in the dashboard's
 * document. See `linkPreviewProtocol.ts` for the messages.
 *
 * @module linkPreview/linkPreviewFrames
 */
import { freshRate, takeMessage, type RateState } from '../appFrame/frameProtocol'
import {
  createSandboxedFrame,
  loadMintedFrame,
  mintAppFrame,
  postToFrame,
  renewIntervalMs,
  type Minted,
} from '../appFrame/sandboxFrame'
import {
  MAX_MESSAGE_BYTES,
  messageBytes,
  readFrameMessage,
  type AnchorRect,
  type FromFrame,
  type Modifiers,
  type ToFrame,
} from './linkPreviewProtocol'

export { FRAME_SANDBOX, type Minted } from '../appFrame/sandboxFrame'
/** Waiting for the module to activate; a frame that has not said `ready` by then failed. */
export const READY_TIMEOUT_MS = 10_000
/** At most this many pinned cards at once; pinning one more closes the oldest. */
export const MAX_PINNED = 8
const MARGIN = 8
const GAP = 6
const DEFAULT_SIZE = { width: 360, height: 200 }
const MIN_SIZE = { width: 40, height: 24 }
const MAX_SIZE = { width: 720, height: 800 }

export interface PreviewFrame {
  key: string
  appName: string
  el: HTMLIFrameElement
  state: 'loading' | 'ready' | 'failed'
  ready: Promise<boolean>
  visible: boolean
  pinned: boolean
  /** Dragged by the user: placed where it was dropped, not by its anchor. */
  moved: boolean
  dock: 'left' | 'right' | null
  left: number
  top: number
  width: number
  height: number
  anchor: AnchorRect | null
  frameId?: string
  /** Overlay mode (`regions`): the frame covers the viewport, clipped to `regions`. */
  overlay: boolean
  regions: AnchorRect[]
  /** The latest show's number; a card message for an older one is dropped. */
  invocation: number
  /** Burst allowance left, and when it was last refilled. */
  rate: RateState
}

export interface FrameHostOptions {
  /** The frame URL and capability for a provider (`POST /api/apps/<app>/frames/linkPreviews/<id>`);
   *  with `frameId`, a renewal for that frame. */
  mint(appName: string, frameId?: string, providerId?: string): Promise<Minted>
  theme(): 'dark' | 'light'
  /** The frame could not load or activate the module. */
  onFailed(frame: PreviewFrame, why: string): void
  onTextPatternsOff(appName: string, ids: string[]): void
  /** Escape pressed inside a card. */
  onEscape(): void
  /** The set of live frames changed (the link bridge listens to them). */
  onFramesChanged(frames: HTMLIFrameElement[]): void
}

export function mintFrame(appName: string, frameId?: string, providerId = ''): Promise<Minted> {
  return mintAppFrame(appName, 'linkPreviews', providerId, frameId)
}

/** A provider key is `${appName}:${id}`; an app name holds no `:`. */
const providerIdOf = (frame: { key: string; appName: string }): string => frame.key.slice(frame.appName.length + 1)

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(n, Math.max(lo, hi)))

export class FrameHost {
  private readonly opts: FrameHostOptions
  private readonly root: HTMLElement
  /** provider key -> its hover frame */
  private readonly hover = new Map<string, PreviewFrame>()
  /** Pinned or once-pinned cards, oldest first. */
  private readonly detached: PreviewFrame[] = []
  private readonly renewals = new Map<PreviewFrame, number>()
  /** How a loading frame settles: its module activated, or it did not. */
  private readonly settlers = new Map<PreviewFrame, { ready(): void; fail(why: string): void; cancel(): void }>()
  /** Bumped by every leave and dismissal, so a show still waiting for a loading
   *  frame is dropped instead of opening a card the user already left. */
  private readonly showTokens = new Map<PreviewFrame, number>()

  constructor(opts: FrameHostOptions, root: HTMLElement = document.body) {
    this.opts = opts
    this.root = root
    this.onMessage = this.onMessage.bind(this)
    this.onResize = this.onResize.bind(this)
    window.addEventListener('message', this.onMessage)
    window.addEventListener('resize', this.onResize)
  }

  frames(): PreviewFrame[] {
    return [...this.hover.values(), ...this.detached]
  }

  isFrame(el: unknown): boolean {
    return el instanceof HTMLIFrameElement && this.frames().some((f) => f.el === el)
  }

  /** The hover frame for a provider, created (and its module loading) on first use. */
  hoverFrame(key: string, appName: string): PreviewFrame {
    const have = this.hover.get(key)
    if (have && have.state !== 'failed') return have
    const frame = this.create(key, appName)
    this.hover.set(key, frame)
    this.changed()
    return frame
  }

  /** Show `link` in the provider's hover frame, next to `anchor`. */
  async show(
    frame: PreviewFrame,
    link: { url?: string; text?: string },
    anchor: AnchorRect,
    modifiers?: Modifiers,
  ): Promise<boolean> {
    const token = this.cancelShow(frame)
    const ok = await frame.ready
    if (!ok || !frame.el.isConnected || this.showTokens.get(frame) !== token) return false
    frame.anchor = anchor
    if (!frame.overlay) {
      frame.moved = false
      frame.dock = null
      this.place(frame)
    }
    frame.invocation += 1
    this.post(frame, { type: 'show', invocation: frame.invocation, ...link, rect: anchor, theme: this.opts.theme(), ...(modifiers ? { modifiers } : {}) })
    return true
  }

  leave(key: string): void {
    const frame = this.hover.get(key)
    if (!frame) return
    this.cancelShow(frame)
    if (frame.state === 'ready') this.post(frame, { type: 'leave' })
  }

  /** Invalidate a pending show; returns the new token. */
  private cancelShow(frame: PreviewFrame): number {
    const next = (this.showTokens.get(frame) ?? 0) + 1
    this.showTokens.set(frame, next)
    return next
  }

  /** Scroll, Escape, blur, another provider: every card that is not pinned goes. */
  hideUnpinned(reason: string, except?: string): void {
    for (const [key, frame] of this.hover) {
      if (key === except) continue
      this.cancelShow(frame)
      // An overlay provider keeps its own pinned windows: it is told, and decides.
      if (frame.overlay) {
        if (frame.state === 'ready') this.post(frame, { type: 'hide', reason })
        continue
      }
      // Dismissal retires the show it answered, visible yet or not: a late `open`
      // (or `resize`) for it is stale from here on.
      frame.invocation += 1
      if (frame.visible) this.setVisible(frame, false)
      if (frame.state === 'ready') this.post(frame, { type: 'hide', reason })
    }
    for (const frame of [...this.detached]) {
      if (frame.pinned) continue
      this.post(frame, { type: 'hide', reason })
      this.destroy(frame)
    }
  }

  /** The app is gone (disabled, uninstalled): every frame of its provider goes. */
  disposeKey(key: string): void {
    for (const frame of this.frames()) if (frame.key === key) this.destroy(frame, 'disabled')
  }

  disposeAll(): void {
    for (const frame of this.frames()) this.destroy(frame, 'disabled')
    window.removeEventListener('message', this.onMessage)
    window.removeEventListener('resize', this.onResize)
  }

  /** Hand a key press to the visible cards (their own shortcuts). The caller has
   *  already checked that the dashboard is not taking text input. */
  forwardKey(e: KeyboardEvent): boolean {
    let sent = false
    for (const frame of this.frames()) {
      if (frame.state !== 'ready' || !frame.visible) continue
      this.post(frame, { type: 'key', key: e.key, code: e.code, shift: e.shiftKey })
      sent = true
    }
    return sent
  }

  setTheme(theme: 'dark' | 'light'): void {
    for (const frame of this.frames()) if (frame.state === 'ready') this.post(frame, { type: 'theme', theme })
  }

  private create(key: string, appName: string): PreviewFrame {
    const el = createSandboxedFrame(appName)
    el.dataset.linkPreviewFrame = key
    Object.assign(el.style, {
      // Placed by `place` / `layoutOverlay` before it is ever shown.
      position: 'fixed',
      width: `${DEFAULT_SIZE.width}px`,
      height: `${DEFAULT_SIZE.height}px`,
      border: '0',
      background: 'transparent',
      colorScheme: 'normal',
      zIndex: '2147482000',
      visibility: 'hidden',
      pointerEvents: 'none',
    })
    let settle: (ok: boolean) => void = () => {}
    const ready = new Promise<boolean>((resolve) => { settle = resolve })
    const frame: PreviewFrame = {
      key, appName, el, state: 'loading', ready, visible: false, pinned: false, moved: false, dock: null,
      left: 0, top: 0, width: DEFAULT_SIZE.width, height: DEFAULT_SIZE.height, anchor: null,
      overlay: false, regions: [], invocation: 0, rate: freshRate(),
    }
    const fail = (why: string) => {
      if (frame.state !== 'loading') return
      frame.state = 'failed'
      settle(false)
      this.opts.onFailed(frame, why)
      this.destroy(frame)
    }
    const timer = window.setTimeout(() => fail('timeout'), READY_TIMEOUT_MS)
    this.settlers.set(frame, {
      ready: () => {
        window.clearTimeout(timer)
        if (frame.state !== 'loading') return
        frame.state = 'ready'
        settle(true)
      },
      fail: (why) => {
        window.clearTimeout(timer)
        fail(why)
      },
      cancel: () => window.clearTimeout(timer),
    })
    this.opts.mint(appName, undefined, providerIdOf(frame)).then(
      (minted) => {
        if (!this.frames().includes(frame)) return
        frame.frameId = minted.frameId
        loadMintedFrame(el, this.root, minted, {
          theme: this.opts.theme,
          onNavigatedAway: () => {
            // eslint-disable-next-line no-console -- an app's frame navigating away is otherwise silent
            console.warn(`[link-preview] ${frame.key}: the card frame navigated away and was closed`)
            this.destroy(frame)
          },
        })
        // Renew the capability well before it lapses, while the frame lives.
        this.renewals.set(frame, window.setInterval(() => { void this.renew(frame) }, renewIntervalMs(minted.ttl)))
      },
      () => {
        window.clearTimeout(timer)
        fail('mint')
      },
    )
    return frame
  }

  private async renew(frame: PreviewFrame): Promise<void> {
    try {
      const minted = await this.opts.mint(frame.appName, frame.frameId, providerIdOf(frame))
      if (frame.el.isConnected) this.post(frame, { type: 'capability', capability: minted.capability })
    } catch {
      // The frame retries on its next 401; nothing to show here.
    }
  }

  private destroy(frame: PreviewFrame, reason?: string): void {
    if (reason && frame.state === 'ready') {
      this.post(frame, { type: 'hide', reason })
      this.post(frame, { type: 'dispose' })
    }
    const interval = this.renewals.get(frame)
    if (interval !== undefined) window.clearInterval(interval)
    this.renewals.delete(frame)
    this.settlers.get(frame)?.cancel()
    this.settlers.delete(frame)
    this.showTokens.delete(frame)
    if (this.hover.get(frame.key) === frame) this.hover.delete(frame.key)
    const at = this.detached.indexOf(frame)
    if (at >= 0) this.detached.splice(at, 1)
    frame.el.remove()
    this.changed()
  }

  private post(frame: PreviewFrame, msg: ToFrame): void {
    postToFrame(frame.el, msg)
  }

  private changed(): void {
    this.opts.onFramesChanged(this.frames().map((f) => f.el))
  }

  private setVisible(frame: PreviewFrame, visible: boolean): void {
    frame.visible = visible
    frame.el.style.visibility = visible ? 'visible' : 'hidden'
    frame.el.style.pointerEvents = visible ? 'auto' : 'none'
  }

  private place(frame: PreviewFrame): void {
    const vw = window.innerWidth
    const vh = window.innerHeight
    frame.width = clamp(frame.width, MIN_SIZE.width, Math.min(MAX_SIZE.width, vw - 2 * MARGIN))
    frame.height = clamp(frame.height, MIN_SIZE.height, Math.min(MAX_SIZE.height, vh - 2 * MARGIN))
    const { width: w, height: h } = frame
    if (frame.dock) {
      frame.left = frame.dock === 'left' ? MARGIN : vw - w - MARGIN
    } else if (!frame.moved && frame.anchor) {
      const a = frame.anchor
      frame.left = a.left
      const below = a.top + a.height + GAP
      frame.top = below + h <= vh - MARGIN ? below : a.top - GAP - h
    }
    frame.left = clamp(frame.left, MARGIN, vw - w - MARGIN)
    frame.top = clamp(frame.top, MARGIN, vh - h - MARGIN)
    Object.assign(frame.el.style, {
      left: `${Math.round(frame.left)}px`,
      top: `${Math.round(frame.top)}px`,
      width: `${Math.round(w)}px`,
      height: `${Math.round(h)}px`,
    })
  }

  /** Overlay mode: the whole viewport, clipped to the regions the provider draws in.
   *  A clipped-out area is not hit-tested, so the page under it stays clickable. */
  private layoutOverlay(frame: PreviewFrame): void {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const rects: AnchorRect[] = []
    for (const r of frame.regions) {
      const left = clamp(r.left, 0, vw)
      const top = clamp(r.top, 0, vh)
      const width = Math.min(r.left + r.width, vw) - left
      const height = Math.min(r.top + r.height, vh) - top
      if (width > 0 && height > 0) rects.push({ left, top, width, height })
    }
    Object.assign(frame.el.style, { left: '0px', top: '0px', width: `${vw}px`, height: `${vh}px` })
    // One closed subpath per rect: move, then horizontal, vertical, horizontal, close.
    const d = rects
      .flatMap((r) => ['M', r.left, r.top, 'h', r.width, 'v', r.height, 'h', -r.width, 'Z'].map((x) => (typeof x === 'number' ? Math.round(x) : x)))
      .join(' ')
    // No regions: the frame is hidden and takes no pointer events (setVisible).
    // A CSS `path()` shape, not copy (`path` is an exempt key for the i18n gate).
    const shape = { path: `path('${d}')` }
    frame.el.style.clipPath = d ? shape.path : ''
    this.setVisible(frame, rects.length > 0)
  }

  private onResize(): void {
    for (const frame of this.frames()) {
      if (frame.overlay) this.layoutOverlay(frame)
      else if (frame.visible) this.place(frame)
    }
  }

  private onMessage(e: MessageEvent): void {
    if (!e.source) return
    const frame = this.frames().find((f) => f.el.contentWindow === e.source)
    if (!frame) return
    // Bounded: an oversized message, or one past the burst allowance, is dropped unread.
    if (!takeMessage(frame.rate)) return
    if (messageBytes(e.data) > MAX_MESSAGE_BYTES) return
    const msg = readFrameMessage(e.data)
    if (msg) this.handle(frame, msg)
  }

  /** Act on one message from `frame`. Public for tests. */
  handle(frame: PreviewFrame, msg: FromFrame): void {
    switch (msg.type) {
      case 'ready': this.settlers.get(frame)?.ready(); return
      case 'failed': this.settlers.get(frame)?.fail(msg.message || 'failed'); return
      case 'capabilityExpired': void this.renew(frame); return
      case 'textPatternsOff': this.opts.onTextPatternsOff(frame.appName, msg.ids); return
      case 'escape': this.opts.onEscape(); return
    }
    if (frame.state !== 'ready') return
    if (msg.type === 'regions') {
      frame.overlay = true
      frame.regions = msg.rects
      this.layoutOverlay(frame)
      return
    }
    // An overlay provider moves its own windows; card messages do not apply.
    if (frame.overlay) return
    // A card message answers one show; one for an older show is stale.
    if ('invocation' in msg && msg.invocation !== frame.invocation) return
    switch (msg.type) {
      case 'open':
        this.place(frame)
        this.setVisible(frame, true)
        return
      case 'close':
        if (this.detached.includes(frame)) this.destroy(frame)
        else this.setVisible(frame, false)
        return
      case 'resize':
        frame.width = msg.width
        frame.height = msg.height
        this.place(frame)
        return
      case 'pin':
        frame.pinned = msg.pinned
        if (msg.pinned && this.hover.get(frame.key) === frame) {
          this.hover.delete(frame.key)
          this.detached.push(frame)
          while (this.detached.filter((f) => f.pinned).length > MAX_PINNED) {
            const oldest = this.detached.find((f) => f.pinned)
            if (!oldest) break
            this.destroy(oldest, 'other')
          }
          this.changed()
        }
        return
      case 'drag':
        frame.moved = true
        frame.dock = null
        frame.left += msg.dx
        frame.top += msg.dy
        this.place(frame)
        return
      case 'dock':
        frame.dock = msg.edge
        frame.moved = true
        this.place(frame)
        return
    }
  }
}
