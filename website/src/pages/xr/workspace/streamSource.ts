/**
 * A page source that shows one of the dashboard's own pages LIVE.
 *
 * A page is not painted inside an immersive session, so the page is drawn somewhere else: in a tab of a browser on
 * the gateway's host. That tab's pictures come here over the dashboard's own socket (`hooks/xrPanelLink.ts`)
 * and the hand's pointer, scroll and text go back as real browser input, through two routes on this origin. The
 * gateway decides who may have a panel and what its tab may open; when it does not know the caller, or the feature
 * is off, there is no picture and no input and `status()` says so.
 *
 * What a picture costs is paid by the pixel on both ends, so this source asks for about as many pixels as the panel
 * covers on the display (`detail`), stops the pictures of a panel that is out of view (`visible`), and takes one
 * picture at a time: it tells the gateway that a picture is decoded, and the gateway makes the next one after that;
 * once the picture is in the texture it says "landed", which is what every rule that waits for his sight reads.
 *
 * Pictures are decoded as they are and written in `beforeDraw()`, before a frame's drawing starts. A picture of the same
 * size is written into the same texture: no new texture, and nothing for React to do.
 */
import { DataTexture, LinearFilter, LinearMipmapLinearFilter, SRGBColorSpace, Texture, type WebGLRenderer } from 'three'
import { api } from '../../../api/client'
import type { BrowserPanelEvent, BrowserPanelInputAnswer, BrowserPanelTarget } from '../../../api/client/browserLive'
import { livePath } from './streamPath'

/** How long the mark at a press stands at most (ms): the page's picture that answers the press takes it away sooner. */
export const PRESS_MARK_MS = 600
import { INPUT_GAP_MS, panelInputClosed, panelInputHeard, panelInputOpened, sendPanelInput, socketTakesInput } from './panelInputLink'
import { listenXrPanels, sendXrPanel, xrPanelLinkOpen, type XrPanelPicture } from '../../../hooks/xrPanelLink'
import { XR_GL } from '../palette'
import { detailStep } from './layout'
import { pressPlaces } from './asks'
import { pinchRoute } from './pinchRoute'
import type { AnswerWord, PageFocus, PageFrame, PageReadout, PageKey, PageNote, PagePointer, PageScroll, PageSource, PageStatus, PageTarget, RestFacts } from './source'
import type { PageLayer } from './pageLayer'
import { watchables, type Watchable, type WatchPage } from './streamWatch'
import { freeRoomFor } from './fullHook'
import { AHEAD, WHOLE_MS } from './warmRooms'

/** How long a refused pinch stays said. */
const NOTE_MS = 4000
/** A new size is sent this long after the last step of a corner drag: the newest size wins. */
const SIZE_SETTLE_MS = 120
/** What the tab starts with of this page's own stored preferences, so it paints the same theme and shows no first-run notes. */
const PREF_KEYS = 400
const PREF_BYTES = 200_000
const PREF_VALUE_CHARS = 16_000

/** The most text one message to the gateway carries. The gateway takes more (`MAX_TEXT_CHARS`); typing never needs it. */
const TEXT_CHARS = 500

/** What a test reads of the stream: what arrived, how big, and what each step cost. */
export interface StreamSeen {
  /** The gateway's name for the panel on its input routes: an address, never a credential. */
  handle: string
  /** Taken back from the warm set and waiting for its fresh picture (`awaitingFresh`, D49). */
  awaitingFresh: boolean
  /** A new page that waits for its first picture with content (`awaitingContent`, D50). */
  awaitingContent: boolean
  /** A live page with no picture since it came into view, which waits for the gateway's next one (`notYet`, D53 1). */
  awaitingPicture: boolean
  /** It takes no input now and is drawn dimmed (`notYet`, D52). */
  notYet: boolean
  /** What it cost to tell whether a picture is flat (ms), for the pictures before the first with content. */
  flatMs: number[]
  /** The last pointer words (not moves): when, what, the frame's word at a down, the wait then, whether a press was held, the pinch's ending. */
  pressLog: { ms: number; type: string; seen: boolean | null; notYet: boolean; had: boolean; ending: string }[]
  /** The same, split: drawing the picture's middle small, and reading the small copy back (ms). */
  flatDrawMs: number[]
  flatReadMs: number[]
  /** When the first picture of the page as it is now was shown, or `null` (`pictureAt`). */
  since: number | null
  status: PageStatus
  note: PageNote
  why: string
  /** The path the tab shows now. */
  path: string
  css: [number, number]
  picture: [number, number]
  detail: number
  visible: boolean
  pictures: number
  stills: number
  /** Pictures of another shape than the page's that the gateway dropped, as its last picture said. */
  misshapen: number
  /** Pictures that arrived here with another shape than their page's. The gateway sends none: this stays 0. */
  offShape: number
  /** Picture pixels and bytes that arrived, in all. */
  pixels: number
  bytes: number
  /** How often the texture went between "moving" (no small copies) and "still" (with them). */
  switches: number
  decodeMs: number[]
  writeMs: number[]
  /** From a click or a scroll to the first picture after it, written and ready to draw. */
  inputMs: number[]
  /** How long each return to view took, from the moment the panel was in view again to its next picture on the texture. */
  backMs: number[]
  /** From a picture's arrival on the socket to its landing on the texture; the wait before its decode began; the wait from its decode to its upload. */
  landMs: number[]
  /** The gateway's `round_ms` of each picture that said one. */
  roundMs: number[]
  queueMs: number[]
  readyWaitMs: number[]
  /** Pictures that arrived and gave way to a newer one before their decode. */
  replaced: number
  /** What the gateway said with the last picture: the round trip of the one before, its pace, whether the page is held (`panel_stream.py`, `_frame_header`). */
  gateway: { roundMs: number | null; pace: number | null; held: boolean | null } | null
  /** The page's pictures go to a composition layer now, not into its texture. */
  layered: boolean
  target: PageTarget | null
}

/** `again`: the picture was shown before and is decoded once more for another way up; it is not counted or answered twice. */
interface Waiting { header: XrPanelPicture; bytes: Uint8Array; again?: boolean }
/** `flipped`: decoded for a layer, which wants the picture's last row first. */
/** `content`: a picture that shows the page (not one flat colour), or a late still (D50); `null`: not tested yet (no panel drew the page then). */
interface Ready { header: XrPanelPicture; bitmap: ImageBitmap; still: boolean; flipped: boolean; again: boolean; content: boolean | null }

const keep = (list: number[], value: number) => { list.push(Math.round(value * 10) / 10); if (list.length > 400) list.shift() }
const isPng = (bytes: Uint8Array) => bytes[0] === 0x89 && bytes[1] === 0x50

/** The XR page's own keys in the store (the kept room): they are not a panel's tab's to have. */
/** Whether a picture of `w` by `h` has the shape of a page of `css`: `w / h` is the page's, to within a pixel. */
export function hasShape(w: number, h: number, css: readonly [number, number]): boolean {
  if (!(w > 0 && h > 0 && css[0] > 0 && css[1] > 0)) return false
  return Math.abs(w * css[1] - h * css[0]) <= Math.max(css[0], css[1])
}

const OWN_KEYS = 'kirocrew:xr-'

/** This page's own stored preferences, bounded, but for the XR page's own keys. Read once, when a panel opens. */
function storedPrefs(): Record<string, string> {
  const prefs: Record<string, string> = {}
  let budget = PREF_BYTES
  try {
    for (let i = 0; i < localStorage.length && i < PREF_KEYS; i += 1) {
      const key = localStorage.key(i)
      const value = key === null ? null : localStorage.getItem(key)
      if (key === null || value === null || key.startsWith(OWN_KEYS) || value.length > PREF_VALUE_CHARS) continue
      budget -= key.length + value.length
      if (budget < 0) break
      prefs[key] = value
    }
  } catch { /* storage is closed to this page: the tab starts with none */ }
  return prefs
}

/** A page taken back from the warm set takes no input until its fresh picture landed, and this long at most (ms, D49). */
export const FRESH_WAIT_MS = 3000
/** A new page takes no input until a picture with content (or a still) landed, and this long after live at most (ms, D50). */
export const CONTENT_WAIT_MS = 3500
/** A live page with no picture of this open asks the gateway for it once more this long after live (ms, D55 1). */
export const NO_PICTURE_ASK_MS = 15_000
/** A live page with still no picture of this open is failed this long after live (ms, D55 1). */
export const NO_PICTURE_FAIL_MS = 30_000

/**
 * How a picture is told flat: its middle half each way (where he looks and presses; a page's side bar and top bar
 * come before its body) drawn this small by the GPU, and flat when at most `most` of its samples differ from the
 * first by more than `step`.
 */
const FLAT = { w: 48, h: 30, step: 24, middle: 0.5, most: 7 } as const
let flatProbe: OffscreenCanvasRenderingContext2D | null | undefined
/**
 * Whether a decoded picture is one flat colour in its middle: the page's first pictures often are (its ground
 * before it paints). Its middle is drawn into a 48×30 canvas and those 1,440 samples are read back; only a page's pictures before its first
 * with content are asked. Where the browser has no such canvas, every picture counts as content.
 */
export const flatness = {
  /** What the last test cost: drawing the picture's middle small, and reading it back (ms), for the probe. */
  split: [0, 0] as [number, number],
  of(bitmap: ImageBitmap): boolean {
    try {
      // A canvas that is read back is kept in memory, not on the GPU (`willReadFrequently`): a GPU canvas's first
      // read stalls the page's thread until the GPU is done, 554 to 961 ms on the test host (`seen.flatMs`).
      if (flatProbe === undefined) flatProbe = typeof OffscreenCanvas === 'undefined' ? null : new OffscreenCanvas(FLAT.w, FLAT.h).getContext('2d', { willReadFrequently: true })
      if (!flatProbe) return false
      const mw = bitmap.width * FLAT.middle
      const mh = bitmap.height * FLAT.middle
      const t0 = performance.now()
      flatProbe.drawImage(bitmap, (bitmap.width - mw) / 2, (bitmap.height - mh) / 2, mw, mh, 0, 0, FLAT.w, FLAT.h)
      const t1 = performance.now()
      const d = flatProbe.getImageData(0, 0, FLAT.w, FLAT.h).data
      flatness.split = [t1 - t0, performance.now() - t1]
      // Content: more than half a percent of the samples differ (a lone icon or a cursor is not yet the page).
      let off = 0
      for (let i = 4; i < d.length; i += 4) if (Math.abs(d[i] - d[0]) + Math.abs(d[i + 1] - d[1]) + Math.abs(d[i + 2] - d[2]) > FLAT.step && (off += 1) > FLAT.most) return false
      return true
    } catch {
      return false
    }
  },
}

export class StreamSource implements PageSource, Watchable {
  private readonly listeners = new Set<() => void>()
  /** Shown until the first picture: the room's own dark, which draws nothing on glasses that add light. */
  private readonly blank: DataTexture
  private texture: Texture
  /** The texture before this one. A panel may still draw it for a frame, so it is freed one change later. */
  private before: Texture | null = null
  /** The texture a panel keeps as it is (`keepPicture`): no picture is written into it, and it is freed at `letGoPicture`. */
  private kept: Texture | null = null
  private css: [number, number]
  private want: [number, number]
  private sizeTimer: ReturnType<typeof setTimeout> | null = null
  private handle = ''
  private state: PageStatus = 'loading'
  private why = ''
  private said: PageNote = ''
  private saidUntil = 0
  private waiting: Waiting | null = null
  private decoding = false
  private ready: Ready | null = null
  /** The layer this page's pictures go to, when the session gave it one (`pageLayer.ts`); else they go into the texture. */
  private layer: PageLayer | null = null
  /** The newest picture as it arrived, kept so that it can be decoded the other way up when the page changes between a layer and its mesh. */
  private last: Waiting | null = null
  private step = 2
  private shown = true
  /** How far the page's middle is from the middle of his look (degrees), as the panel told it this frame. */
  private gazeDeg = 180
  private gazeFar: number | undefined
  /** The gateway was told that he looks at this page (`streamWatch.ts`). */
  private watching = false
  private aim: PageTarget | null = null
  /** The label of the control in `aim`, as the page said it (at most 80 characters); '' for none. */
  private aimLabel = ''
  /** Who opens a window that the page opened (`windows`). */
  private opens: ((path: string) => void) | null = null
  /** The place of the move whose answer set `aim`: a press there names what the page drew there. */
  private aimPlace: { x: number; y: number } | null = null
  private press: { x: number; y: number } | null = null
  /** Who keeps the page's word on the controls that answer an ask, when this is an ask's panel (`answersBy`). */
  private answers: AnswerWord | null = null
  /** A pointer is on the page. */
  private on = false
  private moving = false
  /** The press that was sent and that no picture of the page has answered yet: where, and when (`pressMark`). */
  private pressed: { x: number; y: number; at: number } | null = null
  /** How many moves were sent: an answer lights the aim only when it is the newest move's. */
  private moveCount = 0
  private moveNext: { x: number; y: number } | null = null
  /**
   * The last place a move was sent for, rounded as it was sent. The framework makes a move at every frame for a
   * pointer that rests (47 to 62 a second), and each was a request, a question to the page and a mouse event in
   * its tab: a move to the same place is not sent again.
   */
  private moveAt: { x: number; y: number } | null = null
  private wheeling = false
  private wheelNext: PageScroll | null = null
  private inputAt = 0
  /** Where the page's focus is, as the gateway last said. */
  private where: PageFocus | null = null
  private asking = false
  /** Text and keys that wait: one is on its way at a time, so they reach the page in the order they were typed. */
  private readonly typing: ({ text: string } | { key: string })[] = []
  private typingOn = false
  /** When the panel last came back into view, until its next picture is on the texture. */
  private backAt = 0
  /**
   * When the page's first picture was where the eye sees it: the first since the page was opened, and the first
   * since the panel came back into view. `null` until then: what the panel shows is no picture of the page as it
   * is now, and a press on it would not do what the picture shows.
   */
  private pictureSince: number | null = null
  /** The longest time the page has shown its picture without a break, up to when it last left the view (`wasWhole`). */
  private shownFor = 0
  /** The gateway was told that nobody sees the page before it had filled: its pictures stopped, so it never filled. */
  private hideCut = false
  /**
   * A picture of THIS open has landed (not one decoded again). Until one has, no clock ends a wait: the page shows
   * nothing of this open, so a press would not do what the picture shows (D55 1).
   */
  private landedThisOpen = false
  /**
   * His time in view (ms) with no picture of this open, added up over every stretch in view, and when the stretch
   * that runs now began (`null` out of view). The ask again and the fail read the sum, so looking in turns does not
   * hold the page for ever (D55 1, D56 1).
   */
  private noPictureMs = 0
  private noPictureSince: number | null = null
  /** It asked the gateway again once for a page with no picture (D56 1). */
  private askedAgain = false
  /** When the first picture of this open landed: the content clock counts from the later of it and live (D56 2). */
  private firstLandAt: number | null = null
  /** One more at each open: a picture whose decode began before the open is given up (REVIEW-19 small 3). */
  private openSeq = 0
  /** The `close` words sent whose "closed" has not come back yet: such a "closed" ends no open made since (S2). */
  private closesOwed = 0
  /** The cast of this open, as its first picture named it: a picture of another cast is of the open before (S3). */
  private streamOfOpen: string | null = null
  /**
   * When the page was taken back from the warm set (D49), until its first fresh picture landed: the first one that
   * ARRIVED after it was told it is seen again. Until then it shows its old picture, which may no longer be the page.
   */
  private freshSince: number | null = null
  /** When the page's first picture with content landed (or its wait ran out): from then on it takes input (D50). */
  private contentAt: number | null = null
  /** The content wait counts from live, or from when a page that never landed content was shown again (M6). */
  private contentFrom: number | null = null
  /** `contentAt` was set by the wait running out, not by a picture with content. */
  private contentByCap = false
  /** When a warm page was told early that he may see it (`peek`), or `null` (D52). */
  private earlyAt: number | null = null
  /** When a panel last drew the page (`beforeDraw`): a page with no panel is not tested for content. */
  private drawnAt = -Infinity
  /**
   * The panel said that nobody sees the page before it had filled: the gateway is told only once it has (a page out
   * of view gets no picture, so it would never fill and never be kept warm). Its timer is for a page that sends no
   * late still: it counts 15 s after it was live.
   */
  private hideHeld = false
  private hideTimer: ReturnType<typeof setTimeout> | null = null
  /** When the page last moved to another address by itself (a link inside it): what it showed before counts no more. */
  private movedAt = -Infinity
  /** When the page turned live, when its newest picture reached the tab, and when its newest still did (`restFacts`). */
  private liveAt: number | null = null
  private cameAt: number | null = null
  private stillAt: number | null = null
  private gone = false
  /** The gateway has shown this page once: a refusal after that is not a refusal to open it. */
  private wasLive = false
  private readonly unlisten: () => void
  /** The path the tab shows now. It is the one asked for until the gateway says that the page has moved on. */
  private at: string
  private readonly stats = { pictures: 0, landed: 0, landedBytes: 0, stills: 0, misshapen: 0, offShape: 0, pixels: 0, bytes: 0, picture: [0, 0] as [number, number], switches: 0, decodeMs: [] as number[], writeMs: [] as number[], inputMs: [] as number[], backMs: [] as number[], landMs: [] as number[], readyWaitMs: [] as number[], queueMs: [] as number[], flatMs: [] as number[], flatDrawMs: [] as number[], flatReadMs: [] as number[], roundMs: [] as number[], replaced: 0 }
  /** When each of the last pictures arrived, by its number; and when the last decode ended. For the readout (`Readout.tsx`). */
  private readonly arrivedAt = new Map<number, number>()
  /** The bytes of each picture that arrived, by its number: the readout's kB a picture is of the pictures that landed. */
  private readonly arrivedBytes = new Map<number, number>()
  private decodedAt = 0
  private gateway: StreamSeen['gateway'] = null

  /**
   * `path` is the dashboard page to show, such as `/chat/<session>`. `copiesWhenMoving` writes the texture's small
   * copies for every picture, not only for one that stays: a test sets it to measure what leaving them out saves.
   */
  constructor(readonly id: string, path: string, cssWidth: number, cssHeight: number, private readonly copiesWhenMoving = false) {
    this.css = this.want = [Math.round(cssWidth), Math.round(cssHeight)]
    this.at = path
    const c = XR_GL.glass
    this.blank = new DataTexture(new Uint8Array([(c >> 16) & 255, (c >> 8) & 255, c & 255, 255]), 1, 1)
    this.blank.colorSpace = SRGBColorSpace
    this.blank.needsUpdate = true
    this.texture = this.blank
    watchables.add(this)
    this.unlisten = listenXrPanels({
      // A socket open anew holds nothing of the one before: no answer to a `close` sent on that one will come (Q3).
      open: () => { panelInputOpened(); this.closesOwed = 0; this.open() },
      close: () => { panelInputClosed(); this.handle = ''; this.set('loading', '') },
      message: data => { if (!panelInputHeard(data)) this.told(data) },
      picture: (header, bytes) => this.arrived(header, bytes),
    })
    if (xrPanelLinkOpen()) this.open()
  }

  /** What the panel handed over and what came back, for the test probe. */
  get seen(): StreamSeen {
    const s = this.stats
    return {
      handle: this.handle,
      awaitingFresh: this.waitsForFresh(performance.now()),
      awaitingContent: this.waitsForContent(performance.now()),
      awaitingPicture: this.waitsForPicture(performance.now()),
      flatMs: [...this.stats.flatMs],
      pressLog: this.pressLog.map(p => ({ ...p })),
      flatDrawMs: [...this.stats.flatDrawMs],
      flatReadMs: [...this.stats.flatReadMs],
      notYet: this.waitsForContent(performance.now()) || this.waitsForFresh(performance.now()) || this.waitsForPicture(performance.now()),
      status: this.state, note: this.note(), why: this.why, path: this.at, css: [...this.css], picture: [...s.picture], detail: this.step, visible: this.shown,
      pictures: s.pictures, since: this.pictureSince, stills: s.stills, misshapen: s.misshapen, offShape: s.offShape, pixels: s.pixels, bytes: s.bytes, switches: s.switches, decodeMs: [...s.decodeMs], writeMs: [...s.writeMs], inputMs: [...s.inputMs], backMs: [...s.backMs], landMs: [...s.landMs], roundMs: [...s.roundMs], readyWaitMs: [...s.readyWaitMs], queueMs: [...s.queueMs], replaced: s.replaced, gateway: this.gateway, target: this.aim, layered: this.layer !== null,
    }
  }

  frame(): PageFrame { return { texture: this.texture, cssWidth: this.css[0], cssHeight: this.css[1] } }

  subscribe(changed: () => void): () => void {
    this.listeners.add(changed)
    return () => { this.listeners.delete(changed) }
  }

  status(): PageStatus { return this.state }
  target(): PageTarget | null { return this.aim }

  /**
   * The mark at a press. It stands from the moment the press is sent. It goes when the first picture lands that the
   * gateway can have made after the press reached it (a round after the press, by the gateway's own `round_ms`), when
   * the gateway answers that the press was not sent (a guarded control, a picker, a control that moved), and after
   * `PRESS_MARK_MS` whatever came. A press on what the aim already showed as guarded gets none.
   */
  pressMark(): { x: number; y: number } | null {
    const p = this.pressed
    if (!p) return null
    if (performance.now() - p.at >= PRESS_MARK_MS) { this.pressed = null; return null }
    return p
  }
  note(): PageNote {
    if (this.state === 'signed-out') return 'signed-out'
    // A host that lets one process take hold of another: the gateway does not start its browser there, and says which of the two it is.
    if (this.state === 'failed') return this.why === 'off' ? 'off' : this.why === 'too_many' ? 'full' : this.why === 'other_address' ? 'elsewhere' : this.why.startsWith('host_attach_') ? 'host' : this.why === 'no_picture' ? 'no_picture' : 'failed'
    if (this.waitsForFirstPicture() && this.noPictureNow(performance.now()) >= CONTENT_WAIT_MS) return 'waiting'
    return performance.now() < this.saidUntil ? this.said : ''
  }

  /* ---------------- the gateway ---------------- */

  private open(): void {
    if (this.gone) return
    this.handle = ''
    this.pictureSince = null
    this.shownFor = 0
    this.liveAt = this.cameAt = this.stillAt = null
    this.hideCut = false
    this.landedThisOpen = false
    this.noPictureMs = 0
    this.noPictureSince = null
    this.askedAgain = false
    this.firstLandAt = null
    this.streamOfOpen = null
    // A picture of the open before, waiting to be decoded or to land, is no picture of this open (small 3).
    this.openSeq += 1
    this.ready?.bitmap.close()
    this.ready = null
    this.waiting = null
    this.last = null
    this.contentAt = null
    this.contentFrom = null
    this.contentByCap = false
    this.set('loading', '')
    sendXrPanel({ op: 'open', id: this.id, path: this.at, w: this.want[0], h: this.want[1], detail: this.step, prefs: storedPrefs() })
    // Out of his view: the gateway is told at once, and a hide that was held back is told with it, so the next
    // `visible(true)` is sent (REVIEW-17 M3).
    if (!this.shown) this.hideNow()
    // Opened again (a new socket): the gateway is told again which page he looks at.
    if (this.watching) sendXrPanel({ op: 'watch', id: this.id, on: true })
  }

  /** Ask the gateway for the page anew: one it had no room for, or one failed for want of a picture (D56 1). */
  askAgain(): boolean {
    const note = this.note()
    if (this.gone || (note !== 'full' && note !== 'no_picture') || !xrPanelLinkOpen()) return false
    this.open()
    return true
  }

  private told(data: Record<string, unknown>): void {
    if (data.id !== this.id || this.gone) return
    // The gateway knows the page from its "opened" on: a hide sent with the open came before it and was dropped
    // (`panel_hub.set_visible` finds no panel yet), so a page nobody sees is told again now.
    if (data.op === 'opened' && typeof data.handle === 'string') { this.handle = data.handle; if (!this.shown && !this.hideHeld) sendXrPanel({ op: 'visible', id: this.id, visible: false }) }
    // The gateway's panels were switched to the other way, and every tab was closed for it: the page opens again in the new way.
    else if (data.op === 'status' && data.status === 'closed' && data.why === 'way') this.open()
    // One too many for the gateway's twelve, which other sockets share: a warm room of this tab may make room for it
    // (`fullHook.ts`); then it is asked for again at once, and says "full" only when nothing was freed.
    else if (data.op === 'status' && data.status === 'failed' && data.why === 'too_many' && freeRoomFor(this)) this.open()
    // The answer to its own `close` after it failed for want of a picture: it stays failed, and says so (D56 1).
    // The answer to a `close` that it sent itself (the fail at 30 s): it ends nothing, also when a press has opened the
    // page anew before the answer came (D56 1, REVIEW-19 S2).
    else if (data.op === 'status' && data.status === 'closed' && data.why !== 'way' && this.closesOwed > 0) this.closesOwed -= 1
    else if (data.op === 'status') this.set(data.status as PageStatus, typeof data.why === 'string' ? data.why : '')
    // A refusal says why: a browser dialog that was answered with a no has a line of its own.
    // A link inside the page led to another page on the list: the panel is at that path from here on.
    // The hub tells a path with no query: one that is the path of the address held already keeps that query (the
    // Bots page at `?member=` of the Bot it was opened for).
    else if (data.op === 'path') { const at = livePath(typeof data.path === 'string' ? data.path : null); if (at && at !== this.at && at !== this.at.split('?')[0]) { this.at = at; this.movedAt = performance.now(); this.shownFor = 0; this.changed() } }
    // A window that the page opened, which the gateway lets a panel show: it opens on a panel of its own.
    else if (data.op === 'window') { const path = livePath(typeof data.path === 'string' ? data.path : null); if (path) this.opens?.(path); else this.say('refused') }
    else if (data.op === 'guarded' || data.op === 'refused') this.say(data.op === 'refused' && data.why === 'dialog' ? 'dialog' : data.op)
  }

  private set(status: PageStatus, why: string): void {
    if (status === this.state && why === this.why) return
    const wasLive = this.state === 'live'
    if (status === 'live' && !wasLive) this.liveAt = performance.now()
    if (status === 'live' && !this.landedThisOpen && this.shown) this.noPictureSince ??= performance.now()
    this.state = status
    this.why = why
    if (status === 'live') this.wasLive = true
    if (status !== 'live') { this.aim = null; this.handle = status === 'loading' ? this.handle : '' }
    // A hide held while it loaded starts its timer when it goes live, not only at a landing (REVIEW-17 small 3).
    if (status === 'live' && !wasLive) this.hideIfFilled()
    this.changed()
  }

  private say(note: PageNote): void {
    this.said = note
    this.saidUntil = performance.now() + NOTE_MS
  }

  private changed(): void { for (const l of [...this.listeners]) l() }

  pagePath(): string { return this.at }

  /** When the first picture of the page as it is now came (see `pictureSince`), or `null` while there is none. */
  pictureAt(): number | null { return this.pictureSince }

  restFacts(): RestFacts {
    const now = performance.now()
    const live = this.state === 'live' && this.liveAt !== null
    return { liveForMs: live ? now - (this.liveAt as number) : null, lastPictureMs: live && this.cameAt !== null && this.ready !== null ? now - this.cameAt : null, stillAfterLiveMs: live && this.stillAt !== null ? this.stillAt - (this.liveAt as number) : null }
  }

  countWhole(): void { this.shownFor = Infinity }

  takenBack(): void {
    // A page that never landed a picture with content (put away from the start) waits for it anew from now (M6).
    if (this.contentAt === null || this.contentByCap) { this.contentAt = null; this.contentByCap = false; this.contentFrom = performance.now() }
    // A picture that arrived after the early word (his dot rested on the room's card, `peek`) is fresh too (D52).
    this.freshSince = this.earlyAt ?? performance.now()
    this.earlyAt = null
    this.visible(true)
  }

  /** A warm page: his dot rests on its room's card, so it is told he may see it before the press; or no longer (D52). */
  peek(on: boolean): void {
    if (this.gone) return
    if (on) { this.earlyAt ??= performance.now(); this.visible(true); return }
    this.earlyAt = null
    this.visible(false)
  }

  /** A new page whose first picture with content has not landed: it takes no input, and is drawn dimmed (D50). */
  awaitingContent(): boolean {
    const now = performance.now()
    if (this.waitsForContent(now)) return true
    if (this.contentAt === null && this.state === 'live') { this.contentAt = now; this.contentByCap = true }
    return false
  }

  /** Whether the page still waits for its content, with no word written (the probe reads it too). */
  private waitsForContent(now: number): boolean {
    if (this.contentAt !== null) return false
    if (this.state !== 'loading' && this.state !== 'live') return false
    // No clock opens a page that has shown nothing of this open (D55 1).
    if (!this.landedThisOpen) return true
    // From the later of live (or the take) and the first picture of this open that landed (D56 2): a late first
    // picture that is flat is the page's first paint, and gets its 3.5 s too.
    const base = this.contentFrom ?? this.liveAt
    const from = base === null ? null : Math.max(base, this.firstLandAt ?? base)
    return !(from !== null && now - from >= CONTENT_WAIT_MS)
  }

  /**
   * The page takes no input yet, and is drawn dimmed. The press gate holds by the same word (`useAskPanels`,
   * `pressFate`), so the dim shows exactly while a press is held (D52). Each of its three waits ends by itself, with
   * no change of the page (D53 1):
   * - content: a new page before its first picture with content; ends at that picture, or `CONTENT_WAIT_MS` after
   *   live (or after it was taken back), or when it fails or closes.
   * - fresh: a warm page taken back before a picture that arrived after it was told he sees it; ends at that
   *   picture, or `FRESH_WAIT_MS` after the take.
   * - picture: a live page with no picture since it came into view; ends at the gateway's next picture (a
   *   `visible: true` or an open always brings one), or `FRESH_WAIT_MS` after it came into view or went live. Over a
   *   held hide the gateway never stopped, so the picture is not old and this wait does not begin (`visible`).
   */
  notYet(): boolean { this.noPictureTick(performance.now()); return this.awaitingContent() || this.awaitingFresh() || this.waitsForPicture(performance.now()) }

  /** A live page that has landed no picture of this open: the one wait that no clock ends (D55 1). */
  private waitsForFirstPicture(): boolean { return this.state === 'live' && !this.landedThisOpen }

  /** His time in view so far with no picture of this open (ms), the stretch that runs now included. */
  private noPictureNow(now: number): number { return this.noPictureMs + (this.noPictureSince === null ? 0 : now - this.noPictureSince) }

  /** He looks away: the stretch in view that ran is added to the sum. */
  private pauseNoPicture(now: number): void {
    if (this.noPictureSince !== null) this.noPictureMs += now - this.noPictureSince
    this.noPictureSince = null
  }

  /**
   * The end of the wait for a first picture that needs no change of the page (D53 1, D56 1), in words the gateway
   * answers. After 15 s in view with none, the gateway is told "nobody sees it", then "he sees it": it starts the
   * cast anew and sends a first picture even when it is the same as the last (`panel_stream.set_visible`), and the
   * page keeps its place among the twelve. An `open` would not do: the gateway drops one for a panel it holds. After
   * 30 s in view the page is failed, says so on its glass, and gives its panel back (`close`), so it holds none of the
   * twelve; a press on that glass asks for it anew (`askAgain`). Read where the panel asks each frame whether the page
   * takes a press (`notYet`). Out of his view a page gets no picture: only his time in view counts, added up.
   */
  private noPictureTick(now: number): void {
    if (this.state !== 'live' || this.landedThisOpen || this.gone) return
    if (!this.shown) { this.pauseNoPicture(now); return }
    this.noPictureSince ??= now
    const waited = this.noPictureNow(now)
    if (waited >= NO_PICTURE_FAIL_MS) {
      this.noPictureMs = 0
      this.noPictureSince = null
      this.askedAgain = false
      this.set('failed', 'no_picture')
      this.closesOwed += 1
      sendXrPanel({ op: 'close', id: this.id })
      return
    }
    if (waited >= NO_PICTURE_ASK_MS && !this.askedAgain && xrPanelLinkOpen()) {
      this.askedAgain = true
      sendXrPanel({ op: 'visible', id: this.id, visible: false })
      this.sayShown()
    }
  }

  awaitingFresh(): boolean {
    if (this.freshSince === null) return false
    if (this.waitsForFresh(performance.now())) return true
    this.freshSince = null
    return false
  }

  private waitsForFresh(now: number): boolean { return this.freshSince !== null && now - this.freshSince < FRESH_WAIT_MS }

  private waitsForPicture(now: number): boolean {
    if (this.state !== 'live' || this.pictureSince !== null) return false
    if (!this.landedThisOpen) return true
    return now - Math.max(this.backAt, this.liveAt ?? 0) < FRESH_WAIT_MS
  }

  filledOutOfView(): boolean {
    if (this.state !== 'live' || this.liveAt === null || this.hideCut || this.stats.landed === 0) return false
    // A link inside the page moved it to another address: it fills anew from there, as `wasWhole` starts anew.
    const from = Math.max(this.liveAt, this.movedAt)
    return (this.stillAt !== null && this.stillAt - from >= AHEAD.afterLiveMs) || performance.now() - from >= AHEAD.lateMs
  }

  /** The gateway is told now that nobody sees the page; a held hide and its timer end with it. */
  private hideNow(): void {
    this.hideHeld = false
    if (this.hideTimer) { clearTimeout(this.hideTimer); this.hideTimer = null }
    if (!this.filledOutOfView() && !this.wasWhole(WHOLE_MS)) this.hideCut = true
    sendXrPanel({ op: 'visible', id: this.id, visible: false })
  }

  /** A hide that was held back goes to the gateway once the page has filled (or is over: closed, failed, seen). */
  private hideIfFilled(): void {
    if (!this.hideHeld) return
    if (this.shown || this.gone) { this.hideHeld = false; return }
    if (this.state === 'live' && !this.filledOutOfView()) {
      // Past its late time with no picture ever landed it cannot fill (that needs a landing): the hide goes now,
      // rather than its timer firing every 50 ms for ever (REVIEW-17 small 2).
      const late = this.liveAt !== null && performance.now() >= Math.max(this.liveAt, this.movedAt) + AHEAD.lateMs
      if (!late) {
        if (!this.hideTimer && this.liveAt !== null) this.hideTimer = setTimeout(() => { this.hideTimer = null; this.hideIfFilled() }, Math.max(0, Math.max(this.liveAt, this.movedAt) + AHEAD.lateMs - performance.now()) + 50)
        return
      }
    }
    if (this.state === 'loading') return
    this.hideNow()
  }

  wasWhole(ms: number): boolean {
    return this.state === 'live' && Math.max(this.shownFor, this.shownNowMs()) >= ms
  }

  /** How long the page has shown its picture of the address it is at now, without a break (ms). */
  private shownNowMs(): number {
    return this.shown && this.pictureSince !== null ? performance.now() - Math.max(this.pictureSince, this.movedAt) : 0
  }

  refusedOpen(): boolean { return this.state === 'failed' && this.why === 'address' && !this.wasLive }

  /** One event of the hand, sent to the page as real input. Never throws: a refusal is the gateway's to say. */
  private async send(event: BrowserPanelEvent): Promise<BrowserPanelInputAnswer | null> {
    if (!this.handle || this.state !== 'live') return null
    // A wheel and a move go on the socket when the gateway takes them there: they wait behind no request.
    const onSocket = sendPanelInput(this.handle, event)
    if (onSocket) return onSocket
    try {
      return await api.browserPanelInput(this.handle, event)
    } catch {
      return null
    }
  }

  /* ---------------- pictures ---------------- */

  private arrived(header: XrPanelPicture, bytes: Uint8Array): void {
    if (header.panel !== this.id || this.gone) return
    // A picture of the cast before this open (it came on the socket after the open) is dropped (S3): one before this
    // open's "opened", and one of another cast than the first picture after it named. A gateway that names no cast is
    // taken at its word.
    if (typeof header.stream === 'string') {
      if (!this.handle) return
      if (this.streamOfOpen === null) this.streamOfOpen = header.stream
      else if (header.stream !== this.streamOfOpen) return
    }
    this.stats.pictures += 1
    if (typeof header.misshapen === 'number') this.stats.misshapen = header.misshapen
    if (!hasShape(header.w, header.h, header.css)) this.stats.offShape += 1
    this.stats.pixels += header.w * header.h
    this.stats.bytes += bytes.byteLength
    this.cameAt = performance.now()
    if (isPng(bytes)) this.stillAt = this.cameAt
    this.arrivedAt.set(header.seq, this.cameAt)
    this.arrivedBytes.set(header.seq, bytes.byteLength)
    if (this.arrivedAt.size > 64) this.arrivedAt.delete(this.arrivedAt.keys().next().value as number)
    if (this.arrivedBytes.size > 64) this.arrivedBytes.delete(this.arrivedBytes.keys().next().value as number)
    this.gateway = { roundMs: typeof header.round_ms === 'number' ? header.round_ms : null, pace: typeof header.pace === 'number' ? header.pace : null, held: typeof header.held === 'boolean' ? header.held : null }
    if (typeof header.round_ms === 'number') keep(this.stats.roundMs, header.round_ms)
    if (this.waiting && !this.waiting.again) this.stats.replaced += 1
    // One decode runs at a time. A picture that arrives meanwhile replaces the one that waits.
    this.waiting = { header, bytes }
    this.last = { header, bytes, again: true }
    if (!this.decoding) void this.decode()
  }

  /**
   * The page's pictures go to this layer from now on, or back into the texture with `null`. The picture that is
   * shown now is decoded once more, the way up that the new place wants, so the page is never without one.
   */
  toLayer(layer: PageLayer | null): void {
    if (layer === this.layer) return
    this.layer = layer
    if (this.gone || !this.last) return
    // A picture that waits to be decoded is newer than the last one, and takes the new way up by itself.
    this.waiting ??= this.last
    if (!this.decoding) void this.decode()
  }

  /** The size of the newest picture, in pixels. */
  pictureSize(): [number, number] | null {
    const header = this.ready?.header ?? this.last?.header
    return header ? [header.w, header.h] : null
  }

  private async decode(): Promise<void> {
    this.decoding = true
    while (this.waiting && !this.gone) {
      const { header, bytes, again = false } = this.waiting
      this.waiting = null
      const ofOpen = this.openSeq
      const still = isPng(bytes)
      const flipped = this.layer !== null
      const t0 = performance.now()
      let bitmap: ImageBitmap
      try {
        const blob = new Blob([bytes as BlobPart], { type: still ? 'image/png' : 'image/jpeg' })
        // For the texture, no options: each one makes the browser copy the whole decoded picture once more, and the
        // texture's own `repeat` and `offset` turn the picture the right way up. A layer has nothing that could
        // turn it, and WebGL does not turn a bitmap on upload: for a layer the decoder turns it, and pays the copy.
        bitmap = await (flipped ? createImageBitmap(blob, { imageOrientation: 'flipY' }) : createImageBitmap(blob))
      } catch {
        // A picture that cannot be read is still answered, or the gateway would wait for it.
        if (!again) sendXrPanel({ op: 'ack', id: this.id, seq: header.seq, stream: header.stream })
        continue
      }
      // The page was opened anew while this picture was decoded: it is a picture of the open before (small 3).
      if (ofOpen !== this.openSeq) { bitmap.close(); continue }
      if (!again) { keep(this.stats.decodeMs, performance.now() - t0); const a = this.arrivedAt.get(header.seq); if (a !== undefined) keep(this.stats.queueMs, t0 - a); this.decodedAt = performance.now() }
      // The gateway paces the next picture by this word; every rule that waits for his sight reads "landed" (`landed`).
      if (!again) sendXrPanel({ op: 'ack', id: this.id, seq: header.seq, stream: header.stream, kind: 'decoded' })
      if (this.gone) { bitmap.close(); break }
      if (still && !again) this.stats.stills += 1
      // Content: a picture that is not flat, a still as much as a moving one (D55 3). A page that stays flat opens
      // by the content clock alone, once a picture of this open has landed. Asked only until the first such picture.
      // Only a page that is drawn (its panel's frame asked for a picture within the last second) is tested here: a
      // page with no panel (warm, warmed ahead, put away) lands nothing, so its picture is tested when it lands, if it
      // ever does (`contentOf`).
      let content: boolean | null = this.contentAt !== null && !this.contentByCap ? true : null
      if (content === null && !again && performance.now() - this.drawnAt < 1000) content = this.contentOf(bitmap)
      // A picture that was never drawn gives way to the newer one. If that one had not been answered yet, this one
      // answers for it: the gateway waits for one answer, whichever picture is shown.
      const unanswered = this.ready && !this.ready.again ? this.ready.header : null
      this.ready?.bitmap.close()
      this.ready = unanswered && again ? { header: unanswered, bitmap, still, flipped, again: false, content } : { header, bitmap, still, flipped, again, content }
    }
    this.decoding = false
  }

  /** Whether a decoded picture is content: it is not flat (D50, D55 3). Timed. */
  private contentOf(bitmap: ImageBitmap): boolean {
    const f0 = performance.now()
    const content = !flatness.of(bitmap)
    keep(this.stats.flatMs, performance.now() - f0)
    keep(this.stats.flatDrawMs, flatness.split[0])
    keep(this.stats.flatReadMs, flatness.split[1])
    return content
  }

  /** Before a frame is drawn: a new picture goes into the texture, and only then is the gateway told it arrived. */
  beforeDraw(renderer: WebGLRenderer, mipmaps: boolean): void {
    this.drawnAt = performance.now()
    const next = this.ready
    if (!next || this.gone) return
    if (next.flipped !== (this.layer !== null)) {
      // Decoded for the place the page has just left: once more, the other way up.
      this.ready = null
      next.bitmap.close()
      if (this.last) this.waiting ??= { header: next.header, bytes: this.last.bytes, again: next.again }
      if (!this.decoding) void this.decode()
      return
    }
    // A picture decoded while no panel drew the page is tested as it lands (REVIEW-17 small 4).
    if (next.content === null && !next.again) next.content = (this.contentAt !== null && !this.contentByCap) || this.contentOf(next.bitmap)
    if (this.layer) {
      // On a layer the picture is the layer's to write, inside the frame; the texture is left as it is. A picture
      // offered before an open lands as nothing after it (S3).
      const ofOpen = this.openSeq
      const taken = this.layer.offer({ bitmap: next.bitmap, width: next.header.w, height: next.header.h, flipped: true, done: () => { if (ofOpen === this.openSeq) this.landed(next.header, next.again, next.content ?? false) } })
      // Not taken: the layer is of another size, and the director makes one of this size on its next look.
      if (taken) this.ready = null
      return
    }
    this.ready = null
    const { header, bitmap, still } = next
    if (!next.again && this.decodedAt) keep(this.stats.readyWaitMs, performance.now() - this.decodedAt)
    const old = this.texture
    // A kept texture is never written again: the next picture, of any size, goes into a new one.
    const sameSize = old !== this.blank && old !== this.kept && this.stats.picture[0] === header.w && this.stats.picture[1] === header.h
    // Small copies are for a picture that stays. A moving one is replaced at once: its copies would be made for nothing.
    const copies = mipmaps && (still || this.copiesWhenMoving)
    const t0 = performance.now()
    let texture = old
    if (sameSize) {
      const last = old.image as ImageBitmap | undefined
      old.image = bitmap
      last?.close?.()
    } else {
      // A texture has one size for life.
      texture = new Texture(bitmap)
      texture.colorSpace = SRGBColorSpace
      texture.magFilter = LinearFilter
      texture.anisotropy = old.anisotropy
      // The bitmap's first row is the page's top; a texture's first row is its bottom.
      texture.repeat.set(1, -1)
      texture.offset.set(0, 1)
      if (this.before !== this.kept) this.before?.dispose()
      this.before = old === this.blank ? null : old
      this.texture = texture
    }
    // The filter is changed only when the page stops or starts to move: a change makes the renderer build the
    // texture again, so it must not happen for each picture.
    if (texture.generateMipmaps !== copies || !sameSize) {
      texture.generateMipmaps = copies
      texture.minFilter = copies ? LinearMipmapLinearFilter : LinearFilter
      if (sameSize) this.stats.switches += 1
    }
    texture.needsUpdate = true
    renderer.initTexture(texture)
    keep(this.stats.writeMs, performance.now() - t0)
    const resized = this.landed(header, next.again, next.content ?? false)
    // Only a new texture or a new size is the panel's business. A new picture of the same size is not.
    if (!sameSize && !resized) this.changed()
  }

  /**
   * A picture is where the eye sees it, in the texture or in the layer: the times are taken, and only now is the
   * gateway told that it arrived. Returns whether the page's size in CSS pixels changed (the panel is then told).
   */
  private landed(header: XrPanelPicture, again: boolean, content = true): boolean {
    const now = performance.now()
    // From the frame its first picture with content is shown, a new page takes input (D50).
    if (content && (this.contentAt === null || this.contentByCap)) { this.contentAt = now; this.contentByCap = false }
    this.stats.picture = [header.w, header.h]
    const arrived = this.arrivedAt.get(header.seq)
    if (arrived !== undefined && !again) keep(this.stats.landMs, now - arrived)
    // The first picture that arrived after he came back is the fresh one (a decoded one from before does not count).
    if (!again && this.freshSince !== null && arrived !== undefined && arrived >= this.freshSince) this.freshSince = null
    if (this.inputAt) { keep(this.stats.inputMs, now - this.inputAt); this.inputAt = 0 }
    // The picture that answers a press takes its mark away: the first to land a round or more after the press.
    if (!again && this.pressed && now - this.pressed.at >= (this.gateway?.roundMs ?? 0)) this.pressed = null
    // A picture decoded again for another place (a layer) is the old one: it ends no wait (D55 2, M9).
    if (this.backAt && !again) { keep(this.stats.backMs, now - this.backAt); this.backAt = 0 }
    if (!again) { this.landedThisOpen = true; this.noPictureMs = 0; this.noPictureSince = null; this.askedAgain = false; this.firstLandAt ??= now }
    // A picture that was only decoded again for another place is no new picture of the page.
    if (!again && this.pictureSince === null) { this.pictureSince = now; this.changed() }
    if (!again) { this.stats.landed += 1; this.stats.landedBytes += this.arrivedBytes.get(header.seq) ?? 0; this.arrivedBytes.delete(header.seq); sendXrPanel({ op: 'ack', id: this.id, seq: header.seq, stream: header.stream, kind: 'landed' }) }
    // The page changed under a pointer that rests: one move to its place lets the page's own hover follow (what
    // moved away from under it loses its light). One a picture, and none for a page at rest.
    const rest = this.moveAt
    if (!again && rest && this.on && !this.moving && !this.gone) this.move(rest)
    const resized = header.css[0] !== this.css[0] || header.css[1] !== this.css[1]
    this.css = header.css
    if (resized) this.changed()
    if (!again) this.hideIfFilled()
    return resized
  }

  /* ---------------- what the panel tells ---------------- */

  resize(cssWidth: number, cssHeight: number): void {
    const want: [number, number] = [Math.round(cssWidth), Math.round(cssHeight)]
    if (want[0] === this.want[0] && want[1] === this.want[1]) return
    this.want = want
    // The newest size wins: only the size the drag has come to rest on is laid out.
    if (this.sizeTimer) clearTimeout(this.sizeTimer)
    this.sizeTimer = setTimeout(() => { this.sizeTimer = null; sendXrPanel({ op: 'size', id: this.id, w: this.want[0], h: this.want[1] }) }, SIZE_SETTLE_MS)
  }

  visible(on: boolean, panel = false): void {
    if (on === this.shown) {
      // Its panel went while its hide was held (put away): nothing of it lands any more, so it is told now (M4).
      if (!on && !panel && this.hideHeld) this.hideNow()
      return
    }
    // It leaves the view: how long it showed its picture is kept.
    if (!on && this.pictureSince !== null) this.shownFor = Math.max(this.shownFor, this.shownNowMs())
    // It leaves the view while a pointer is on it (a room switched under the resting dot): the pointer leaves it too.
    // The gateway takes a pointer off a page only at `leave` (`panel_hub._input`), and a page that still holds one
    // comes back as a moving page (N6, REVIEW-20). Back under the dot, the pointer's next move enters it again. A
    // press cannot mean anything across a hide, so any press is forgotten: its release clicks nothing (R1, REVIEW-21).
    if (!on) this.press = null
    if (!on && this.on) { this.on = false; this.aim = null; this.aimPlace = null; this.moveNext = null; this.moveAt = null; void this.send({ type: 'leave' }) }
    this.shown = on
    this.backAt = on ? performance.now() : 0
    // A page with no picture of its open counts its wait for one only while it is in his view (D55 1).
    if (!this.landedThisOpen && this.state === 'live') { if (on) this.noPictureSince = performance.now(); else this.pauseNoPicture(performance.now()) }
    // Out of view the page got no pictures: back in view it shows an old one until the next arrives. Over a held hide
    // the gateway was never told he left and went on sending, so the picture on the texture is not old: the page does
    // not wait for a fresh one, and no word is sent that would bring one (D53 1, M7).
    // Its picture counts from now, as one shown anew, so the time out of view is not counted as time in view.
    if (on) this.pictureSince = this.hideHeld && (this.pictureSince !== null || this.stats.landed > 0) ? performance.now() : null
    // A page on a panel that he never had whole in his view goes on filling out of view until it has (D48, A): the
    // gateway is told later. One that he had whole for 4 s stops at once: it comes back whole either way. A page
    // with no panel (put away, warm) is told at once: no picture of it lands, so it would never be told (M4).
    // A page whose pictures were cut before it filled (`hideCut`) never fills out of view: its hide goes at once (M5).
    if (!on && panel && !this.hideCut && !this.wasWhole(WHOLE_MS) && !this.filledOutOfView()) { this.hideHeld = true; this.hideIfFilled(); return }
    if (on && this.hideHeld) { this.hideHeld = false; return }
    if (on) this.sayShown()
    else sendXrPanel({ op: 'visible', id: this.id, visible: false })
  }

  gaze(deg: number, far?: number): void { this.gazeDeg = deg; this.gazeFar = far }

  // A warm page told "seen" by a rest on its room's card (`peek`) is not in his view: it is never the watched page (D53 5).
  watchPage(): WatchPage { return { id: this.id, inView: this.shown && this.earlyAt === null && this.state !== 'failed', gazeDeg: this.gazeDeg, far: this.gazeFar } }

  /** He looks at this page (the moving pace with no pointer on it), or no longer. An older gateway drops the word. */
  /**
   * Tell the gateway he sees the page, and again that he watches it when he does. The gateway forgets the watch at
   * every `visible: false` and takes none for a page out of view (`panel_hub.set_visible`, `set_watched`), so a
   * watch said once before would leave the page he looks at on the held pace (REVIEW-19 S1).
   */
  private sayShown(): void {
    sendXrPanel({ op: 'visible', id: this.id, visible: true })
    if (this.watching) sendXrPanel({ op: 'watch', id: this.id, on: true })
  }

  watch(on: boolean): void {
    if (on === this.watching || this.gone) return
    this.watching = on
    sendXrPanel({ op: 'watch', id: this.id, on })
  }

  detail(texelsPerCssPx: number): void {
    const step = detailStep(texelsPerCssPx, this.step)
    if (step === this.step) return
    this.step = step
    sendXrPanel({ op: 'detail', id: this.id, detail: step })
  }

  pointer(event: PagePointer): void {
    // Its picture may not be the page (yet, or any more): what he does goes nowhere until it is (D49, D50). The
    // press is judged at its down: a down that went nowhere leaves no press, so its release sends nothing. A cancel
    // always ends the press, and a down or a release that the wait takes ends the one before it: a release after the
    // wait never sends a click for a down that came before the wait (D53 6, REVIEW-17 small 1).
    if (event.type !== 'move') this.logPress(event)
    if (event.type === 'cancel') { this.press = null; return }
    // A down is judged by the frame he saw before it (`seen`), when the panel says it: a word that turned between
    // that frame and the down does not decide it (D50).
    if (event.type === 'down' && event.seen !== undefined) { this.press = event.seen ? this.clamp(event.x, event.y) : null; return }
    // A press on the glass of a page failed for want of a picture asks for the page anew, as its line says (D56 1).
    if (event.type === 'up' && this.press && this.note() === 'no_picture') { this.press = null; this.askAgain(); return }
    if (this.notYet()) { if (event.type === 'down' || event.type === 'up') this.press = null; return }
    const at = this.clamp(event.x, event.y)
    if (event.type === 'move') {
      this.on = true
      const last = this.moveNext ?? this.moveAt
      if (last && last.x === at.x && last.y === at.y) return
      this.move(at)
      return
    }
    if (event.type === 'down') { this.press = at; return }
    if (event.type === 'leave') { this.on = false; this.aim = null; this.aimPlace = null; this.moveNext = null; this.moveAt = null; void this.send({ type: 'leave' }); return }
    // A release: the press and the release go together, where the hand let go. One hand at a time.
    if (!this.press) return
    this.press = null
    // A pinch that travelled (a drag) or was held long ends with no click, as on a page drawn by the tab. The stream
    // sends a press as one click at its release, so such a pinch sends nothing to the page (shortcut: a page's own
    // long press does not run on a streamed page; send the down at the down when one needs it).
    if (pinchRoute.ending === 'drag' || pinchRoute.ending === 'long') return
    this.inputAt = performance.now()
    // On an ask's panel the press says where it saw the controls that answer an ask. The gateway sends none when the
    // page draws them elsewhere now: a card that came in the round trip. Its word then starts the gate's second.
    const word = this.answers
    const grants = pressPlaces(word?.heard() ?? null)
    // What he aimed at: the control that the page said stands at this place in the picture on the texture (the move
    // after each new picture asks it, `landed`). The gateway sends the press only when the same control stands there now.
    // A move on its way to this same place only makes that answer newer, so the press still carries the last one; a
    // pointer that went to another place and has no answer for it yet carries none.
    // Its label goes with the box: two controls of one size that swap places have one box and two labels.
    const aimed = this.aimPlace && this.aimPlace.x === at.x && this.aimPlace.y === at.y ? (this.aim ? [...this.aim.rect, ...(this.aimLabel ? [this.aimLabel] : [])] : null) : undefined
    // The mark at the press, at once; none where the aim already showed a control that a plain press does not take.
    const mark = aimed && this.aim?.guarded ? null : { ...at, at: this.inputAt }
    this.pressed = mark
    void this.send({ type: 'mouse', action: 'click', ...at, ...(grants ? { grants } : {}), ...(aimed !== undefined ? { aimed } : {}) }).then(answer => {
      // A press that the gateway did not send to the page leaves no mark standing.
      if (this.pressed === mark && (!answer || !answer.sent || answer.lost)) this.pressed = null
      if (answer?.stale) this.say('stale')
      if (answer?.moved && Array.isArray(answer.grants)) word?.moved(JSON.stringify(answer.grants))
      if (answer?.guarded) this.say('guarded')
      if (answer?.picker) this.say('picker')
      // A press that was sent says where the page's focus went.
      if (answer && 'focus' in answer) this.heard(answer.focus)
      if (answer?.target !== undefined && this.on) this.aimFrom(answer.target, at)
    })
  }

  takesPress(): boolean { return this.press !== null && !this.notYet() }

  /** For the probe: the last pointer words and what became of each (down taken or not, up sent or why not). */
  private readonly pressLog: { ms: number; type: string; seen: boolean | null; notYet: boolean; had: boolean; ending: string }[] = []
  private logPress(e: PagePointer): void {
    this.pressLog.push({ ms: Math.round(performance.now()), type: e.type, seen: e.seen ?? null, notYet: this.notYet(), had: this.press !== null, ending: String(pinchRoute.ending ?? '') })
    if (this.pressLog.length > 12) this.pressLog.shift()
  }

  /**
   * One move is on its way at a time; the newest place waits behind it. On the socket the next goes `INPUT_GAP_MS`
   * after this one, not after its answer: only the newest move's answer says what he aims at.
   */
  private move(at: { x: number; y: number }): void {
    if (this.moving) { this.moveNext = at; return }
    this.moving = true
    this.moveAt = at
    this.moveCount += 1
    const mine = this.moveCount
    const onSocket = socketTakesInput()
    const after = () => {
      this.moving = false
      const next = this.moveNext
      this.moveNext = null
      if (next && !this.gone) this.move(next)
    }
    void this.send({ type: 'mouse', action: 'move', ...at }).then(answer => {
      // An answer that comes back after the pointer left lights nothing. A move whose send was cut (`lost`) asked the
      // page nothing it answered: the aim stays what the last answer said.
      if (answer && !answer.lost && this.on && mine === this.moveCount) this.aimFrom(answer.target, at)
      if (!onSocket) after()
    })
    if (onSocket) window.setTimeout(after, INPUT_GAP_MS)
  }

  scroll(event: PageScroll): void {
    if (this.notYet()) return
    if (this.wheeling) {
      // Scrolls that wait are added up: the page ends where the hand did.
      const w = this.wheelNext
      this.wheelNext = { x: event.x, y: event.y, dx: (w?.dx ?? 0) + event.dx, dy: (w?.dy ?? 0) + event.dy }
      return
    }
    this.wheeling = true
    this.inputAt ||= performance.now()
    const at = this.clamp(event.x, event.y)
    const after = () => {
      this.wheeling = false
      const next = this.wheelNext
      this.wheelNext = null
      if (next && !this.gone) { this.scroll(next); return }
      // The page moved under a pointer that rests: what is under it now is asked once, with a move to the same place.
      const rest = this.moveAt
      if (rest && this.on && !this.gone && !this.moving) this.move(rest)
    }
    // On the socket the next wheel goes `INPUT_GAP_MS` after this one, not after its answer.
    const onSocket = socketTakesInput()
    const sent = this.send({ type: 'wheel', ...at, dx: Math.round(event.dx), dy: Math.round(event.dy) })
    if (onSocket) window.setTimeout(after, INPUT_GAP_MS)
    else void sent.then(after)
  }

  text(text: string): void {
    if (!text || this.notYet()) return
    // Letters typed one after the other go on as one text while the one before them is on its way.
    const last = this.typing[this.typing.length - 1]
    if (last && 'text' in last && last.text.length + text.length <= TEXT_CHARS) last.text += text
    else for (let i = 0; i < text.length; i += TEXT_CHARS) this.typing.push({ text: text.slice(i, i + TEXT_CHARS) })
    void this.type()
  }

  key(event: PageKey): void {
    if (event.type !== 'down' || this.notYet()) return
    this.typing.push({ key: event.key })
    void this.type()
  }

  /** Send what waits, one at a time. Two messages on their way at once could reach the page in either order. */
  private async type(): Promise<void> {
    if (this.typingOn) return
    this.typingOn = true
    while (this.typing.length && !this.gone) {
      const next = this.typing.shift()
      if (!next) break
      this.inputAt = performance.now()
      // Text and keys go to the page's focus. With the focus inside a guarded surface the gateway sends none of
      // it, and says so: what was typed behind it was meant for the same place, and is dropped. A piece whose send was
      // cut (`lost`) drops what waits behind it too, so the page never gets words with a gap in them.
      const answer = await this.send('text' in next ? { type: 'text', text: next.text } : { type: 'key', key: next.key })
      if (answer?.guarded) { this.say('guarded'); this.typing.length = 0 }
      if (answer?.lost) this.typing.length = 0
    }
    this.typingOn = false
    // Enter may have sent a message, Tab moved on: where the focus is now is asked once, after the last one.
    await this.look()
  }

  focus(): PageFocus | null { return this.state === 'live' ? this.where : null }

  refocus(): void { void this.look() }

  answersBy(word: AnswerWord | null): void { this.answers = word }
  windows(open: ((path: string) => void) | null): void { this.opens = open }

  readout(): PageReadout {
    const s = this.stats
    // The readout counts pictures that reached the texture (or the layer), not arrivals: a picture replaced before it
    // was drawn is one he never saw.
    return { way: 'stream', count: s.landed, picture: s.picture, bytes: s.landedBytes, visible: this.shown, landMs: s.landMs, decodeMs: s.decodeMs, readyWaitMs: s.readyWaitMs, writeMs: s.writeMs, inputMs: s.inputMs, gateway: this.gateway, roundMs: s.roundMs }
  }

  async answerPlaces(): Promise<string | null> {
    if (this.gone || !this.handle || this.state !== 'live') return null
    try {
      const state = (await api.browserPanelAsk(this.handle, 'state')).answer as { grants?: unknown } | null | undefined
      return state && Array.isArray(state.grants) ? JSON.stringify(state.grants) : null
    } catch { return null }
  }

  private async look(): Promise<void> {
    if (this.asking || this.gone || !this.handle || this.state !== 'live') return
    this.asking = true
    try {
      this.heard((await api.browserPanelAsk(this.handle, 'focus')).answer)
    } catch { /* not answered: what was heard last stands */ } finally {
      this.asking = false
    }
  }

  /** The gateway's word on the page's focus. The same object stays while nothing changed. */
  private heard(raw: unknown): void {
    const f = raw as (Partial<PageFocus> & { kind?: unknown }) | null | undefined
    const next = f && typeof f.editable === 'boolean' && typeof f.guarded === 'boolean' ? { editable: f.editable, guarded: f.guarded, secret: f.kind === 'password' } : null
    if (next?.editable === this.where?.editable && next?.guarded === this.where?.guarded && next?.secret === this.where?.secret && (next === null) === (this.where === null)) return
    this.where = next
  }

  private clamp(x: number, y: number): { x: number; y: number } {
    return { x: Math.round(Math.min(this.css[0], Math.max(0, x))), y: Math.round(Math.min(this.css[1], Math.max(0, y))) }
  }

  /** What the page said stands at `at`: the control and its label, kept for a press at that place. */
  private aimFrom(target: BrowserPanelTarget | null | undefined, at: { x: number; y: number }): void {
    this.aim = this.targetOf(target)
    this.aimLabel = this.aim && typeof target?.label === 'string' ? target.label.slice(0, 80) : ''
    this.aimPlace = at
  }

  private targetOf(target: BrowserPanelTarget | null | undefined): PageTarget | null {
    if (!target || !Array.isArray(target.rect) || target.rect.length !== 4) return null
    return { rect: target.rect, kind: target.kind, guarded: target.guarded === true }
  }

  keepPicture(): Texture {
    if (this.kept !== this.texture) this.letGoPicture()
    if (this.texture !== this.blank) this.kept = this.texture
    return this.texture
  }

  letGoPicture(): void {
    const k = this.kept
    this.kept = null
    // Freed here only when the source no longer holds it itself, as its picture now or the one before.
    if (!k || k === this.texture || k === this.before) return
    ;(k.image as ImageBitmap | undefined)?.close?.()
    k.dispose()
  }

  dispose(): void {
    if (this.gone) return
    this.gone = true
    this.letGoPicture()
    watchables.delete(this)
    this.unlisten()
    if (this.sizeTimer) clearTimeout(this.sizeTimer)
    if (this.hideTimer) clearTimeout(this.hideTimer)
    sendXrPanel({ op: 'close', id: this.id })
    this.ready?.bitmap.close()
    this.ready = null
    this.waiting = null
    for (const t of [this.texture, this.before]) {
      if (!t || t === this.blank) continue
      ;(t.image as ImageBitmap | undefined)?.close?.()
      t.dispose()
    }
    this.blank.dispose()
    this.listeners.clear()
  }
}
