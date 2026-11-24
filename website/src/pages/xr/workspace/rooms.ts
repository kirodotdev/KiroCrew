/**
 * NAMED ROOMS (D37), as plain rules. Ray's first principle is more pages that he sets up as he likes: he keeps more
 * than one room, each as he left it, one press apart. Nothing here draws or reads a store: `useRooms.ts` keeps the
 * state and applies a switch, `RoomCards.tsx` draws the cards.
 *
 * - A room keeps itself as he changes it. There is no "save": the room that stands is the room's record of today
 *   (`room.ts`, the key `kirocrew:xr-room`), written as he changes it, and nothing about that changes.
 * - The list of rooms is a second key (`ROOMS_KEY`): each room's name, whether he named it, and its layout; and
 *   which one stands. A switch first writes the room he leaves into its place in the list, then makes the other
 *   room's layout the standing record. So a switch never loses a room, and a build that reads only the first key
 *   opens the room that stood last and ignores the list.
 * - A room holds his own doing: its pages (which, where, how big, whether a hand placed each), whether they are put
 *   away, and where he put Bots. It never holds the crew's state: who stands in front, which asks he handed back,
 *   his last visit and what changed since, or the marks that end with a Bot's state. Those are the same in every
 *   room and stay in the standing record across a switch (`standWith`). The drawn room and the desk stay too: they
 *   are how this device is set up, not a layout.
 * - Five rooms at most. The room that stands cannot be removed, nor the last one.
 */
import { releaseKind, type Moved } from './board'
import { signs } from './homeNames'
import { PAGES, M_PER_PX } from './layout'
import { ROOM_VERSION, readRoom, type KeptPage } from './room'

export const ROOMS_KEY = 'kirocrew:xr-rooms'
export const ROOMS_VERSION = 1
/** The most rooms: the cards are one row over the palette's four columns, four and the "+", or five. */
export const ROOMS_MOST = 5
/** The most characters of a name, and of the whole list (a list that is longer is not written). */
export const ROOM_NAME_MOST = 40
export const ROOMS_MOST_CHARS = 200_000
/** A removed room is offered back this long (ms). */
export const ROOM_BACK_MS = 6000

/** One room: `named` says that he gave the name (a proposed one follows the room's pages; his never changes). */
export interface NamedRoom { id: string; name: string; named: boolean; layout: string }
/** The list. `now`: the room that stands. `told`: he has seen that rooms are kept on this device. */
export interface Rooms { v: number; now: string; rooms: NamedRoom[]; told: boolean }

/** What of a room's record is its layout; everything else is the crew's state or the device's setup. */
const LAYOUT_FIELDS = ['pagesHidden', 'crew', 'places', 'pages'] as const
/** What never goes with a switch: the marks of Bots that stand where he put them end with their state. */
const DROPPED_FIELDS = ['put', 'room'] as const

const parse = (text: string | null | undefined): Record<string, unknown> | null => {
  if (!text || text.length > 64_000) return null
  try {
    const r = JSON.parse(text) as Record<string, unknown> | null
    return r && typeof r === 'object' && r.v === ROOM_VERSION ? r : null
  } catch { return null }
}

/** The layout of a room's record: only what a room holds. A record that cannot be read gives the empty room. */
export function layoutOf(record: string | null | undefined): string {
  const r = parse(record)
  const out: Record<string, unknown> = { v: ROOM_VERSION }
  for (const field of LAYOUT_FIELDS) if (r && r[field] !== undefined) out[field] = r[field]
  if (!Array.isArray(out.pages)) out.pages = []
  return JSON.stringify(out)
}

/**
 * The record that stands when he goes to a room: that room's layout, with the crew's state and the device's setup
 * of the record that stood (`standing`). Nothing of the room he leaves is in it but that state.
 */
export function standWith(layout: string, standing: string | null | undefined): string {
  const from = parse(standing) ?? { v: ROOM_VERSION }
  const room = parse(layout) ?? { v: ROOM_VERSION, pages: [] }
  const out: Record<string, unknown> = {}
  for (const [field, value] of Object.entries(from)) {
    if ((LAYOUT_FIELDS as readonly string[]).includes(field) || (DROPPED_FIELDS as readonly string[]).includes(field)) continue
    out[field] = value
  }
  for (const field of LAYOUT_FIELDS) if (room[field] !== undefined) out[field] = room[field]
  if (!Array.isArray(out.pages)) out.pages = []
  return JSON.stringify(out)
}

const okId = (v: unknown): v is string => typeof v === 'string' && /^r[0-9]{1,6}$/.test(v)
/** The whole signs of a text: a flag or a joined emoji is one (as Home's names are cut, `homeNames.ts`). */
export const signsOf = signs
export const cleanName = (name: string): string => signsOf(name.replace(/\s+/g, ' ').trim()).slice(0, ROOM_NAME_MOST).join('').trimEnd()

/** The list as a store holds it, or null: what is no list is dropped whole, a room that is none is dropped alone. */
export function readRooms(text: string | null | undefined): Rooms | null {
  if (!text || text.length > ROOMS_MOST_CHARS) return null
  let raw: unknown
  try { raw = JSON.parse(text) } catch { return null }
  if (!raw || typeof raw !== 'object') return null
  const r = raw as { v?: unknown; now?: unknown; rooms?: unknown; told?: unknown }
  if (r.v !== ROOMS_VERSION || !Array.isArray(r.rooms)) return null
  const rooms: NamedRoom[] = []
  for (const item of r.rooms as unknown[]) {
    const x = item as { id?: unknown; name?: unknown; named?: unknown; layout?: unknown } | null
    if (!x || !okId(x.id) || typeof x.name !== 'string' || typeof x.layout !== 'string' || rooms.some(k => k.id === x.id)) continue
    const name = cleanName(x.name)
    if (name && rooms.length < ROOMS_MOST) rooms.push({ id: x.id, name, named: x.named === true, layout: layoutOf(x.layout) })
  }
  if (!rooms.length) return null
  return { v: ROOMS_VERSION, now: rooms.some(k => k.id === r.now) ? (r.now as string) : rooms[0].id, rooms, told: r.told === true }
}

/** The list to write, or null when it is too long to keep (then nothing is written, and the list stays as it was). */
export function writeRooms(rooms: Rooms): string | null {
  const text = JSON.stringify(rooms)
  return text.length <= ROOMS_MOST_CHARS ? text : null
}

const nextId = (rooms: readonly NamedRoom[]): string => `r${Math.max(0, ...rooms.map(k => Number(k.id.slice(1)) || 0)) + 1}`

/**
 * A name for a room from what stands in it: its largest page's title, and "+n" for the others; with no page,
 * "Room n". `titleOf` says a page's title from its path (the dashboard's own label, a Bot's name for its page).
 */
export function proposeName(layout: string, n: number, titleOf: (path: string) => string, words: { room: (n: number) => string; more: (name: string, count: number) => string }): string {
  const pages = readRoom(layout)
  if (!pages.length) return cleanName(words.room(n))
  const largest = [...pages].sort((a, b) => b.css[0] * b.css[1] - a.css[0] * a.css[1])[0]
  const title = titleOf(largest.path) || words.room(n)
  return cleanName(pages.length > 1 ? words.more(title, pages.length - 1) : title)
}

/** The list for a browser that kept none: one room, the one that stands. */
export function startRooms(kept: Rooms | null, standing: string | null | undefined, name: (layout: string, n: number) => string): Rooms {
  if (kept) return kept
  const layout = layoutOf(standing)
  return { v: ROOMS_VERSION, now: 'r1', rooms: [{ id: 'r1', name: name(layout, 1), named: false, layout }], told: false }
}

/**
 * The room that stands, written into its place in the list (`standing`: its record now). A name that he did not
 * give follows the room's pages.
 */
export function keepStanding(rooms: Rooms, standing: string | null | undefined, name: (layout: string, n: number) => string): Rooms {
  const layout = layoutOf(standing)
  const others = rooms.rooms.filter(k => k.id !== rooms.now).map(k => k.name)
  return { ...rooms, rooms: rooms.rooms.map((k, i) => (k.id === rooms.now ? { ...k, layout, name: k.named ? k.name : uniqueName(name(layout, i + 1), others) } : k)) }
}

/** A proposed name that no other card has: the name, or the name with the next free number ("Sessions (2)"). */
export function uniqueName(name: string, taken: readonly string[]): string {
  if (!taken.includes(name)) return name
  for (let n = 2; ; n += 1) {
    const tail = ` (${n})`
    const next = `${signsOf(name).slice(0, ROOM_NAME_MOST - tail.length).join('').trimEnd()}${tail}`
    if (!taken.includes(next)) return next
  }
}

/**
 * Which room the standing record is: it says so itself (`room`), so a tab that dies between the two writes of a
 * switch opens the room that the record holds, and the list's "now" follows it. A build that knows no rooms writes
 * the record without the field: the record is then the room that the list says stands.
 */
export function tagRoom(record: string, id: string): string {
  const r = parse(record)
  return r ? JSON.stringify({ ...r, room: id }) : record
}
export function roomOfRecord(record: string | null | undefined): string | null {
  const id = parse(record)?.room
  return okId(id) ? id : null
}

/** Whether a standing record can be read as a room of this version. */
export const readable = (record: string | null | undefined): boolean => parse(record) !== null

/**
 * The list when the workspace opens: the kept one (or one room), standing where the record says, with the record in
 * it. A record that cannot be read (none, too long, another version), or that names a room which the list no longer
 * holds, is never written over the layout that the list holds for the room that stands: that layout is the room, and `standingFor` gives the record to stand from it.
 */
export function openRooms(kept: Rooms | null, standing: string | null | undefined, name: (layout: string, n: number) => string): Rooms {
  const rooms = startRooms(kept, standing, name)
  const here = roomOfRecord(standing)
  const at = here && rooms.rooms.some(k => k.id === here) ? { ...rooms, now: here } : rooms
  return kept && !standsFor(kept, standing) ? at : keepStanding(at, standing, name)
}

/**
 * Whether the record is a room of the list: it can be read, and the room it names (if it names one) is in the list.
 * A record that names a room which another tab removed is no room of the list: its layout is the removed room's,
 * and written into the room that stands it would put that room's pages under another room's name.
 */
const standsFor = (kept: Rooms, standing: string | null | undefined): boolean => {
  const here = roomOfRecord(standing)
  return readable(standing) && (!here || kept.rooms.some(k => k.id === here))
}

/**
 * The record that must stand when the workspace opens, or null when the one that stands is right. With a list kept:
 * a record that cannot be read, or that names a room which is gone, is made from the list's layout of the room that
 * stands; a record with no tag (a
 * build that knows no rooms wrote it) gets the tag of that room at once, so that no untagged record stands beside
 * a list through a switch.
 */
export function standingFor(kept: Rooms | null, standing: string | null | undefined): string | null {
  if (!kept) return null
  const room = kept.rooms.find(k => k.id === kept.now)
  if (!room) return null
  // The crew's state of a record that reads goes on (its tag names a room that is gone; its state is the crew's).
  if (!standsFor(kept, standing)) return tagRoom(standWith(room.layout, readable(standing) ? standing : null), room.id)
  return roomOfRecord(standing) ? null : tagRoom(standing as string, room.id)
}

/** "+": the room as it stands becomes a new room too, and he is in the new one. Null at the most rooms. */
export function addRoom(rooms: Rooms, standing: string | null | undefined, name: (layout: string, n: number) => string): Rooms | null {
  if (rooms.rooms.length >= ROOMS_MOST) return null
  const kept = keepStanding(rooms, standing, name)
  const layout = layoutOf(standing)
  const id = nextId(kept.rooms)
  return { ...kept, now: id, rooms: [...kept.rooms, { id, name: uniqueName(name(layout, kept.rooms.length + 1), kept.rooms.map(k => k.name)), named: false, layout }] }
}

/** A layout with no page, nothing put away and no Bot put anywhere: a new room's (D93 1). */
export const EMPTY_LAYOUT = JSON.stringify({ v: ROOM_VERSION, pages: [] })

/**
 * "New room" (D93 1, 2): a room with no page joins the list, named "Room n" with the first n that no room has. That
 * name is its own (`named`): it never follows pages, and is never another room's. The room that stands is written
 * into its place first and still stands: the caller then goes to the new one (`switchRoom`). Null at the most rooms.
 */
export function addEmptyRoom(rooms: Rooms, standing: string | null | undefined, name: (layout: string, n: number) => string, roomWord: (n: number) => string): { rooms: Rooms; id: string } | null {
  if (rooms.rooms.length >= ROOMS_MOST) return null
  const kept = keepStanding(rooms, standing, name)
  const taken = new Set(kept.rooms.map(k => k.name))
  let n = kept.rooms.length + 1
  while (taken.has(cleanName(roomWord(n)))) n += 1
  const id = nextId(kept.rooms)
  return { rooms: { ...kept, rooms: [...kept.rooms, { id, name: cleanName(roomWord(n)), named: true, layout: EMPTY_LAYOUT }] }, id }
}

/**
 * "Copy this room" (D93 4): a room with the standing room's layout joins the list, named "<name> copy" (`copyWord`),
 * with a number after it when a room has that name. The caller goes to it. Null at the most rooms.
 */
export function copyRoom(rooms: Rooms, standing: string | null | undefined, name: (layout: string, n: number) => string, copyWord: (name: string) => string): { rooms: Rooms; id: string } | null {
  if (rooms.rooms.length >= ROOMS_MOST) return null
  const kept = keepStanding(rooms, standing, name)
  const from = kept.rooms.find(k => k.id === kept.now)
  if (!from) return null
  const id = nextId(kept.rooms)
  // A second copy keeps the word too (N7): the name is cut, never the word or its number.
  const taken = kept.rooms.map(k => k.name)
  // A copy of a copy is "name copy (2)", never "name copy copy" (D109 5): the word is put on the name under it.
  // A long name is cut once, to what leaves room for the word and a number: every copy of it then has the same name
  // under the word, so the next copy of any of them finds the others ("… copy", "… copy (2)", "… copy (3)").
  const room = ROOM_NAME_MOST - signsOf(`${copyWord('')} (9)`).length
  const base = signsOf(copyParts(from.name, copyWord)?.base ?? from.name).slice(0, Math.max(1, room)).join('').trimEnd()
  let copied = copyName(base, copyWord)
  for (let n = 2; taken.includes(copied); n += 1) copied = copyName(base, plain => `${copyWord(plain)} (${n})`)
  return { rooms: { ...kept, rooms: [...kept.rooms, { id, name: copied, named: true, layout: from.layout }] }, id }
}

/**
 * A copy's name in its parts, or null when `name` is no copy's name: the name under it (`base`), and what stands
 * after it (`tail`: the word for "copy" where the language puts it last, and the number). The word is found as the
 * catalog writes it: `copyWord` is asked for a name of one private sign, and what it puts before and after that sign
 * is the word. A room that he himself named "Hard copy" reads as a copy of "Hard": its copy is "Hard copy (2)".
 */
export function copyParts(name: string, copyWord: (name: string) => string): { base: string; tail: string } | null {
  const [before = '', after = ''] = copyWord('\uE000').split('\uE000')
  const number = / \(\d+\)$/.exec(name)?.[0] ?? ''
  const plain = number ? name.slice(0, -number.length) : name
  const [head, end] = [before.trimStart(), after.trimEnd()]
  if (!(head || end)) return null
  // The word alone ("copy") is a copy of a room with no name left.
  if (plain === `${head}${end}`.trim()) return { base: '', tail: `${end}${number}` }
  // "Hardcopy" is no copy of "Hard": the catalog's own space between the name and the word is part of `head` and `end`.
  if (plain.length < head.length + end.length || !plain.startsWith(head) || !plain.endsWith(end)) return null
  return { base: plain.slice(head.length, plain.length - end.length).trim(), tail: `${end}${number}` }
}

/** A copy's name: the room's name is cut, by whole signs, so that the word for "copy" stays whole (D98 6). */
export function copyName(name: string, copyWord: (name: string) => string): string {
  const room = ROOM_NAME_MOST - signsOf(copyWord('')).length
  return cleanName(copyWord(signsOf(name).slice(0, Math.max(1, room)).join('').trimEnd()))
}

/** Whether another room has this name already (as `cleanName` keeps it). */
export const nameTaken = (rooms: Rooms, id: string, name: string): boolean => { const clean = cleanName(name); return !!clean && rooms.rooms.some(k => k.id !== id && k.name === clean) }

/**
 * He goes to another room. The room he leaves is written into the list FIRST (`rooms`), and only then is the other
 * room's record the one that stands (`stand`): written in that order, a tab that dies between the two writes has
 * lost nothing. Null: no such room, or he is in it.
 */
export function switchRoom(rooms: Rooms, to: string, standing: string | null | undefined, name: (layout: string, n: number) => string): { rooms: Rooms; stand: string } | null {
  const target = rooms.rooms.find(k => k.id === to)
  if (!target || to === rooms.now) return null
  const kept = keepStanding(rooms, standing, name)
  return { rooms: { ...kept, now: to }, stand: tagRoom(standWith(target.layout, standing), to) }
}

/** A room he removed, and where it was in the row: what "bring it back" puts back. */
export interface RemovedRoom { room: NamedRoom; at: number }

/** He throws a room's card away. Null: he is in that room, it is the last one, or there is none such. */
export function removeRoom(rooms: Rooms, id: string): { rooms: Rooms; removed: RemovedRoom } | null {
  const at = rooms.rooms.findIndex(k => k.id === id)
  if (at < 0 || id === rooms.now || rooms.rooms.length <= 1) return null
  return { rooms: { ...rooms, rooms: rooms.rooms.filter(k => k.id !== id) }, removed: { room: rooms.rooms[at], at } }
}

/** The removed room comes back where it was. Null when the row is full again or a room has its id. */
export function bringBack(rooms: Rooms, removed: RemovedRoom): Rooms | null {
  if (rooms.rooms.length >= ROOMS_MOST || rooms.rooms.some(k => k.id === removed.room.id)) return null
  const list = [...rooms.rooms]
  list.splice(Math.min(removed.at, list.length), 0, removed.room)
  return { ...rooms, rooms: list }
}

/** He names a room. An empty name changes nothing, nor does a name that another room has (D93: never two rooms with one name). */
export function renameRoom(rooms: Rooms, id: string, name: string): Rooms {
  const clean = cleanName(name)
  if (!clean || nameTaken(rooms, id, clean)) return rooms
  return { ...rooms, rooms: rooms.rooms.map(k => (k.id === id ? { ...k, name: clean, named: true } : k)) }
}

/** One open page as a switch sees it. */
export interface OpenPage { id: string; path: string }
/** What a switch does with the pages: these stay and go to a kept place, these go, these come. */
export interface SwitchPlan { stay: { id: string; to: KeptPage }[]; go: string[]; come: KeptPage[] }

/** The page a path is of: its first part (`/chat` of `/chat/a-session`, `/members` of `/members?member=Juno`). */
const pageOf = (path: string): string => path.split(/[/?]/, 2).join('/')

/**
 * Which open pages a switch keeps. A page of the same path in both rooms stays live and moves to its place in the
 * new room (each kept page takes one open page, in order). Then, only for a kept page that is the bare page (`/chat`,
 * `/members`): an open page that moved on within that page since (the chat went on to a session) stays for it. A
 * kept page that names a session, a Bot or a tab is never stood in for by another one of its kind: it comes fresh,
 * so no room ever shows another room's page in its place. The other open pages go; the other kept pages come.
 */
export function switchPlan(open: readonly OpenPage[], kept: readonly KeptPage[]): SwitchPlan {
  const taken = new Set<string>()
  const found = new Map<number, string>()
  for (const same of [(o: OpenPage, k: KeptPage) => o.path === k.path, (o: OpenPage, k: KeptPage) => k.path === pageOf(k.path) && pageOf(o.path) === k.path]) {
    kept.forEach((page, i) => {
      if (found.has(i)) return
      const o = open.find(x => !taken.has(x.id) && same(x, page))
      if (o) { taken.add(o.id); found.set(i, o.id) }
    })
  }
  const stay = kept.flatMap((page, i) => (found.has(i) ? [{ id: found.get(i)!, to: page }] : []))
  return { stay, go: open.filter(o => !taken.has(o.id)).map(o => o.id), come: kept.filter((_p, i) => !found.has(i)) }
}

/** A page on a card's plan: its middle and its size as shares of the plan (x to the right, y up, each 0 to 1). */
export interface PlanRect { x: number; y: number; w: number; h: number }
/** The part of the space round him that a card's plan shows: so far to each side, so far down and up (degrees). */
export const PLAN_VIEW = { yawDeg: 75, downDeg: 30, upDeg: 40 } as const

/** A room's plan: each page where it stands round him, as he would see it from above his own eyes. */
export function planOf(pages: readonly Pick<KeptPage, 'at' | 'css'>[]): PlanRect[] {
  const deg = (m: number, d: number) => (2 * Math.atan2(m / 2, d) * 180) / Math.PI
  const spanY = PLAN_VIEW.downDeg + PLAN_VIEW.upDeg
  return pages.map(p => {
    const d = Math.max(0.3, p.at.distanceM || PAGES.distanceM)
    const w = deg(p.css[0] * M_PER_PX, d) / (2 * PLAN_VIEW.yawDeg)
    const h = deg(p.css[1] * M_PER_PX, d) / spanY
    const x = 0.5 + p.at.yawDeg / (2 * PLAN_VIEW.yawDeg)
    const y = (p.at.pitchDeg + PLAN_VIEW.downDeg) / spanY
    return { x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)), w: Math.min(1, w), h: Math.min(1, h) }
  })
}

/**
 * Whether a held card was thrown away when he let it go. Told as a board's throw is (`board.ts`, `releaseKind`): far
 * AND still fast at the release. A hand that drifts while he says the card's name, or a slow carry however far, is
 * not a throw: the card goes back and takes the name he said. A press that ends as a cancel is never a throw.
 */
export const cardThrown = (moved: Moved): boolean => releaseKind({ ...moved, onBot: false }) === 'throw'
