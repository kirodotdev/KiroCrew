/**
 * THE SOURCE CONTRACT: where a panel of the workspace gets its picture, and what it tells the page behind it.
 *
 * A panel (`Panel.tsx`) is a flat rectangle in the room. It draws no page itself. It is given a `PageSource`, and
 * that is the whole seam between the panel shell and whatever makes the page's pixels: a still picture
 * (`stillSource.ts`), a canvas drawn here (`gridSource.ts`), and later a live page.
 *
 * Everything on this seam is in CSS PIXELS of the page, with the origin at the page's top left corner, x to the
 * right and y down. The panel knows metres; the source never does.
 *
 * What the source gives:
 *   - `frame()`      the picture as it is now: a three.js texture that covers the whole page, and the page's size in
 *                    CSS pixels. `texture` may hold more texels than CSS pixels (a sharper picture); the panel never
 *                    assumes one texel is one CSS pixel. The texture's own `offset` and `repeat` are the source's to set.
 *                    How it is filtered (mipmaps, anisotropy) is the panel's to set.
 *   - `subscribe()`  hears when `frame()` would answer something new: a new picture, or a new size.
 *
 * A texture has one size for its whole life: a picture of another size is another texture. The panel may draw the
 * texture it was given before for a frame or two after the source has a new one, so a source frees a texture it has
 * replaced only when it replaces the next one, or in `dispose()`. The workspace calls `dispose()` once no panel draws
 * the source any more.
 *
 * What the source is told:
 *   - `resize()`     the panel wants the page laid out for this many CSS pixels: the person made the panel bigger or
 *                    smaller, so the page gets more or less room at the same size of text. The source answers through
 *                    `frame()`: it may settle on another size than it was asked for (a still has only the sizes it was
 *                    taken at), and the panel then takes the size the source gives. The panel calls it on every step
 *                    of a drag: THE NEWEST SIZE WINS, and a source that lays the page out somewhere slow lays it out
 *                    for the newest size only.
 *   - `pointer()`    the page's own pointer: a hand's ray or the mouse moved over the page, pressed, let go, or left.
 *                    One pointer id is one hand. A press, then a release at the same place, is a click. Between a
 *                    press and its release every move goes to the page, also outside its rectangle.
 *   - `scroll()`     the person dragged the page with a press held: scroll by this many CSS pixels, at this place.
 *   - `key()`        a key for whatever has the page's focus: Enter, Backspace, an arrow.
 *   - `text()`       text for whatever has the page's focus, as if it were typed. In the flat preview a real
 *                    keyboard and the phone's own text field call both (`typing.ts`); a test's script does too.
 *                    They reach the page in the order they were given.
 *   - `visible()`    whether any of the panel is in the person's view. A source that pays for each picture stops
 *                    making them for a page nobody sees.
 *   - `detail()`     how many picture pixels one CSS pixel of the page is worth now: about as many as it covers on
 *                    the display, and a little more. A far or a small panel asks for less. The source may round it.
 *   - `beforeDraw()` called by the panel once before each frame is drawn, with the renderer. A source whose picture
 *                    changes writes the new one into its texture here, before the frame's drawing starts, and never
 *                    in between. `mipmaps` says whether the panel wants the texture's small copies: a source may
 *                    leave them out for a picture that is replaced at once and make them for one that stays.
 *   - `dispose()`    the panel is gone: free the picture.
 *   - `toLayer()`    a source whose pictures can go onto a composition layer (`pageLayer.ts`) is told here where to
 *                    send them: to that layer, or back into its texture. `pictureSize()` says how big its newest
 *                    picture is. A still and a sheet have neither, and stay on their mesh.
 *
 * What a live source also says (a still has none of it):
 *   - `status()`     whether the page is there: `loading`, `live`, `signed-out` (the gateway does not know the
 *                    caller, so there is no picture and no input), `failed`, `closed`.
 *   - `target()`     the thing of the page that a pinch would take where the pointer is now: its rectangle in CSS
 *                    pixels. The panel lights it before the pinch, so the pinch takes what is lit. `guarded` says
 *                    the thing grants an agent permission: a plain pinch is not passed to the page.
 *   - `note()`       one short thing the panel should say now, as a code the panel has words for, or ''.
 *   - `focus()`      where the page's focus is, as the gateway last said: in a field that takes text or not, and
 *                    whether that field is inside a surface that grants an agent permission. `null` until a press
 *                    has been answered. The same object for as long as nothing changed. `refocus()` asks again:
 *                    a page can move its focus by itself.
 *
 * A source never throws for an event it has no use for. A still takes them and does nothing.
 */
import type { Camera, Object3D, Texture, WebGLRenderer } from 'three'
import type { PageLayer } from './pageLayer'

/** The picture of a page, and how big the page is in its own CSS pixels. */
export interface PageFrame {
  texture: Texture
  cssWidth: number
  cssHeight: number
}

/** The last word of a page on where the controls that answer an ask are, as `answerPlaces` gives it, and its keeper. */
export interface AnswerWord {
  /** That word now, or `null` while nothing was heard. */
  heard(): string | null
  /** The gateway did not send a press: the page draws them at these places now. */
  moved(places: string): void
}

/** What the readout (`?fps=1`, `Readout.tsx`) reads of a page. The lists are the source's own: read, never kept. */
export interface PageReadout {
  /** How the page's picture comes: the gateway's stream, or the page itself in this tab. */
  way: 'stream' | 'dom'
  /** Pictures that reached the texture so far; for the page itself, its uploads. */
  count: number
  picture: [number, number]
  /** The bytes of every picture so far; 0 for the page itself. */
  bytes: number
  visible: boolean
  /** Each picture's way, in ms (the last few hundred): socket to texture, its decode, the wait for a frame, its upload, and press to picture. */
  landMs: readonly number[]
  decodeMs: readonly number[]
  readyWaitMs: readonly number[]
  writeMs: readonly number[]
  inputMs: readonly number[]
  /** What the gateway said with the last picture, or `null` (no picture yet, or no gateway). */
  gateway: { roundMs: number | null; pace: number | null; held: boolean | null } | null
  /** The gateway's round trip of each picture that said one (its header's `round_ms`: send to decoded ack), the last few hundred. */
  roundMs: readonly number[]
}

/** What the person did inside a page that the browser hands input to itself (`domSource.ts`), as the host hears it. */
export type PageInput = 'down' | 'up' | 'wheel' | 'key'

/** What a pointer of the page did. `x` and `y` are CSS pixels from the page's top left corner. */
export interface PagePointer {
  type: 'move' | 'down' | 'up' | 'cancel' | 'leave'
  /** One hand, or the mouse: the same number for as long as that pointer is there. */
  pointerId: number
  x: number
  y: number
  /**
   * On a down: whether the frame he saw before it showed the page taking a press (not dimmed, the dot lit; D50: a
   * press is judged when the fingers closed, by what he saw). Left out: judged by the page's word now.
   */
  seen?: boolean
}

/** A scroll of the page: by `dx` and `dy` CSS pixels, with the pointer at `x`, `y`. Down the page is positive `dy`. */
export interface PageScroll {
  x: number
  y: number
  dx: number
  dy: number
}

/** A key for the page, as the browser names it (`KeyboardEvent.key`). */
export interface PageKey {
  type: 'down' | 'up'
  key: string
}

/** Whether a live page is there. */
export type PageStatus = 'loading' | 'live' | 'signed-out' | 'failed' | 'closed'

/** The thing a pinch would take: its rectangle on the page in CSS pixels (left, top, width, height). */
export interface PageTarget {
  rect: [number, number, number, number]
  kind: 'button' | 'text' | ''
  /** It grants an agent permission: a plain pinch on it is not passed to the page. */
  guarded: boolean
}

/** Where a live page's focus is. `guarded`: inside a surface that grants an agent permission, where text and keys are not passed on. */
export interface PageFocus {
  editable: boolean
  guarded: boolean
  /** The field is a password field: what is typed into it is for no keyboard to learn or suggest. */
  secret?: boolean
}

/** What the panel says in one line: nothing, a pinch that was not passed on, a page it may not open, or why there is no page. */
export type PageNote = '' | 'guarded' | 'refused' | 'dialog' | 'picker' | 'stale' | 'signed-out' | 'failed' | 'off' | 'full' | 'host' | 'elsewhere' | 'waiting' | 'no_picture'

/**
 * How many live pages one person can have open at once: the gateway's own limit for one socket
 * (`browser_harness/panel_hub.py`, `MAX_PANELS_PER_SOCKET`). The gateway refuses the next one, and its panel says
 * so in one line that names this number; a test holds the two numbers together.
 */
export const MOST_LIVE_PAGES = 12

/** What a page says of itself while it fills out of sight (`PageSource.restFacts`). */
export interface RestFacts { liveForMs: number | null; lastPictureMs: number | null; stillAfterLiveMs: number | null }

export interface PageSource {
  /** Names the source in the test probe. */
  readonly id: string
  frame(): PageFrame
  /** Returns the call that stops it. */
  subscribe(changed: () => void): () => void
  resize(cssWidth: number, cssHeight: number): void
  pointer(event: PagePointer): void
  /** Whether a release now would send the press's click: its down was taken, and the page takes input. Left out: yes. */
  takesPress?(): boolean
  /**
   * Keep the picture drawn now as it is (a held corner's kept picture, M52, D96 5): the next pictures go into another
   * texture, and this one is not freed until `letGoPicture`. Left out: the frame's texture is already never written
   * again.
   */
  keepPicture?(): Texture
  letGoPicture?(): void
  scroll(event: PageScroll): void
  key?(event: PageKey): void
  text?(text: string): void
  /** Whether he can see the page now. `panel`: the word comes from the page's own panel (its view), which a page
   * with no panel (put away, warm) does not have. */
  visible?(on: boolean, panel?: boolean): void
  /** How far the page is from the middle of his look (its point nearest to it), in degrees, and how far that point is from him, at each frame that it is in view. */
  gaze?(deg: number, far?: number): void
  detail?(texelsPerCssPx: number): void
  beforeDraw?(renderer: WebGLRenderer, mipmaps: boolean): void
  toLayer?(layer: PageLayer | null): void
  pictureSize?(): [number, number] | null
  status?(): PageStatus
  target?(): PageTarget | null
  /**
   * The mark at a press: where on the page (CSS pixels) a press that was sent stands, until the page's picture
   * answers it, or `null`. The panel draws a ring there at once, so the hand sees its press before the round trip.
   */
  pressMark?(): { x: number; y: number } | null
  note?(): PageNote
  /** The words of the note, when the source has its own (a page's own dialog text); else the panel's words for the code. */
  noteWords?(): string
  focus?(): PageFocus | null
  refocus?(): void
  /** The path of the dashboard page that the source shows now: where a link inside the page has led. No query. */
  pagePath?(): string
  /**
   * When the first picture of the page as it is now was shown: the first since it opened, and the first since the
   * panel came back into view. `null` while there is none: a press then does nothing (`asks.ts`, `pressFate`).
   * A source that has no such method always shows its page.
   */
  pictureAt?(): number | null
  /**
   * Whether the page has shown its picture for `ms` or more without a break, now or at some time since it opened: it
   * had the time to fill. A page that is kept open out of view must have been whole before (`warmRooms.ts`): one
   * that was hidden before it filled comes back as its shell.
   */
  wasWhole?(ms: number): boolean
  /**
   * It filled out of his view as a page warmed ahead does (`warmRooms.ts`, `filled`'s rule: a still 6 s or more
   * after it was live, else 15 s live), its pictures never cut off before. A page that he never had whole in his
   * view (never looked at, or only glanced at) counts as whole by this when he leaves its room (D48, A).
   */
  filledOutOfView?(): boolean
  /** Taken back from the warm set at a switch: it is seen again, and waits for its fresh picture (D49). */
  takenBack?(): void
  /**
   * It was taken back and its first fresh picture has not landed yet (3 s at most): its old picture may no longer be
   * the page. It takes no press, wheel or key, and is drawn dimmed (`Panel.tsx`).
   */
  awaitingFresh?(): boolean
  /**
   * The page takes no input yet and is drawn dimmed: a warm page waiting for its fresh picture (`awaitingFresh`), or a
   * new page whose first picture with content has not landed (3.5 s after live at most, D50). One sign for both.
   */
  notYet?(): boolean
  /** A warm page: his dot rests on its room's card, so it is told he may see it before the press, or no longer (D52). */
  peek?(on: boolean): void
  /**
   * For a page that is held open out of his sight until it has filled (`useWarmRooms.ts`, warm ahead): how long it
   * has been live, how long ago its newest picture reached the tab, and how long after it turned live its newest
   * still came: the gateway's lossless picture, which it sends once a page has stopped changing. `null` while
   * there is none.
   */
  restFacts?(): RestFacts
  /** It filled out of his sight: it counts as whole from here on (`wasWhole`). */
  countWhole?(): void
  /**
   * Where the controls that answer an ask are drawn on the page now, as one string to compare with the answer
   * before: the gateway's `state` answer names the places of the controls that carry the dashboard's grant mark,
   * and of a question card's Dismiss and Submit.
   * `null` when the page could not be asked. A source that has no such method has no such controls to wait for.
   */
  answerPlaces?(): Promise<string | null>
  /**
   * Who keeps the page's last word on those controls (`answerPlaces`), as `useAskPanels.ts` does for an ask's
   * panel. A press then carries that word, the gateway sends none when the page draws them elsewhere now, and its
   * new word goes to `moved`. `null`: a press carries none.
   */
  answersBy?(word: AnswerWord | null): void
  /**
   * Called by the panel each frame after `beforeDraw`, with the page's mesh: a page that the XR tab draws itself
   * (`domSource.ts`) tells the browser where it is drawn, so the browser's own hit test sends the pointer into it.
   */
  place?(mesh: Object3D, camera: Camera, renderer: WebGLRenderer): void
  /**
   * Whether the room's own ray finds this panel's page first under the mouse now. A page that the browser hands input
   * to itself takes the mouse only then, so a thing that the room draws in front of it keeps its click.
   */
  hover?(on: boolean): void
  /**
   * Who hears the person's own input inside the page, for a page that the browser hands input to itself: the host
   * then counts him busy as it does for a press, a notch or a key that it passes on (`asks.ts`, `isBusy`). A source
   * whose input all goes through the panel has none.
   */
  listen?(hear: ((input: PageInput) => void) | null): void
  /** What the readout shows of this page. A source that has none is left out of it. */
  readout?(): PageReadout
  /**
   * Who opens a window that the page opens: by its path, on a panel of its own, when it is a page of the gateway's
   * own origin that a panel may show. Any other window opens nothing and the page's line says so; no browser window
   * is ever opened (`domSource.ts`; the hub for a streamed page, `panel_window.py`).
   */
  windows?(open: ((path: string) => void) | null): void
  /** A page drawn by the tab takes no press of its own (the browser's) for `ms`: a window that came by itself. */
  holdFor?(ms: number): void
  /** A page drawn by the tab is told how to ask whether the room shows a point of it uncovered (`roomSees.ts`). */
  seenBy?(sees: ((x: number, y: number) => boolean) | null): void
  /** A page drawn by the tab: the browser paints it no more behind a session, and its panel hands it to the stream. */
  paintStopped?(): boolean
  /**
   * A page drawn by the tab that is to be streamed while a session runs, and why: its paint stopped (`paint`), or it
   * holds a frame of another site that may open a browser dialog, which nobody sees in the glasses (`frame`).
   */
  streamWanted?(): 'paint' | 'frame' | null
  /** The gateway refused to open the page at its address, before it showed anything. */
  refusedOpen?(): boolean
  /** Ask the gateway for the page once more when it had no room for it. Says whether it asked. */
  askAgain?(): boolean
  dispose(): void
}
