/**
 * The handle a test has on the workspace (`?probe=1`), and the shapes it opens and reads. Moved out of `Workspace.tsx`
 * whole, which keeps it under the repository's line rule; the workspace imports them from here.
 */
import type { Quaternion, Vector3 } from 'three'
import type { BoardMarks } from './Board'
import type { CrewProbe } from './Crew'
import type { DashMarks } from './Dash3d'
import type { DeskProbe } from './Desk'
import type { PaletteProbe } from './Palette'
import type { PanelProbe, Quality } from './Panel'
import type { PinchProbe } from './pinchRoute'
import type { DoneFoot } from './says'
import type { PageNote, PageSource } from './source'
import type { Still } from './stillSource'
import type { Turn, View } from './previewLook'
import type { BoardProbe } from './useBoards'

/** A page handed to the stream: on its way (no picture yet), or there. */
export type Handing = 'handing' | 'handed'

/** A panel that is open: its page, where it was last stood, and the size it opened at in CSS pixels. */
export interface Open { id: string; source: PageSource; position: Vector3; css: [number, number]; turned?: Quaternion; glide?: boolean; behindM?: number }

/**
 * What a test asks for when it opens a panel. A still names its pictures; `live` names a page of the dashboard to
 * show live (`copies: 'always'` makes its small copies for every picture, to measure what leaving them out saves);
 * with neither the panel shows the ruled sheet. `note`, with `?fixture=1` only: a sample page that says that line
 * under itself (a code of `noteText`, D80 2, M41).
 */
export interface OpenSpec { id: string; stills?: Still[]; live?: string; copies?: 'always'; cssWidth?: number; cssHeight?: number; yawDeg?: number; pitchDeg?: number; distanceM?: number; note?: PageNote }
/** Whether a test named the place itself. With no place named, a new page finds its own (`placeNew`). */
export const namesPlace = (spec: OpenSpec) => spec.yawDeg !== undefined || spec.pitchDeg !== undefined || spec.distanceM !== undefined

/** The handle a test has on the workspace. Present with `?probe=1` only. */
export interface TestHook {
  open: (spec: OpenSpec) => void
  /** Type into whatever has the focus of a panel's page, or press one key there. The keyboard is parked; a script stands in. */
  text: (id: string, text: string) => void
  key: (id: string, key: string) => void
  close: (id: string) => void
  clear: () => void
  /** Stand the open panels round the person, in the order they were opened. */
  arrange: () => void
  quality: (q: Partial<Quality>) => void
  /** Every Bot at rest strolls (`true`), none does (`false`), or each as `idleLife` picks (`null`): `Crew.crewStroll`. */
  stroll: (all: boolean | null, everyMs?: number) => void
  /** With `uplate=1` the sample crew's asks are held back; this lets them come, or (`false`) ends them again. */
  asksCome: (on?: boolean) => void
  /** Put the preview's head and lens where a test wants them. `lens: 'page'` is as far back as the eye can step: the whole first page is in view. */
  look: (to: { yawDeg?: number; pitchDeg?: number; lens?: 'page' | 'start' | number; shift?: number }) => void
  /** Stand in for a socket whose frames are held back: the crew's row then cannot know a state. */
  stale: (on: boolean) => void
  /** Stand in for a browser that paints a page drawn by the tab no more behind a session (`DomSource.paintless`). */
  paintless: (id: string) => void
  /** Stand in for an ask that changed after its board was taken: the held board is bound to an older version. */
  boardStale: (id: string) => void
  /** Draw every leaving board's pieces at this time of their own (seconds), or on their own clock again (`null`). */
  dustAt: (s: number | null) => void
  /** Put the new chat card away, as its close mark does. */
  cardClose: () => void
  /** What a card does, as a press would: go to a room, add one, name one, remove one, bring it back. */
  room: (act: 'go' | 'add' | 'new' | 'copy' | 'ask' | 'rename' | 'remove' | 'back', id?: string, name?: string) => void
  /** Open a page as the palette does (`openPage`): the room's own way, with its rules for the twelve. */
  page: (path: string) => void
  /** `placed` names the panels that a hand has moved or resized; `crossed` the ones that found no clear place when they opened. */
  /** `layers` says which pages a composition layer shows, and how many pictures each layer has had written. Empty in a session with no layers. */
  /** `turn` is where the preview's head looks, `view` its canvas and lens, and `near` how many times nearer than the start view the eye is. `keys` is the page that takes the keys, or null. `walls`: the drawn room is switched on. */
  /** `palette` is the palette on the palm: which hand holds it, which tile is lit, and where its tiles are. */
  /** `crew` is the Bots of the row; `least` how far back the eye can step, as a number of times the start view. */
  read: () => { panels: PanelProbe[]; sources: Record<string, unknown>; head: [number, number, number]; inSession: boolean; textures: number; calls: number; placed: string[]; crossed: string[]; layers: { onLayer: string[]; writes: Record<string, number> }; turn: Turn; turning: boolean; keys: string | null; walls: boolean; view: View; near: number; least: number; palette: PaletteProbe | null; crew: CrewProbe[]; asks: unknown; pinch: PinchProbe | null; plate: readonly string[]; plateCovers: { covers: string[]; under: string | null; where: string | null } | null; handing: Record<string, Handing>; desk: DeskProbe | null; boards: BoardProbe; boardMarks: Record<string, BoardMarks>; dash3d: Record<string, DashMarks>; says: Record<string, { raise: boolean; state: string; sentence: string; sheets: number; ownUnread: boolean; slot: string; version: string; done: DoneFoot | null; rest: { slot: string; unread: boolean; ts: number }[] }>; note: unknown }
}
