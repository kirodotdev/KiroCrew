/**
 * NAMED ROOMS (D37) as state: the list of rooms he keeps (`rooms.ts`, the second key), and what a press on a card
 * does to the room that stands. The rules are `rooms.ts`; the cards are `RoomCards.tsx`; the workspace hands over
 * only what it owns (its record, its pages, its places).
 *
 * A switch writes the list first, with the room he leaves in it, then the record of the room he goes to (tagged
 * with its id, `tagRoom`), and only then changes what stands: a page that both rooms hold stays live and glides to
 * its new place, the others go, the new room's own come. Each step reads the list from the store again, so a second
 * tab's rename is not written over.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Vector3 } from 'three'
import { activeLocale } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { LANG_KEY, listen, startLang } from './chatSpeech'
import { PAGE_MENU, menuLabel } from './pageMenu'
import { ROOM_KEY, pagesHiddenAtStart, readCrew, readPlaces, readRoom, type KeptPage } from './room'
import {
  ROOMS_KEY, ROOM_BACK_MS, ROOM_NAME_MOST, ROOMS_MOST, signsOf, addEmptyRoom, addRoom, bringBack, cleanName, copyRoom, nameTaken, keepStanding, openRooms, proposeName, readRooms, removeRoom, renameRoom, standingFor, switchPlan, switchRoom, tagRoom, writeRooms,
  type OpenPage, type RemovedRoom, type Rooms,
} from './rooms'
import { CHAT_SESSION, MEMBER_PAGE } from './streamPath'
import type { KeptCrew } from './crewRow'
import type { KeptPlaces } from './places'
import type { PageSource } from './source'

/** How long a page that stays glides to its place in the room he goes to (ms). */
export const ROOM_GLIDE_MS = 450
/** A page that glides passes this far behind the page he looks at (m). */
const BEHIND_M = 0.3

/**
 * How far from the head each page that glides keeps on its way, by id: behind the page of the new room that stands
 * nearest the middle of his look (so far to the right, degrees), which stays on top until the others have landed.
 * That page itself glides straight.
 */
export function glideBehind(pages: readonly { id: string; at: Vector3 }[], head: Vector3, lookYawDeg: number): (id: string) => number {
  const off = (p: { at: Vector3 }) => Math.abs((((Math.atan2(p.at.x - head.x, -(p.at.z - head.z)) * 180) / Math.PI - lookYawDeg + 540) % 360) - 180)
  const front = [...pages].sort((a, b) => off(a) - off(b))[0]
  return id => (!front || id === front.id ? 0 : front.at.distanceTo(head) + BEHIND_M)
}

/** How long the room he leaves stands at most while the new room's pages get their first picture (ms). */
export const ROOM_READY_MS = 2500
/** How long a line for him stands over the cards (ms). */
export const ROOM_SAID_MS = 4000

/**
 * How long the hold waits for a new page after the gateway says it is live (ms). The new room's pages are not in
 * the room while the old one stands, so no panel draws them and `pictureAt` stays `null` for each: this wait is
 * what every new streamed page gets. It is the page's own time to fill: "live" is the page's load event, its first
 * pictures show its side bar and top bar with an empty body, then its frame with grey boxes, and its words come
 * last (measured on the build host: the Bots page is whole about 1.4 s after it is live). Nothing in the tab says
 * when a page is whole: "a picture has reached the tab" and "no new picture for 0.6 s" both let the old room go on
 * an empty page (tried, and read from pixels: row `rooms-look fresh`). So this is a time, and a page slower than it
 * stands for a moment with its grey boxes.
 */
const PICTURE_WAIT_MS = 1200

/** How long the Bots get at least to begin to change spots before the old room may go (ms). */
const BOTS_BEGIN_MS = 150

/**
 * Resolves when every Bot has its whole size again (none shrinks away from its old spot or grows at its new one), or
 * after `most`: the room he leaves stands until the Bots of the room he goes to stand. `grows` says each Bot's share
 * of its size now; with no Bot (the row, or a crew that is not drawn yet) it resolves at once.
 */
export function botsStand(grows: () => readonly number[], most = ROOM_READY_MS): Promise<void> {
  const t0 = performance.now()
  return new Promise(done => {
    const look = () => {
      const ran = performance.now() - t0
      const list = grows()
      if (!list.length || ran >= most || (ran >= BOTS_BEGIN_MS && list.every(g => g >= 1))) { done(); return }
      setTimeout(look, 50)
    }
    look()
  })
}

/** Resolves when each source shows its page (or is refused), or after `most`. */
export function roomReady(sources: readonly PageSource[], most = ROOM_READY_MS): Promise<void> {
  const t0 = performance.now()
  const liveAt = new Map<PageSource, number>()
  // There with a picture, there a little while, or never to have one (refused, signed out).
  const ready = (src: PageSource, now: number): boolean => {
    const st = src.status?.() ?? 'live'
    if (st === 'loading') return false
    if (st !== 'live' || !src.pictureAt || src.pictureAt() !== null) return true
    if (!liveAt.has(src)) liveAt.set(src, now)
    return now - (liveAt.get(src) ?? now) >= PICTURE_WAIT_MS
  }
  return new Promise(done => {
    const look = () => {
      const now = performance.now()
      if (now - t0 >= most || sources.map(src => ready(src, now)).every(Boolean)) { done(); return }
      setTimeout(look, 50)
    }
    look()
  })
}

/** A page's name on a card: the palette's own label, a Bot's name for its page, the chat's for a chat. */
export function pageTitle(path: string): string {
  if (path.startsWith(MEMBER_PAGE)) return decodeURIComponent(path.slice(MEMBER_PAGE.length).split('&')[0])
  if (path.startsWith(CHAT_SESSION)) return menuLabel(PAGE_MENU[0])
  const row = PAGE_MENU.find(p => path === p.path || path.startsWith(`${p.path}/`) || path.startsWith(`${p.path}?`))
  return row ? menuLabel(row) : path.replace(/^\/+/, '').split(/[/?]/)[0]
}

export const nameRoom = (layout: string, n: number): string =>
  proposeName(layout, n, pageTitle, { room: k => i18nT('xr.room_n', { n: k }), more: (name, more) => i18nT('xr.room_more', { name, more }) })

const read = (key: string): string | null => { try { return window.localStorage.getItem(key) } catch { return null } }
/** False: nothing was written (no text to write, no store, or a store that is full). */
const write = (key: string, text: string | null): boolean => { if (text === null) return false; try { window.localStorage.setItem(key, text); return true } catch { return false } }

/**
 * Before the workspace reads the standing record: with a list kept, a record that cannot be read is made again from
 * the list's layout of the room that stands, and an untagged one is tagged (`rooms.ts`, `standingFor`).
 */
export function mendStanding(): void {
  const stand = standingFor(readRooms(read(ROOMS_KEY)), read(ROOM_KEY))
  if (stand !== null) write(ROOM_KEY, stand)
}

export interface RoomsHost {
  /** The standing record as the workspace would write it now (`writeRoom`). */
  record: () => string
  /** The record that the workspace wrote last is this one: it does not write it again. */
  wrote: (text: string) => void
  /** The open pages that the record keeps (not an ask's panel). */
  open: () => OpenPage[]
  /** Change what stands: these pages go, these stay and go to a place, these come. */
  /**
   * `away`: the room he goes to keeps its pages put away. `ahead` is called once the workspace knows where the new
   * room's pages will stand: it tells the crew where that room keeps its Bots, while the old room still stands.
   * `rooms`: the room he leaves and the room he goes to (the pages that go may stay open for the first, and the
   * second's may be open already: `useWarmRooms.ts`).
   */
  place: (go: readonly string[], stay: readonly { id: string; to: KeptPage }[], come: readonly KeptPage[], away: boolean, ahead: () => void, rooms: { from: string; to: string }) => Promise<void>
  setCrew: (crew: KeptCrew) => void
  setPlaces: (places: KeptPlaces) => void
  /** Put the pages away, or show them, as the room he goes to keeps it (`useHome`, `setAway`). */
  setAway: (on: boolean) => void
  /** One line for him (the workspace's note). */
  say?: (words: string) => void
  /** The new chat card's speech stands aside while a held card listens, as for a hold on a Bot (`useChatCard.micAway`). */
  micAway?: (away: boolean) => void
}

export function useRooms(host: RoomsHost) {
  const hostNow = useRef(host)
  hostNow.current = host
  const [rooms, setRooms] = useState<Rooms>(() => {
    const opened = openRooms(readRooms(read(ROOMS_KEY)), read(ROOM_KEY), nameRoom)
    write(ROOMS_KEY, writeRooms(opened))
    return opened
  })
  /**
   * The list as the store holds it now (another tab may have named, added or removed a room), with THIS tab's room
   * as the one that stands: two tabs may stand in two rooms, and each keeps its own.
   */
  const fresh = useCallback((): Rooms => ({ ...(readRooms(read(ROOMS_KEY)) ?? rooms), now: rooms.now }), [rooms])
  /** False: the list was not written (too long, or the store is full), and what he sees stays as the store has it. */
  /**
   * The list is written, and stands. A list that could not be written (the store is full) stops the act that made it,
   * and the line says so at every such act (D98 5, M67): a switch, a new room, a copy, a name, a removal, a way back.
   * `quiet`: a write that is no act of his (the mark that he was told where rooms are kept).
   */
  const put = useCallback((next: Rooms, quiet = false): boolean => {
    const text = writeRooms(next)
    if (!write(ROOMS_KEY, text)) {
      // A list too long for our own limit says so in its own words (T7), not that the browser's store is full.
      if (!quiet) { const words = i18nT(text === null ? 'xr.rooms_too_long' : 'xr.room_store_full'); setSaidNow.current(words); hostNow.current.say?.(words) }
      return false
    }
    setRooms(next)
    return true
  }, [])
  /**
   * The standing record is written, checked as the list is (M86). When it cannot be (the store is full), the list
   * goes back to `before`, the line says so, and the act that needed it stops: a list that names a room whose record
   * was never written would open the wrong pages after a reload.
   */
  const stand = useCallback((text: string, before: Rooms): boolean => {
    if (write(ROOM_KEY, text)) return true
    write(ROOMS_KEY, writeRooms(before))
    setRooms(before)
    const words = i18nT('xr.room_store_full'); setSaidNow.current(words); hostNow.current.say?.(words)
    return false
  }, [])
  // Another tab changed the list: this tab shows it, and stays in its own room.
  useEffect(() => {
    const changed = (e: StorageEvent) => { if (e.key !== ROOMS_KEY) return; const list = readRooms(e.newValue); if (list) setRooms(was => ({ ...list, now: was.now })) }
    window.addEventListener('storage', changed)
    return () => window.removeEventListener('storage', changed)
  }, [])
  /** The record was written: the room that stands has it in the list too. */
  // The layout's own save is no act of his: when it cannot be written the line says so once, not at each save (M86),
  // and again only after a save went through.
  const keepSaid = useRef(false)
  const keep = useCallback((text: string) => {
    const next = keepStanding(fresh(), text, nameRoom)
    if (writeRooms(next) !== read(ROOMS_KEY)) keepSaid.current = !put(next, keepSaid.current)
  }, [fresh, put])
  const switching = useRef(false)
  /**
   * The record to write, tagged with the room that stands. None while a switch is under way (what stands is neither
   * room), and none when another tab removed the room this tab stands in (its record would stand for a room that
   * is not there).
   */
  const tag = useCallback((text: string): string | null => (switching.current || !fresh().rooms.some(k => k.id === rooms.now) ? null : tagRoom(text, rooms.now)), [fresh, rooms.now])
  // The pages are put away (or shown) once the new room's pages stand, in the render after: those that came too.
  const awayAfter = useRef<boolean | null>(null)
  const [stood, setStood] = useState(0)
  const go = useCallback((id: string) => {
    const h = hostNow.current
    if (switching.current) return
    const before = fresh()
    const got = switchRoom(before, id, tagRoom(h.record(), rooms.now), nameRoom)
    if (!got) return
    // The room he leaves is in the list before the other room's record stands. A list that could not be written
    // stops the switch: the room he leaves would be lost. So does a record that could not be written.
    if (!put(got.rooms) || !stand(got.stand, before)) return
    h.wrote(got.stand)
    // The room's pages stand where it keeps them: a room of the list is this build's own, so nothing is stood anew
    // (`settleRoom` is for a record of an older build, once, when the workspace opens).
    const plan = switchPlan(h.open(), readRoom(got.stand))
    switching.current = true
    const away = pagesHiddenAtStart(got.stand)
    // Where the room keeps its Bots is told before its pages stand: they are at their places when the old room goes.
    const ahead = () => { h.setCrew(readCrew(got.stand)); h.setPlaces(readPlaces(got.stand)) }
    void h.place(plan.go, plan.stay, plan.come, away, ahead, { from: rooms.now, to: id }).then(() => {
      awayAfter.current = away
      setStood(n => n + 1)
    })
  }, [fresh, put, stand, rooms.now])
  useEffect(() => {
    const away = awayAfter.current
    if (away === null) return
    awayAfter.current = null
    // The new room's pages stand in this render: from here on the record is the new room's again.
    switching.current = false
    hostNow.current.setAway(away)
  }, [stood])
  const goNow = useRef(go)
  goNow.current = go
  const add = useCallback(() => {
    const h = hostNow.current
    // While a switch holds, the list says that the new room stands and the pages are still the old room's: a room
    // added now would take the old pages into the room he goes to.
    // While a removed room is offered back, its place in the row is kept for it.
    if (switching.current || goneNow.current) return
    const before = fresh()
    const next = addRoom(before, h.record(), nameRoom)
    if (!next || !put(next)) return
    // The record says at once that it is the new room: a reload opens the room he is in.
    const tagged = tagRoom(h.record(), next.now)
    if (!stand(tagged, before)) return
    h.wrote(tagged)
  }, [fresh, put, stand])
  const rename = useCallback((id: string, name: string) => put(renameRoom(fresh(), id, name)), [fresh, put])
  /**
   * The rooms whose name he is asked for, each with the letters typed so far (D93 2): a name line stands in the lens
   * while the room that stands is one of them. The letters stay with their room across a switch. Kept in this tab's
   * memory only. `pages`: how many pages stood when it was asked; `fresh`: a room just made ("New room"), whose
   * asking ends when he opens a page; `yawDeg`: where he faced.
   */
  const setSaidNow = useRef<(words: string | null) => void>(() => undefined)
  const [asking, setAsking] = useState<Readonly<Record<string, { text: string; pages: number; fresh: boolean; yawDeg: number }>>>({})
  const askingNow = useRef(asking)
  askingNow.current = asking
  const endAsk = useCallback((id: string) => setAsking(was => { if (!(id in was)) return was; const next = { ...was }; delete next[id]; return next }), [])
  const ask = useCallback((id: string, fresh2 = false, yawDeg = 0) => setAsking(was => (id in was ? was : { ...was, [id]: { text: '', pages: fresh2 ? 0 : hostNow.current.open().length, fresh: fresh2, yawDeg } })), [])
  /** The letters of the name as he types or says them: at most a name's length, whole signs. */
  const typeName = useCallback((id: string, text: string) => setAsking(was => (id in was ? { ...was, [id]: { ...was[id], text: signsOf(text.replace(/\s+/g, ' ').trimStart()).slice(0, ROOM_NAME_MOST).join('') } } : was)), [])
  /** Enter: the name is kept. An empty one keeps the room's own name; another room's name is refused, and one line says so. */
  /** One line that says why nothing happened: on the cards' line, and on the name line while it stands (D98 5). */
  const tell = useCallback((words: string) => { setSaidNow.current(words); hostNow.current.say?.(words) }, [])
  const keepName = useCallback((id: string) => {
    const text = cleanName(askingNow.current[id]?.text ?? '')
    if (text && nameTaken(fresh(), id, text)) { tell(i18nT('xr.room_name_taken', { name: text })); return }
    // A name that cannot be kept (the store is full) is not lost in silence: the line stays with his letters and says why (D98 5).
    if (text && !rename(id, text)) return
    endAsk(id)
  }, [fresh, rename, endAsk, tell])
  /** "New room": an empty room joins the list, he goes to it, and it asks its name once. */
  const addEmpty = useCallback((yawDeg = 0) => {
    const h = hostNow.current
    if (switching.current || goneNow.current) return
    const got = addEmptyRoom(fresh(), tagRoom(h.record(), rooms.now), nameRoom, n => i18nT('xr.room_n', { n }))
    if (!got) { tell(i18nT('xr.rooms_most', { count: ROOMS_MOST })); return }
    if (!put(got.rooms)) return
    ask(got.id, true, yawDeg)
    goNow.current(got.id)
  }, [fresh, put, rooms.now, ask, tell])
  /** "Copy this room": a room with the same pages, named "<name> copy"; he goes to it. */
  const copy = useCallback(() => {
    const h = hostNow.current
    if (switching.current || goneNow.current) return
    const got = copyRoom(fresh(), tagRoom(h.record(), rooms.now), nameRoom, name => i18nT('xr.room_copy_name', { name }))
    if (!got) { tell(i18nT('xr.rooms_most', { count: ROOMS_MOST })); return }
    if (!put(got.rooms)) return
    endAsk(rooms.now)
    goNow.current(got.id)
  }, [fresh, put, rooms.now, endAsk, tell])
  const [gone, setGone] = useState<RemovedRoom | null>(null)
  const goneNow = useRef(gone)
  goneNow.current = gone
  useEffect(() => {
    if (!gone) return
    const timer = setTimeout(() => setGone(null), ROOM_BACK_MS)
    return () => clearTimeout(timer)
  }, [gone])
  const remove = useCallback((id: string) => { const got = removeRoom(fresh(), id); if (got && put(got.rooms)) setGone(got.removed) }, [fresh, put])
  // "Back" on a store that cannot be written keeps the offer (M86): the room is not lost with the line that says why.
  const back = useCallback(() => { if (!gone) return; const next = bringBack(fresh(), gone); if (next && !put(next)) return; setGone(null) }, [fresh, gone, put])
  const told = useCallback(() => { const now = fresh(); if (!now.told) put({ ...now, told: true }, true) }, [fresh, put])
  // A card in his hand: the card's speech listens (in the card's language, `chatSpeech.ts`), and what he says is its
  // new name when he lets it go. Thrown away, the room is removed, and one line offers it back for a while.
  const [naming, setNaming] = useState<{ id: string; words: string } | null>(null)
  // One line over the cards for a little while (in a session the cards' own line is the one he sees).
  const [said, setSaid] = useState<string | null>(null)
  setSaidNow.current = setSaid
  useEffect(() => {
    if (!said) return
    const timer = setTimeout(() => setSaid(null), ROOM_SAID_MS)
    return () => clearTimeout(timer)
  }, [said])
  const namingNow = useRef(naming)
  namingNow.current = naming
  const ear = useRef<{ stop: () => void } | null>(null)
  const hold = useCallback((id: string) => {
    ear.current?.stop()
    hostNow.current.micAway?.(true)
    setNaming({ id, words: '' })
    let lang: string | null = null
    try { lang = window.localStorage.getItem(LANG_KEY) } catch { /* the dashboard's language */ }
    const mine = (words: string) => setNaming(n => (n && n.id === id ? { id, words } : n))
    ear.current = listen(startLang(lang, activeLocale()), { started: () => undefined, heard: (final, interim) => mine(`${final} ${interim}`.trim()), none: () => undefined })
  }, [])
  const letGo = useCallback((id: string, thrown: boolean) => {
    ear.current?.stop()
    ear.current = null
    const said = namingNow.current?.id === id ? namingNow.current.words : ''
    setNaming(null)
    hostNow.current.micAway?.(false)
    // The card of the room he stands in cannot be thrown away: it goes back to its place, and one line says why.
    if (thrown && id === rooms.now) { const words = i18nT('xr.room_in_it'); setSaid(words); hostNow.current.say?.(words) }
    else if (thrown) remove(id)
    else if (said) rename(id, said)
  }, [remove, rename, rooms.now])
  useEffect(() => () => { if (ear.current) { ear.current.stop(); hostNow.current.micAway?.(false) } }, [])
  /** Whether a switch is under way: what stands is neither room. */
  const moving = useCallback(() => switching.current, [])
  /** The name line that stands now: the room that stands is asked its name. */
  const nameAsk = useMemo(() => { const a = asking[rooms.now]; return a ? { id: rooms.now, ...a } : null }, [asking, rooms.now])
  return useMemo(() => ({ rooms, keep, tag, go, add, addEmpty, copy, rename, remove, back, gone, told, naming, hold, letGo, said, moving, nameAsk, ask, typeName, keepName, endAsk }), [rooms, keep, tag, go, add, addEmpty, copy, rename, remove, back, gone, told, naming, hold, letGo, said, moving, nameAsk, ask, typeName, keepName, endAsk])
}
