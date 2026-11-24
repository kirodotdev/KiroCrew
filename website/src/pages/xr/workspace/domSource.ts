/**
 * A page drawn by the XR tab itself (HTML-in-Canvas): a same-origin `<iframe>` of the dashboard page is a child of the
 * room's canvas, the browser lays it out and paints it, and each new paint of it is copied into the panel's texture
 * with `gl.texElementSubImage2D`. No page browser on the gateway, no picture on the socket.
 *
 * The browser's own hit test sends the pointer into the page: the panel tells it each frame where the page is drawn
 * on the canvas (`place`, `canvas.updateElementGeometry`). So a tap, the caret, the wheel, the keys and a native list
 * are the page's own, and the panel's press path sends nothing (`pointer`, `scroll`, `key` and `text` do nothing).
 *
 * Chosen for a page only when the address asks for it (`source=dom`) and the browser has the API (`domPagesWork`);
 * else the page is streamed (`streamSource.ts`), as before. In a session the session's pointers hand their presses
 * in under the rules of `domAim.ts`, and a page whose paint stops behind the session says so (`paintStopped`). Learned by the measuring line
 * (iterations/iter-12/h2c/H2C-1.md): a page is marked changed only inside the canvas's `paint` event (before its
 * first paint the browser has no picture of it); `gl.getError()` is never asked in the steady state (it waits for
 * the graphics process); the texture is allocated anew when the picture's size changes (the canvas's scale changed),
 * or an upload into the old one is refused. three's own `HTMLTexture` (0.187) uses names that Chrome 157 and 158 do
 * not have, so the texture is an `ExternalTexture` over a WebGL texture of this source's own.
 */
import { ExternalTexture, LinearFilter, Matrix4, SRGBColorSpace, type Camera, type Object3D, type Texture, type WebGLRenderer } from 'three'
import { pickCopy } from './domCopies'
import type { PageFrame, PageInput, PageNote, PagePointer, PageReadout, PageScroll, PageSource, PageStatus, PageTarget } from './source'
import { act, scrollAt } from './domAct'
import { aimAt, pressFate, rectOf, samePlace, type Lit, type Refusal } from './domAim'
import { pinchRoute } from './pinchRoute'
import { i18nT } from '../../../i18n/t'
import { livePath, staysOut } from './streamPath'

type DrawableCanvas = HTMLCanvasElement & {
  getElementImageDefaultSize?: (el: Element) => { width: number; height: number }
  updateElementGeometry?: (el: Element, init: { canvasTransform: DOMMatrix }) => void
  requestPaint?: () => void
}
type DrawableGl = WebGL2RenderingContext & { texElementSubImage2D?: (target: number, level: number, x: number, y: number, el: Element) => void }

/** How often the page's address is looked at: a move inside the page fires no event in this document. */
const ADDRESS_EVERY_MS = 250
/**
 * In a session the canvas is asked for a paint when it sent none for `askMs`; when it sends none for `stoppedMs`
 * after that, the browser paints the page no more behind the session, and the page goes to the stream (`paintStopped`).
 */
const PAINT = { askMs: 1000, stoppedMs: 2500 } as const
/** How long the line that tells of a page's own dialog in a session stands under the page (ms). */
const DIALOG_SAID_MS = 5000
/** How long a dialog's own text may be in that line (characters). */
const DIALOG_TEXT_MAX = 90

/** How often a session's pointer looks for a control to snap to, at most (ms): the page's whole list is read for it. */
const AIM_EVERY_MS = 50

const keep = (list: number[], value: number) => { list.push(Math.round(value * 10) / 10); if (list.length > 400) list.shift() }

/** Whether this browser can draw a page of this tab into the room. No context is made to ask. */
/** A paint of a page whose own document did not change since its last copy marks it no more often than this. */
const UNTOUCHED_MS = 250

export function domPagesWork(): boolean {
  if (typeof document === 'undefined' || typeof WebGL2RenderingContext === 'undefined') return false
  return 'requestPaint' in document.createElement('canvas') && 'texElementSubImage2D' in WebGL2RenderingContext.prototype
}

/** A window of another origin opens as the browser's tab only this soon after his own press in the page (ms). */
const OUTSIDE_PRESS_MS = 1000

/**
 * The address a page stands at now, as the panel keeps it: its path, and for the Bots page its one query. A page of
 * the list that stays out of XR (`staysOut`) is never one of them.
 */
export function addressOf(location: { pathname: string; search: string }): string {
  return location.pathname === '/members' && location.search.startsWith('?member=') ? `${location.pathname}${location.search}` : location.pathname
}

export class DomSource implements PageSource {
  /**
   * The page whose own press holds the pointer now (a press that the browser gave it), if any. While one does, no
   * other page drawn by the tab takes the pointer: a release over another page is no release of that page's.
   */
  private static holder: DomSource | null = null
  /**
   * Every page drawn by the tab, and which of them is copied in the room's frame now (`domCopies.ts`): at most one
   * page a frame. The pick is made at the first page's `beforeDraw` of each frame, over every page; a page that the
   * panels tell later in that frame is in view by what they told the frame before.
   */
  private static readonly all = new Set<DomSource>()
  private static pickedFor = -1
  private static frames = 0
  private static winner: DomSource | null = null
  private static last: string | null = null
  /** Room frames it has waited for a copy since the browser painted it anew; and how far it is from the middle of his look. */
  private waited = 0
  private gazeDeg = 180
  private readonly listeners = new Set<() => void>()
  private readonly el: HTMLIFrameElement
  private readonly texture: ExternalTexture
  private canvas: DrawableCanvas | null = null
  private glTexture: WebGLTexture | null = null
  private size: [number, number] = [0, 0]
  /** The browser painted the page anew since the last upload. Set only in the canvas's `paint` event. */
  private changed = false
  private state: PageStatus = 'loading'
  private shown = true
  private firstAt: number | null = null
  /** The address the page stands at, and the last one of them that may be shown in XR. */
  private at: string
  private readonly uploads: number[] = []
  private count = 0
  private readonly timer: ReturnType<typeof setInterval>
  private readonly onPaint = (ev: Event) => {
    if (this.paintless) return
    this.paintAt = performance.now()
    const changed = (ev as Event & { changedElements?: Element[] }).changedElements
    if (changed && !changed.includes(this.el)) return
    // The browser names a page in its paint though nothing in it changed: after our own copy, and for a still page
    // too, many times a second (`DOM-READ.md`). A page is marked changed by its own change: its document, a scroll,
    // an input since the last copy. A paint with none of those marks it at most every `UNTOUCHED_MS`.
    // shortcut: an animation with no change in the document (a CSS loop, a canvas) is copied 4 times a second; read
    // the page's own animations when one shows that.
    // The page he looks at and the page under the pointer are marked at once (a spinner or the typing dots there
    // must not stutter); the one-copy bound still holds for them. A paint that is dropped is marked again
    // `UNTOUCHED_MS` after the last copy (`pick`), so no old picture stays.
    if (!this.touched && performance.now() - this.copiedAt < UNTOUCHED_MS && !this.watched()) { this.dropped = true; return }
    this.dropped = false
    this.changed = true
  }
  /** A paint was dropped by the 250 ms rule: the page is marked again once that time is over. */
  private dropped = false
  /** He gave the page input (a key, a scroll) since its last copy: it goes first in the next frame. */
  private input = false
  private readonly onInput = () => { this.input = true; this.touched = true }
  /** The room frame of its last draw: a page that is not drawn holds no pick. */
  private drawnKey = -1
  /** Whether he watches this page: the pointer is on it, or it is the page in view nearest the middle of his look. */
  private watched(): boolean {
    if (this.hovered || this.pressed || this.lit) return true
    if (!this.shown) return false
    for (const s of DomSource.all) if (s !== this && s.shown && s.gazeDeg < this.gazeDeg) return false
    return true
  }
  /** When it was last copied, and whether its own document changed since (for `onPaint`). */
  private copiedAt = -Infinity
  private touched = true
  private readonly onTouch = () => { this.touched = true }
  private readonly onLoad = () => { this.guardLinks(); this.follow() }
  /** Who hears the person's input inside the page (`listen`). The page's own events are only heard, never stopped. */
  private hear: ((input: PageInput) => void) | null = null
  private readonly heard: Record<string, (ev: Event) => void> = {
    pointerdown: ev => { if (ev.isTrusted) { this.downAt = performance.now(); this.upOutside = false; this.own = true; DomSource.holder = this; this.took(ev as PointerEvent); this.hear?.('down') } },
    pointerup: ev => { if (ev.isTrusted) { this.upOutside = this.outside(ev as PointerEvent); this.letGo(); this.hear?.('up') } },
    pointercancel: ev => { if (ev.isTrusted) { this.letGo(); this.hear?.('up') } },
    wheel: ev => { if (ev.isTrusted) this.hear?.('wheel') },
    keydown: ev => { if (ev.isTrusted) this.hear?.('key') },
    // The mouse inside the page: the room must still see where it is, or a Bot, a handle or another panel that comes
    // under it would never take it back from the page (`hover`).
    pointermove: ev => { if (ev.isTrusted) { if (this.own) this.took(ev as PointerEvent, false); this.moved(ev as PointerEvent) } },
  }
  /** Where the page last heard its own held press, and what took its down: the release that comes outside goes there. */
  private heldAt: { x: number; y: number; el: Element | null } | null = null
  private took(ev: PointerEvent, down = true): void {
    const el = down ? (ev.target as Element | null) : this.heldAt?.el ?? null
    this.heldAt = { x: ev.clientX, y: ev.clientY, el }
  }
  /**
   * The browser sent the page's own release although it came outside the page's picture (it holds a press that began
   * in a frame for that frame). The click that it sends after it is no click of the page's: it is stopped (`noClick`).
   */
  private upOutside = false
  private outside(ev: PointerEvent): boolean { return ev.clientX < 0 || ev.clientY < 0 || ev.clientX >= this.cssWidth || ev.clientY >= this.cssHeight }
  private readonly noClick = (ev: MouseEvent) => {
    if (!this.upOutside || !ev.isTrusted) return
    this.upOutside = false
    ev.preventDefault()
    ev.stopImmediatePropagation()
  }
  private letGo(): void { this.own = false; this.heldAt = null; if (DomSource.holder === this) DomSource.holder = null }
  /**
   * The page's own press is let go OUTSIDE it (on the room, on a handle, on another page, outside the window), or the
   * window loses the pointer: the page never hears that release itself. The hold ends here, and the page hears the
   * end of its press by script at the last place it heard: a `pointerup` and a `mouseup`, no click.
   */
  private readonly endOutside = () => {
    if (!this.own) return
    const at = this.heldAt
    const doc = this.doc
    this.letGo()
    if (doc && at) act(doc, at.x, at.y, 'up', at.el)
    this.hear?.('up')
  }
  /** The pointer left the window: at its edge, toward nothing (going into a page has that page's element, or stays inside). */
  private readonly onTopOut = (ev: PointerEvent) => {
    const edge = ev.clientX <= 0 || ev.clientY <= 0 || ev.clientX >= window.innerWidth - 1 || ev.clientY >= window.innerHeight - 1
    if (!ev.relatedTarget && edge) this.endOutside()
  }
  /** Focus going into a page is no loss of the window's; only a window that lost it altogether ends the hold. */
  private readonly onTopBlur = () => { setTimeout(() => { if (!document.hasFocus()) this.endOutside() }, 0) }
  private readonly onClick = (ev: MouseEvent) => {
    // A link to a page that stays out of XR is not followed inside the panel.
    const link = (ev.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
    if (!link) return
    // A link that names another window opens no browser window: it is a window of the page (`window`).
    const target = (link.getAttribute('target') ?? '').trim().toLowerCase()
    if (target && target !== '_self' && target !== '_top' && target !== '_parent') {
      // A link out of the dashboard that he pressed on the desk opens as a browser tab, as on the dashboard itself.
      if (!this.ownWindow(link.href) && this.outsideMay(ev.isTrusted)) return
      ev.preventDefault(); this.window(link.href); return
    }
    let to: URL
    try { to = new URL(link.href, this.el.contentWindow?.location.href) } catch { return }
    if (to.origin === location.origin && staysOut(to.pathname)) { ev.preventDefault(); ev.stopPropagation() }
  }
  /** Who opens a window that the page opens (`windows`), and when the page last asked for one that may not open. */
  private opens: ((path: string) => void) | null = null
  private windowRefusedAt = -Infinity
  /** When he last pressed in the page himself (the browser's own press). */
  private downAt = -Infinity
  /** Until when the page takes no press of its own (`holdFor`). */
  private heldUntil = 0
  /**
   * A window that the page asked for, by its page's `window.open` or a link that names another window. A page of
   * this origin that a panel may show opens on a panel of its own; any other says the page's line, but for one of
   * another origin that he pressed for on the desk, which the browser opens as its own tab (`outsideMay`).
   */
  private window(url: unknown): void {
    const path = this.ownWindow(url)
    if (path && this.opens) { this.opens(path); return }
    this.windowRefusedAt = performance.now()
    this.tell()
  }
  /** The path of a window of this origin that a panel may show, or `null`. */
  private ownWindow(url: unknown): string | null {
    let to: URL | null = null
    try { to = new URL(String(url ?? '') || 'about:blank', this.el.contentWindow?.location.href) } catch { to = null }
    return to && to.origin === location.origin ? livePath(addressOf(to)) : null
  }
  /**
   * Whether a window of another origin may open as the browser's own tab: only with no session (in the glasses he
   * cannot see a browser's tab) and at his press (the browser's own word for it, a trusted click or the page's
   * transient activation). A window that the page opens by itself never does.
   */
  private outsideMay(trusted: boolean): boolean {
    if (this.inXr) return false
    if (trusted) return true
    // The browser's activation lasts some seconds; his press is the one of the last second (`WINDOW_PRESS_MS` in the workspace).
    const activation = (this.el.contentWindow?.navigator as (Navigator & { userActivation?: { isActive: boolean } }) | undefined)?.userActivation
    return activation?.isActive === true && performance.now() - this.downAt < OUTSIDE_PRESS_MS
  }
  private readonly m = new Matrix4()
  private readonly toUnit = new Matrix4()
  private readonly toCss = new Matrix4()
  /** The room's own ray says that this page's surface is the first thing under the mouse (`hover`). */
  private hovered = false
  /** A press that the room handed in by script, and where (`pointer`). */
  private pressed: { pointerId: number; x: number; y: number; el: Element | null } | null = null
  /** A press that the browser gave the page itself is held: who takes the pointer stays as it was at the down. */
  private own = false
  /** A session runs: the hand-in follows a session's rules (`domAim.ts`), and the paint is watched. */
  private inXr = false
  /** What a session's pointer lights on the page now, why the last pinch pressed nothing, and when it last looked. */
  private lit: Lit | null = null
  private refused: Refusal = null
  /** Whether the room shows a point of this page uncovered, given by the panel (`seenBy`). */
  private sees: ((x: number, y: number) => boolean) | null = null
  private aimedAt = 0
  /** When each element came into the page (`domAim.pressFate`: a control that came a moment ago takes no pinch). */
  private added = new WeakMap<Element, number>()
  private watch: MutationObserver | null = null
  /** The watches of the frames inside the page, at every depth (`coverFrames`). */
  private frameWatch: MutationObserver[] = []
  /** The frames inside the page that this tab cannot reach and whose sandbox lets them open dialogs (`modals`). */
  private farFrames = new Set<HTMLIFrameElement>()
  /** The frames inside the page that this tab can neither reach nor shut: of another site, with no sandbox (`streamWanted`). */
  private openFrames = new Set<HTMLIFrameElement>()
  /** The canvas's last paint event, and when it was last asked for one. `paintless`: a test's stand-in for a browser that stops. */
  private paintAt = 0
  private askedAt = 0
  private stopped = false
  paintless = false
  /** A dialog of the page's own that a session answered for it (`dialogs`): its text, and when. */
  private asked: { text: string; at: number } | null = null

  constructor(readonly id: string, path: string, private cssWidth: number, private cssHeight: number) {
    this.at = path
    this.el = document.createElement('iframe')
    this.el.src = path
    this.el.title = path
    // A child that the canvas may draw: without the mark the browser keeps no picture of it.
    this.el.setAttribute('drawable', '')
    this.el.setAttribute('data-xr-dom-page', id)
    this.el.style.width = `${Math.round(cssWidth)}px`
    this.el.style.height = `${Math.round(cssHeight)}px`
    this.el.style.border = '0'
    this.el.style.display = 'block'
    // The page takes the pointer itself only while the room's ray finds it first (`hover`).
    this.el.style.pointerEvents = 'none'
    this.el.addEventListener('load', this.onLoad)
    // A release anywhere in the window outside the page reaches this document, not the page's (`endOutside`).
    window.addEventListener('pointerup', this.endOutside, true)
    window.addEventListener('pointercancel', this.endOutside, true)
    window.addEventListener('pointerout', this.onTopOut, true)
    window.addEventListener('blur', this.onTopBlur)
    this.texture = new ExternalTexture(null)
    this.texture.colorSpace = SRGBColorSpace
    this.texture.magFilter = LinearFilter
    this.texture.minFilter = LinearFilter
    this.texture.generateMipmaps = false
    // The element's first row is the page's top; a texture's first row is its bottom.
    this.texture.repeat.set(1, -1)
    this.texture.offset.set(0, 1)
    this.timer = setInterval(() => this.follow(), ADDRESS_EVERY_MS)
  }

  private tell(): void { for (const l of [...this.listeners]) l() }

  /** The page's own document: same origin, so its address and its links can be read. */
  private get doc(): Document | null { try { return this.el.contentDocument } catch { return null } }

  /** On every document the page loads: its links are guarded, and its input is heard. */
  private guardLinks(): void {
    const doc = this.doc
    if (!doc) return
    doc.addEventListener('click', this.onClick, true)
    // Before every handler of the page: on its window, in the capture phase.
    doc.defaultView?.addEventListener('click', this.noClick, true)
    for (const [type, fn] of Object.entries(this.heard)) doc.addEventListener(type, fn, { capture: true, passive: true })
    this.dialogs(doc)
    for (const w of this.frameWatch) w.disconnect()
    this.frameWatch = []
    this.coverFrames(doc)
    this.watch?.disconnect()
    const Watch = doc.defaultView?.MutationObserver
    if (!Watch) return
    this.watch = new Watch(records => {
      const now = performance.now()
      this.touched = true
      for (const r of records) for (const n of r.addedNodes) if (n.nodeType === 1) this.added.set(n as Element, now)
    })
    // Any change of the page's own document, and its scrolls and inputs, are its own changes (`onPaint`).
    this.watch.observe(doc, { childList: true, subtree: true, attributes: true, characterData: true })
    doc.addEventListener('load', this.onTouch, { capture: true, passive: true })
    for (const type of ['scroll', 'input', 'keydown', 'wheel']) doc.addEventListener(type, this.onInput, { capture: true, passive: true })
  }

  /**
   * The page's own dialogs, at each load. On a desk they stay the browser's. While a session runs nobody sees a
   * browser dialog, and it stops the XR tab until it is put away, so the page gets the outcome that the stream gives:
   * `confirm` answers false, `prompt` null, `alert` returns at once, and the panel says the dialog's text in its line;
   * and the page leaves with no "leave this page?".
   */
  /**
   * A frame inside the page has a window of its own, with the browser's dialogs. Each one that this tab can reach (of
   * its origin) gets the same dialogs as the page, at once and at each of its loads, and so do the frames inside it;
   * frames that come later are watched for. A frame of another origin cannot open such a dialog in Chrome at all.
   * shortcut: a frame's script that asks before its own load has ended still gets the browser's dialog; upgrade if a
   * page's frame does that.
   */
  private coverFrames(doc: Document): void {
    const hook = (frame: HTMLIFrameElement) => {
      const cover = () => {
        let inner: Document | null = null
        try { inner = frame.contentDocument } catch { inner = null }
        if (!inner) {
          if (frame.sandbox.contains('allow-modals') || frame.dataset.xrModals === '1') { this.farFrames.add(frame); if (this.inXr) this.modals(false) }
          else if (!frame.hasAttribute('sandbox')) this.openFrames.add(frame)
          return
        }
        this.openFrames.delete(frame)
        this.dialogs(inner)
        this.coverFrames(inner)
      }
      frame.addEventListener('load', cover)
      cover()
    }
    for (const frame of doc.querySelectorAll('iframe')) hook(frame)
    const Watch = doc.defaultView?.MutationObserver
    if (!Watch) return
    const watch = new Watch(records => {
      for (const r of records) for (const n of r.addedNodes) {
        if (n.nodeType !== 1) continue
        const el = n as Element
        if (el.tagName === 'IFRAME') hook(el as HTMLIFrameElement)
        for (const inner of el.querySelectorAll('iframe')) hook(inner)
      }
    })
    watch.observe(doc, { childList: true, subtree: true })
    this.frameWatch.push(watch)
  }

  /**
   * A frame that this tab cannot reach keeps the browser's dialogs. While a session runs, one whose sandbox allows them
   * has `allow-modals` taken out, and is loaded again once for the change to hold (what it showed is loaded anew); at
   * the session's end the word is put back, for its next load.
   */
  private modals(allowed: boolean): void {
    for (const frame of [...this.farFrames]) {
      if (!frame.isConnected) { this.farFrames.delete(frame); continue }
      if (!allowed && frame.sandbox.contains('allow-modals')) {
        frame.sandbox.remove('allow-modals')
        frame.dataset.xrModals = '1'
        if (frame.hasAttribute('srcdoc')) frame.srcdoc = frame.srcdoc
        else frame.src = frame.src
      } else if (allowed && frame.dataset.xrModals === '1') {
        frame.sandbox.add('allow-modals')
        delete frame.dataset.xrModals
      }
    }
  }

  private dialogs(doc: Document): void {
    const win = doc.defaultView as (Window & typeof globalThis) | null
    if (!win) return
    const own = { confirm: win.confirm.bind(win), alert: win.alert.bind(win), prompt: win.prompt.bind(win) }
    const told = (message: unknown) => { this.asked = { text: String(message ?? '').replace(/\s+/g, ' ').trim(), at: performance.now() } }
    win.confirm = (message?: string) => { if (!this.inXr) return own.confirm(message); told(message); return false }
    win.alert = (message?: unknown) => { if (!this.inXr) { own.alert(message); return } told(message) }
    win.prompt = (message?: string, value?: string) => { if (!this.inXr) return own.prompt(message, value); told(message); return null }
    // On the desk too: a window of the page is a panel, never a browser window beside the XR tab.
    const browserOpen = win.open?.bind(win)
    win.open = (url?: string | URL, target?: string, features?: string) => {
      if (!this.ownWindow(url) && browserOpen && this.outsideMay(false)) return browserOpen(url, target, features)
      this.window(url)
      return null
    }
    // "Leave this page?": the browser asks it for a frame that the person has pressed into (from the desk, before the
    // session). In a session the page's own `beforeunload` is not heard, so it leaves with no dialog. Ours runs first:
    // at the window, a capturing listener comes before the page's own.
    win.addEventListener('beforeunload', ev => { if (this.inXr) ev.stopImmediatePropagation() }, true)
  }

  /** When the element or a box round it last came into the page, or `null`. */
  private cameAt(el: Element): number | null {
    let most: number | null = null
    for (let n: Element | null = el; n; n = n.parentElement) { const t = this.added.get(n); if (t !== undefined && (most === null || t > most)) most = t }
    return most
  }

  /** What a session's pointer lights at a point of the page: kept with the time it came, or moved, there. */
  private aim(doc: Document, x: number, y: number, now: number): void {
    const a = aimAt(doc, x, y, this.cssWidth, this.cssHeight, this.sees)
    if (!a) { this.lit = null; return }
    const rect = rectOf(a.el)
    const lit = this.lit
    if (lit && lit.el === a.el) {
      lit.snapped = a.snapped
      if (!samePlace(rect, lit.rect)) { lit.rect = rect; lit.since = now; lit.movedAt = now }
      return
    }
    this.lit = { el: a.el, rect, snapped: a.snapped, since: now, movedAt: -Infinity }
  }

  /** The control that a pinch would press now, lit by the panel before the pinch. Only in a session. */
  target(): PageTarget | null {
    const lit = this.inXr ? this.lit : null
    if (!lit) return null
    const text = lit.el.matches('input, textarea, [contenteditable=""], [contenteditable="true"]')
    return { rect: [...lit.rect], kind: text ? 'text' : 'button', guarded: false }
  }

  /** What a test reads (`?probe=1`): what is lit, why the last pinch pressed nothing, and a dialog that a session answered. */
  get seen(): { lit: { label: string; rect: number[]; snapped: boolean } | null; refused: Refusal; stopped: boolean; dialog: string | null } {
    const lit = this.inXr ? this.lit : null
    const label = lit ? (lit.el.getAttribute('aria-label') || lit.el.textContent || lit.el.tagName).trim().slice(0, 60) : ''
    return { lit: lit ? { label, rect: lit.rect.map(Math.round), snapped: lit.snapped } : null, refused: this.refused, stopped: this.stopped, dialog: this.note() === 'dialog' ? this.asked?.text ?? null : null }
  }

  /** The panel's answer to whether the room shows a point of this page uncovered (`roomSees.ts`), or `null`. */
  seenBy(sees: ((x: number, y: number) => boolean) | null): void { this.sees = sees }

  /** In a session the browser paints this page no more: its panel hands it to the stream. */
  paintStopped(): boolean { return this.stopped }

  /**
   * To be streamed while a session runs: the paint stopped, or the page holds a frame of another site with no sandbox
   * (its window gets no dialogs of ours, and none can be taken from it), which could stop the XR tab by a dialog.
   */
  streamWanted(): 'paint' | 'frame' | null {
    if (this.stopped) return 'paint'
    if (!this.inXr) return null
    for (const frame of [...this.openFrames]) { if (frame.isConnected) return 'frame'; this.openFrames.delete(frame) }
    return null
  }

  listen(hear: ((input: PageInput) => void) | null): void { this.hear = hear }

  /** The panel's path follows the page; a page that stays out of XR (or the XR page itself) is left at once. */
  private follow(): void {
    let where: Location | null = null
    try { where = this.el.contentWindow?.location ?? null } catch { where = null }
    if (!where || where.href === 'about:blank') return
    if (staysOut(where.pathname)) { where.replace(this.at); return }
    const now = addressOf(where)
    if (now !== this.at) { this.at = now; this.tell() }
  }

  frame(): PageFrame { return { texture: this.texture as Texture, cssWidth: this.cssWidth, cssHeight: this.cssHeight } }
  subscribe(changed: () => void): () => void { this.listeners.add(changed); return () => { this.listeners.delete(changed) } }
  status(): PageStatus { return this.state }
  pagePath(): string { return this.at }
  pictureAt(): number | null { return this.firstAt }
  note(): PageNote {
    const now = performance.now()
    if (this.asked && now - this.asked.at < DIALOG_SAID_MS && this.asked.at >= this.windowRefusedAt) return 'dialog'
    return now - this.windowRefusedAt < DIALOG_SAID_MS ? 'refused' : ''
  }
  noteWords(): string {
    if (this.note() !== 'dialog') return ''
    const text = this.asked?.text ?? ''
    return i18nT('xr.page_dialog_said', { text: text.length > DIALOG_TEXT_MAX ? `${text.slice(0, DIALOG_TEXT_MAX - 1)}…` : text })
  }
  visible(on: boolean): void { this.shown = on }
  /** How far the page's middle is from the middle of his look, in degrees: the page he looks at is copied first. */
  gaze(deg: number): void { this.gazeDeg = deg }
  windows(open: ((path: string) => void) | null): void { this.opens = open }
  holdFor(ms: number): void { this.heldUntil = performance.now() + ms }

  resize(cssWidth: number, cssHeight: number): void {
    if (cssWidth === this.cssWidth && cssHeight === this.cssHeight) return
    this.cssWidth = cssWidth
    this.cssHeight = cssHeight
    this.el.style.width = `${Math.round(cssWidth)}px`
    this.el.style.height = `${Math.round(cssHeight)}px`
    this.tell()
  }

  /**
   * Whether the room's own ray finds this page's surface first under the mouse. Only then does the page take the
   * mouse itself (the browser's hit test knows only the iframes, not what the room draws in front of them): a Bot, a
   * handle, the close mark or another panel drawn over the page keeps its click.
   */
  hover(on: boolean): void { this.hovered = on }

  /** Where the iframe takes the pointer itself now: the press, the wheel and the keys are then the browser's own. */
  private takes(): boolean { return this.el.style.pointerEvents !== 'none' }

  /**
   * The room's press on this page, when the browser did not send it in itself (a finger, a session's ray, a thing
   * of the room that was in front a moment ago): handed in by script at the same point (`domAct.ts`).
   */
  pointer(event: PagePointer): void {
    const doc = this.doc
    if (!doc || this.takes()) { this.pressed = null; return }
    if (this.inXr) { this.xrPointer(doc, event); return }
    // What is handed in is the person's own input: the host hears it as it hears the browser's (`listen`).
    if (event.type === 'down') { this.pressed = { pointerId: event.pointerId, x: event.x, y: event.y, el: act(doc, event.x, event.y, 'down') }; this.hear?.('down'); return }
    if (event.type === 'cancel' || event.type === 'leave') { if (this.pressed) { act(doc, this.pressed.x, this.pressed.y, 'cancel', this.pressed.el); this.hear?.('up') } this.pressed = null; return }
    if (event.type === 'up') {
      const was = this.pressed
      this.pressed = null
      // The release and its click go to what took the down, wherever the pointer is now.
      if (was?.pointerId === event.pointerId) { act(doc, event.x, event.y, 'click', was.el); this.hear?.('up') }
    }
  }

  /**
   * A session's press: only the lit control, asked again at the down (`domAim.pressFate`), and at the place that was
   * lit: a snapped control is pressed at its middle. The target is fixed at the down; the release goes there too.
   */
  private xrPointer(doc: Document, event: PagePointer): void {
    const now = performance.now()
    if (event.type === 'move') { if (!this.pressed && now - this.aimedAt >= AIM_EVERY_MS) { this.aimedAt = now; this.aim(doc, event.x, event.y, now) } return }
    if (event.type === 'down') {
      const lit = this.lit
      const fresh = aimAt(doc, event.x, event.y, this.cssWidth, this.cssHeight, this.sees)
      this.refused = pressFate(lit, fresh && { el: fresh.el, rect: rectOf(fresh.el) }, now, lit ? this.cameAt(lit.el) : null)
      if (this.refused || !lit) { this.pressed = null; return }
      const [x, y] = lit.snapped ? [lit.rect[0] + lit.rect[2] / 2, lit.rect[1] + lit.rect[3] / 2] : [event.x, event.y]
      this.pressed = { pointerId: event.pointerId, x, y, el: act(doc, x, y, 'down') }
      this.hear?.('down')
      return
    }
    const was = this.pressed
    if (event.type === 'leave' && !was) { this.lit = null; return }
    if (!was || was.pointerId !== event.pointerId) return
    this.pressed = null
    // A pinch that travelled is a drag, and one held long is a long press: its release is a mouse's release with no
    // click (`pinchRoute.ending`). A long press comes as the pointer's cancel, so it is told from a hand that was lost
    // by the pinch's own end. A cancel with no such end stays a cancel.
    const ending = pinchRoute.ending
    const plain = ending === 'drag' || ending === 'long'
    const kind = plain ? 'up' : event.type === 'up' ? 'click' : 'cancel'
    act(doc, was.x, was.y, kind, was.el)
    this.hear?.('up')
  }

  scroll(event: PageScroll): void {
    const doc = this.doc
    if (!doc || this.takes() || (event.dx === 0 && event.dy === 0)) return
    scrollAt(doc, event.x, event.y, event.dx, event.dy)
    this.hear?.('wheel')
  }

  /** A mouse move inside the page, told to the room at the same point of the canvas, so that the room keeps hit-testing it. */
  private moved(ev: PointerEvent): void {
    const canvas = this.canvas
    if (!canvas || ev.pointerType !== 'mouse') return
    const e = this.m.elements
    const W = e[3] * ev.clientX + e[7] * ev.clientY + e[15]
    if (!(W > 0)) return
    const box = canvas.getBoundingClientRect()
    canvas.dispatchEvent(new PointerEvent('pointermove', {
      bubbles: true, clientX: box.left + (e[0] * ev.clientX + e[4] * ev.clientY + e[12]) / W, clientY: box.top + (e[1] * ev.clientX + e[5] * ev.clientY + e[13]) / W,
      pointerId: ev.pointerId, pointerType: 'mouse', isPrimary: ev.isPrimary, buttons: ev.buttons,
    }))
  }

  /** Before a frame is drawn: the page goes into the room's canvas once, and a new paint of it into the texture. */
  beforeDraw(renderer: WebGLRenderer): void {
    const canvas = renderer.domElement as DrawableCanvas
    if (this.canvas !== canvas) this.attach(canvas)
    this.watchPaint(canvas, renderer.xr.isPresenting)
    // A page out of view is not copied: a copy is the whole page, the room's dearest work. It keeps its mark, and
    // the panel tells it that it is in view before this call (`Panel.tsx`), so it is copied for the first frame
    // that shows it again.
    // One copy a room frame: the pick, at the first page of the frame, over every page that is drawn. A page joins at
    // each draw, so one that was taken out and drawn again (a remount) is in the pick.
    DomSource.all.add(this)
    const key = renderer.info?.render.frame ?? ++DomSource.frames
    this.drawnKey = key
    if (DomSource.pickedFor !== key) { DomSource.pickedFor = key; DomSource.winner = DomSource.pick(canvas, key) }
    if (!this.changed || !this.shown || DomSource.winner !== this) return
    // It holds the pick. If its copy draws nothing, it gives the pick up in this frame: the next page picks again.
    DomSource.winner = null
    DomSource.pickedFor = -1
    DomSource.last = this.id
    this.waited = 0
    this.changed = false
    const gl = renderer.getContext() as DrawableGl
    if (!gl.texElementSubImage2D || !canvas.getElementImageDefaultSize) { this.state = 'failed'; this.tell(); return }
    const t0 = performance.now()
    try {
      // The picture is made at the canvas's own scale; another scale is another size, and another texture.
      const d = canvas.getElementImageDefaultSize(this.el)
      const want: [number, number] = [Math.max(1, Math.ceil(d.width)), Math.max(1, Math.ceil(d.height))]
      const fresh = !this.glTexture || want[0] !== this.size[0] || want[1] !== this.size[1]
      if (fresh) this.allocate(renderer, gl, want)
      if (!this.glTexture) return
      renderer.state.bindTexture(gl.TEXTURE_2D, this.glTexture)
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false)
      gl.texElementSubImage2D(gl.TEXTURE_2D, 0, 0, 0, this.el)
      DomSource.pickedFor = key
      this.copiedAt = performance.now()
      this.touched = false
      this.input = false
      this.count += 1
      keep(this.uploads, performance.now() - t0)
      if (this.state !== 'live') { this.state = 'live'; this.firstAt = performance.now() }
      if (fresh || this.count === 1) this.tell()
    } catch {
      // The page has no picture yet: its next paint tries again.
    }
  }

  /** Which page the room copies in this frame (`pickCopy`); every other page that waits has waited one frame more. */
  private static pick(canvas: DrawableCanvas, key: number): DomSource | null {
    const now = performance.now()
    // A paint that the 250 ms rule dropped is marked once that time is over.
    for (const s of DomSource.all) if (s.dropped && now - s.copiedAt >= UNTOUCHED_MS) { s.dropped = false; s.changed = true }
    // A page that was not drawn in this frame or the one before holds no pick.
    const waiting = [...DomSource.all].filter(s => s.changed && s.shown && s.drawnKey >= key - 1 && (s.canvas === canvas || !s.canvas))
    const id = pickCopy(waiting.map(s => ({ id: s.id, waited: s.waited, pointer: s.hovered || !!s.pressed || !!s.lit, gazeDeg: s.gazeDeg, input: s.input })), DomSource.last)
    let chosen: DomSource | null = null
    for (const s of waiting) { if (s.id === id && !chosen) chosen = s; else s.waited += 1 }
    return chosen
  }

  /** In a session: ask for a paint when none came for a while, and say so when none comes after the asking. */
  private watchPaint(canvas: DrawableCanvas, presenting: boolean): void {
    const now = performance.now()
    if (presenting !== this.inXr) {
      this.inXr = presenting
      this.modals(!presenting)
      this.lit = null
      this.paintAt = now
      this.askedAt = now
    }
    if (!presenting || this.stopped || this.state !== 'live') return
    if (now - this.paintAt > PAINT.askMs && this.askedAt <= this.paintAt) { this.askedAt = now; canvas.requestPaint?.() }
    if (this.askedAt > this.paintAt && now - this.askedAt > PAINT.stoppedMs) { this.stopped = true; this.tell() }
  }

  private attach(canvas: DrawableCanvas): void {
    this.canvas?.removeEventListener('paint', this.onPaint)
    this.canvas = canvas
    canvas.setAttribute('content', 'drawable')
    canvas.addEventListener('paint', this.onPaint)
    canvas.appendChild(this.el)
    canvas.requestPaint?.()
  }

  private allocate(renderer: WebGLRenderer, gl: WebGL2RenderingContext, size: [number, number]): void {
    this.size = size
    this.glTexture ??= gl.createTexture()
    renderer.state.bindTexture(gl.TEXTURE_2D, this.glTexture)
    // sRGB storage, as three keeps a picture's texture: the shader reads linear light.
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.SRGB8_ALPHA8, size[0], size[1], 0, gl.RGBA, gl.UNSIGNED_BYTE, null)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    // shortcut: no anisotropy and no small copies, so a panel seen from far to the side is softer than a stream's;
    // upgrade when a real graphics card shows what they cost (H2C-1: mipmaps cost much in software).
    this.texture.sourceTexture = this.glTexture
  }

  /** Where the page is drawn on the canvas now, for the browser's own hit test: the full 4 x 4, each frame. */
  place(mesh: Object3D, camera: Camera, renderer: WebGLRenderer): void {
    const canvas = this.canvas
    if (!canvas?.updateElementGeometry || this.state !== 'live') return
    const cw = renderer.domElement.clientWidth
    const ch = renderer.domElement.clientHeight
    // The page's CSS pixels (origin top left, y down) to the mesh's own unit square (origin middle, y up).
    this.toUnit.set(1 / this.cssWidth, 0, 0, -0.5, 0, -1 / this.cssHeight, 0, 0.5, 0, 0, 1, 0, 0, 0, 0, 1)
    // Clip space to the canvas's CSS pixels, kept homogeneous: x' = (x + w) cw / 2, y' = (w - y) ch / 2.
    this.toCss.set(cw / 2, 0, 0, cw / 2, 0, -ch / 2, 0, ch / 2, 0, 0, 1, 0, 0, 0, 0, 1)
    this.m.copy(this.toCss).multiply(camera.projectionMatrix).multiply(camera.matrixWorldInverse).multiply(mesh.matrixWorld).multiply(this.toUnit)
    try { canvas.updateElementGeometry(this.el, { canvasTransform: DOMMatrix.fromFloat32Array(new Float32Array(this.m.elements)) }) } catch { /* not laid out yet: the next frame tells it */ }
    // The page takes the pointer itself only while the room's own ray finds its surface first under the mouse: the
    // browser's hit test knows only the iframes, in the order they were made, and not what the room draws in front
    // of them (a Bot, a handle, the close mark, a nearer panel). Elsewhere the room takes it, and hands a press on
    // this page in by script (`pointer`).
    // Who takes the pointer is decided at the down and kept until the up or the cancel: a press held in the page, or
    // one that the room holds on it, ends where it began.
    // A lit control that went, or moved, is lit no more there.
    const lit = this.lit
    if (lit && !lit.el.isConnected) this.lit = null
    else if (lit) { const rect = rectOf(lit.el); if (!samePlace(rect, lit.rect)) { lit.rect = rect; lit.since = performance.now(); lit.movedAt = lit.since } }
    if (this.own || this.pressed) return
    const events = this.hovered && performance.now() >= this.heldUntil && (!DomSource.holder || DomSource.holder === this) ? '' : 'none'
    if (this.el.style.pointerEvents !== events) this.el.style.pointerEvents = events
  }

  readout(): PageReadout {
    return { way: 'dom', count: this.count, picture: this.size, bytes: 0, visible: this.shown, landMs: [], decodeMs: [], readyWaitMs: [], writeMs: this.uploads, inputMs: [], gateway: null, roundMs: [] }
  }

  dispose(): void {
    DomSource.all.delete(this)
    if (DomSource.winner === this) DomSource.winner = null
    clearInterval(this.timer)
    this.watch?.disconnect()
    for (const w of this.frameWatch) w.disconnect()
    this.frameWatch = []
    this.farFrames.clear()
    this.openFrames.clear()
    this.canvas?.removeEventListener('paint', this.onPaint)
    this.el.removeEventListener('load', this.onLoad)
    window.removeEventListener('pointerup', this.endOutside, true)
    window.removeEventListener('pointercancel', this.endOutside, true)
    window.removeEventListener('pointerout', this.onTopOut, true)
    window.removeEventListener('blur', this.onTopBlur)
    this.letGo()
    this.doc?.removeEventListener('click', this.onClick, true)
    this.doc?.defaultView?.removeEventListener('click', this.noClick, true)
    for (const [type, fn] of Object.entries(this.heard)) this.doc?.removeEventListener(type, fn, { capture: true })
    this.hear = null
    this.el.remove()
    if (this.glTexture && this.canvas) this.canvas.getContext('webgl2')?.deleteTexture(this.glTexture)
    this.texture.dispose()
    this.state = 'closed'
    this.listeners.clear()
  }
}
