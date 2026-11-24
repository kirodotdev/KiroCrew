import { describe, expect, it } from 'vitest'
import { Group, Mesh, MeshBasicMaterial } from 'three'
import { BAND, DEPTH, DESK_FRONT, DRAW, LAYERS, PRESS, layIn } from './drawOrder'

describe('one order, drawn and pressed the same (M37)', () => {
  it('draws and presses each layer over every farther one, pages and Bots in one band', () => {
    for (let i = 1; i < LAYERS.length; i++) {
      const near = LAYERS[i - 1]
      const far = LAYERS[i]
      if (near === 'page' && far === 'bot') {
        expect(DRAW.page).toBe(DRAW.bot)
        expect(PRESS.page).toBe(PRESS.bot)
        continue
      }
      expect(DRAW[near] - DRAW[far]).toBeGreaterThanOrEqual(near === 'keys' || far === 'keys' ? 5 : BAND)
      if (near !== 'dot') expect(PRESS[near]).toBeGreaterThan(PRESS[far as Exclude<typeof far, 'dot'>])
    }
    // A desk board's front is over the other Bots and under Home.
    expect(DESK_FRONT).toBeGreaterThan(PRESS.bot)
    expect(DESK_FRONT).toBeLessThan(PRESS.home)
  })

  it('puts a layer in its band and the transparent pass, and gives each mesh back its own pass in the room', () => {
    const g = new Group()
    const mat = new MeshBasicMaterial()
    const m = new Mesh(undefined, mat)
    m.renderOrder = 2
    g.add(m)
    layIn(g, 'held')
    expect(m.renderOrder).toBe(DRAW.held + 2)
    expect(mat.transparent).toBe(true)
    layIn(g, 'bot')
    expect(m.renderOrder).toBe(DRAW.bot + 2)
    expect(mat.transparent).toBe(false)
    layIn(g, 'speak')
    expect(m.renderOrder).toBe(DRAW.speak + 2)
  })

  it('keeps a mesh\'s own order inside its band, so it never reaches the nearer layer (G2)', () => {
    const g = new Group()
    const m = new Mesh(undefined, new MeshBasicMaterial())
    m.renderOrder = BAND + 5
    g.add(m)
    layIn(g, 'home')
    expect(m.renderOrder).toBeGreaterThanOrEqual(DRAW.home)
    expect(m.renderOrder).toBeLessThan(DRAW.home + BAND)
    expect(m.renderOrder).toBeLessThan(DRAW.held)
  })

  it('names which bands test depth in the same table: the held board and the room only', () => {
    expect(LAYERS.filter(l => DEPTH[l])).toEqual(['held', 'page', 'bot'])
  })

  it('puts the card\'s keys and their line under the held card and over the room\'s name line, with no depth test (M87, M68)', () => {
    expect(LAYERS.indexOf('keys')).toBe(LAYERS.indexOf('held') + 1)
    expect(DRAW.keys).toBe(45)
    expect(PRESS.keys).toBeGreaterThan(PRESS.roomname)
    expect(PRESS.keys).toBeLessThan(PRESS.held)
    expect(DEPTH.keys).toBe(false)
    // Its parts' own orders 0 to 2 stay under the held band: 45 to 47, never 51 and 52.
    const g = new Group()
    const top = new Mesh(undefined, new MeshBasicMaterial())
    top.renderOrder = 7
    g.add(top)
    layIn(g, 'keys')
    expect(top.renderOrder).toBeLessThan(DRAW.held)
    expect(top.renderOrder).toBeGreaterThanOrEqual(DRAW.keys)
  })

  it('puts the room\'s name line between the held board and Home, over Home, with no depth test (M58)', () => {
    expect(LAYERS.indexOf('roomname')).toBe(LAYERS.indexOf('keys') + 1)
    expect(LAYERS.indexOf('roomname')).toBe(LAYERS.indexOf('home') - 1)
    expect(DRAW.roomname).toBeGreaterThan(DRAW.home)
    expect(DRAW.roomname).toBeLessThan(DRAW.held)
    expect(PRESS.roomname).toBeGreaterThan(PRESS.home)
    expect(PRESS.roomname).toBeLessThan(PRESS.held)
    expect(DEPTH.roomname).toBe(false)
  })

  it('puts a Bot\'s plate line between Home and the pages, with no depth test (M51)', () => {
    expect(LAYERS.indexOf('botnote')).toBe(LAYERS.indexOf('home') + 1)
    expect(LAYERS.indexOf('botnote')).toBe(LAYERS.indexOf('page') - 1)
    expect(DRAW.botnote).toBeLessThan(DRAW.home)
    expect(DRAW.botnote).toBeGreaterThan(DRAW.page)
    expect(DEPTH.botnote).toBe(false)
  })
})
