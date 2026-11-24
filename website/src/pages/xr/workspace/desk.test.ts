import { describe, expect, it } from 'vitest'
import { DESK, defaultDesk, deskAsked, deskForSession, deskPoint, deskStartPitch, deskZone, keptDesk, seatOrder, seatPlaces, tilePlaces, swapSeat } from './desk'
import { inZone } from './layout'
import { readDesk, readRoom, writeRoom } from './room'

const EYE: [number, number, number] = [0, 1.6, 0]

describe('the desk', () => {
  it('stands at its default place with no hands: its top 0.40 m under the eyes, its near edge 0.35 m ahead, level', () => {
    const d = defaultDesk(EYE)
    expect(d.at[1]).toBeCloseTo(1.2, 9)
    expect(d.at[2]).toBeCloseTo(-0.35, 9)
    expect(deskPoint(d, 0, DESK.depthM)[2]).toBeCloseTo(-0.85, 9)
    // Turned to the right, the desk's "in" is to the right of straight ahead, and its "across" turns with it.
    const turned = defaultDesk(EYE, 90)
    expect(turned.at[0]).toBeCloseTo(0.35, 9)
    expect(deskPoint(turned, 0.1, 0)[2]).toBeCloseTo(0.1, 9)
  })

  it('stands up to 6 seats in one row in the band, 7 or 8 as a zigzag, never on the rest or on each other, and the tiles in one row at its left', () => {
    const d = defaultDesk(EYE)
    const clear = (seats: ReturnType<typeof seatPlaces>) => {
      for (const s of seats) {
        expect(s.v - DESK.padM / 2).toBeGreaterThanOrEqual(DESK.restM - 1e-9)
        expect(s.v + DESK.padM / 2).toBeLessThanOrEqual(DESK.restM + DESK.bandM + 1e-9)
        expect(s.u + DESK.padM / 2).toBeLessThanOrEqual(DESK.widthM / 2 + 1e-9)
        // Ahead of the eyes, from 0.55 to 0.70 m.
        expect(-deskPoint(d, s.u, s.v)[2]).toBeGreaterThanOrEqual(0.55)
        expect(-deskPoint(d, s.u, s.v)[2]).toBeLessThanOrEqual(0.7)
      }
      for (let i = 0; i < seats.length; i += 1) for (let j = i + 1; j < seats.length; j += 1) expect(Math.hypot(seats[i].u - seats[j].u, seats[i].v - seats[j].v)).toBeGreaterThan(DESK.padM)
    }
    for (let n = 1; n <= 6; n += 1) { const s = seatPlaces(n); expect(new Set(s.map(x => x.v)).size).toBe(1); clear(s) }
    for (const n of [7, 8]) {
      const s = seatPlaces(n)
      expect(s.filter(x => x.row === 0).map(x => x.v).every(v => v === DESK.zigFarV)).toBe(true)
      expect(s.filter(x => x.row === 1).map(x => x.v).every(v => v === DESK.zigNearV)).toBe(true)
      clear(s)
    }
    // Never more places than the desk has seats.
    expect(seatPlaces(18)).toHaveLength(DESK.seatsMost)
    const tiles = tilePlaces(DESK.tileSlots)
    expect(new Set(tiles.map(x => x.v)).size).toBe(1)
    expect(DESK.tileM).toBeGreaterThanOrEqual(0.08)
    for (const x of tiles) expect(x.v - DESK.tileM / 2).toBeGreaterThanOrEqual(DESK.restM)
    expect(Math.max(...tiles.map(x => x.u)) + DESK.tileM / 2).toBeLessThan(seatPlaces(1)[0].u - DESK.padM / 2)
  })

  it('keeps each Bot at its own seat: one that leaves leaves its seat empty, a new one takes the first empty seat, and past 8 the last place is the others\' pad', () => {
    const first = seatOrder([], ['a', 'b', 'c', 'd'])
    expect(first).toEqual({ seats: ['a', 'b', 'c', 'd'], more: [] })
    // b leaves: c and d keep their seats.
    expect(seatOrder(first.seats, ['a', 'c', 'd'])).toEqual({ seats: ['a', '', 'c', 'd'], more: [] })
    // e comes: it takes b's empty seat.
    expect(seatOrder(['a', '', 'c', 'd'], ['e', 'a', 'c', 'd'])).toEqual({ seats: ['a', 'e', 'c', 'd'], more: [] })
    // Nine: seven seated, the eighth place is the pad of the other two.
    const nine = seatOrder([], ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'])
    expect(nine.seats).toEqual(['a', 'b', 'c', 'd', 'e', 'f', 'g', ''])
    expect(nine.more).toEqual(['h', 'i'])
    // Eight fit: no pad.
    expect(seatOrder([], ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']).more).toEqual([])
  })

  it('lowers the preview\'s start view until the band is whole over the picture\'s lower edge', () => {
    const d = defaultDesk(EYE)
    // The start lens: 50 degrees tall, the frame shifted down by a third of its height.
    const lowerTan = Math.tan((25 * Math.PI) / 180) * (1 + 0.317)
    const pitch = deskStartPitch(d, EYE, lowerTan)
    expect(pitch).toBeLessThan(0)
    const s = Math.sin((pitch * Math.PI) / 180)
    const c = Math.cos((pitch * Math.PI) / 180)
    for (const u of [-DESK.widthM / 2, 0, DESK.widthM / 2]) {
      const p = deskPoint(d, u, DESK.restM)
      const [y, z] = [p[1] - EYE[1], p[2] - EYE[2]]
      expect((y * c + z * s) / (y * s - z * c)).toBeGreaterThanOrEqual(-lowerTan)
    }
    // A lens that sees the band already: level.
    expect(deskStartPitch(d, EYE, 10)).toBe(0)
  })

  it('keeps a page that opens clear of the Bots\' heads as the eye sees them', () => {
    const d = defaultDesk(EYE)
    const zone = deskZone(d, EYE)
    const head = deskPoint(d, 0, DESK.zigFarV, DESK.botM)
    const pitch = (Math.atan2(head[1] - EYE[1], -head[2]) * 180) / Math.PI
    expect(zone.pitchMaxDeg).toBeGreaterThan(pitch)
    // The first page at its default place is clear of it.
    expect(inZone({ at: { yawDeg: 0, pitchDeg: -4, distanceM: 1.75 }, widthM: 1.5, heightM: 1.05 }, zone)).toBe(false)
  })

  it('is kept as an optional field of the room\'s record at the same version, and an older reader keeps the pages', () => {
    const page = { path: '/chat', at: { yawDeg: 0, pitchDeg: -4, distanceM: 1.75 }, css: [1024, 720] as [number, number], byHand: false }
    const text = writeRoom([page], false, defaultDesk(EYE, 0, true))
    expect(JSON.parse(text).v).toBe(1)
    expect(readRoom(text)).toEqual([page])
    const kept = readDesk(text)
    expect(kept?.from).toBe('kept')
    expect(kept?.confirmed).toBe(false)
    expect(deskForSession(kept)).not.toBeNull()
    // A desk kept in the flat preview has no real height: it is not used in a session.
    expect(deskForSession(readDesk(writeRoom([page], false, defaultDesk(EYE))))).toBeNull()
    expect(readDesk(writeRoom([page]))).toBeNull()
    expect(keptDesk({ at: [0, 9, 0], yawDeg: 0 })).toBeNull()
    // The seats' order is kept with the desk.
    expect(readDesk(writeRoom([page], false, { ...defaultDesk(EYE, 0, true), seats: ['mira', '', 'juno'] }))?.seats).toEqual(['mira', '', 'juno'])
    expect(keptDesk({ at: [0, 1, 0], yawDeg: 0, seats: [1, 2] })?.seats).toBeUndefined()
    expect(deskAsked('?view=pages&desk=1')).toBe(true)
    expect(deskAsked('?view=pages')).toBe(false)
  })
})

describe('a Bot put down on a seat', () => {
  it('sits there, and the Bot that sat there takes the seat it left', () => {
    expect(swapSeat(['a', 'b', 'c'], 'a', 2)).toEqual(['c', 'b', 'a'])
    expect(swapSeat(['a', '', 'c'], 'c', 1)).toEqual(['a', 'c', ''])
  })
  it('changes nothing for a Bot with no seat, its own seat, or a seat that does not stand', () => {
    expect(swapSeat(['a', 'b'], 'z', 0)).toEqual(['a', 'b'])
    expect(swapSeat(['a', 'b'], 'a', 0)).toEqual(['a', 'b'])
    expect(swapSeat(['a', 'b'], 'a', 5)).toEqual(['a', 'b'])
  })
})
