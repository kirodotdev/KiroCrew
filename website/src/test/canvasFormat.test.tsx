/**
 * The canvas file format: a JSON Canvas document with Kiro's fields added. These
 * read a file exactly as Obsidian saved it after editing one of Kiro's boards,
 * and check that what Kiro writes back keeps everything it did not understand.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import CanvasEditor from '../components/canvas/CanvasEditor'
import { copyShapes, pasteClip, restack } from '../components/canvas/canvasEditing'
import { emptyCanvas, parseCanvas, serializeCanvas, type Canvas } from '../components/canvas/canvasModel'
import { connectorRoute } from '../components/canvas/connectorGeometry'

/** A board Kiro wrote, then edited in Obsidian: a text change, a move, a preset colour and a new card. */
const OBSIDIAN_SAVED = `{
	"kirocanvas":1,
	"nodes":[
		{"id":"sec1","type":"group","shape":"section","swatch":"smoke","x":-40,"y":-80,"width":760,"height":380,"color":"#D9D9D9","label":"Sprint board"},
		{"id":"rect1","type":"text","text":"Review with design","shape":"rect","swatch":"blue","fill":"transparent","weight":"thick","border":"dashed","borderSwatch":"violet","fontSize":20,"locked":true,"x":260,"y":30,"width":220,"height":130,"color":"#3FAAFF"},
		{"id":"ell1","type":"text","text":"Done?","shape":"ellipse","swatch":"green","inkHex":"#1F2328","strike":true,"x":520,"y":40,"width":180,"height":130,"color":"#6CD26E"},
		{"id":"dia1","type":"text","text":"Decide","shape":"diamond","hex":"#FF7A00","x":520,"y":380,"width":170,"height":150,"color":"#FF7A00"},
		{"id":"txt1","type":"text","text":"# not a heading in Kiro\\n* not a bullet in Kiro","shape":"text","ink":"dark","fontSize":16,"align":"left","x":0,"y":400,"width":320,"height":60},
		{"id":"stk1","type":"text","text":"Ship the canvas updated","shape":"sticky","swatch":"lavender","bold":true,"align":"center","x":-220,"y":355,"width":200,"height":200,"color":"3"},
		{"id":"36a799c3db668637","x":-270,"y":65,"width":250,"height":60,"color":"4","type":"text","text":"heyhkejn"}
	],
	"edges":[
		{"id":"c1","line":"straight","fromNode":"stk1","fromSide":"right","toNode":"rect1","toSide":"left"},
		{"id":"c2","swatch":"red","line":"curve","dashed":true,"fromNode":"rect1","fromSide":"right","toNode":"ell1","toSide":"left","fromEnd":"arrow","color":"#F2573C"},
		{"id":"c3","line":"elbow","route":[{"x":610,"y":270},{"x":605,"y":270}],"fromNode":"ell1","fromSide":"bottom","toNode":"dia1","toSide":"top"}
	]
}`

const read = (raw: string): Canvas => {
  const r = parseCanvas(raw)
  if (!r.ok) throw new Error('not a canvas')
  return r.canvas
}
const shape = (c: Canvas, id: string) => c.shapes.find(s => s.id === id)!
const conn = (c: Canvas, id: string) => c.connectors.find(x => x.id === id)!

describe('the canvas file format', () => {
  it('reads a board Obsidian saved, in its stacking order', () => {
    const c = read(OBSIDIAN_SAVED)
    expect(c.shapes.map(s => s.id)).toEqual(['sec1', 'rect1', 'ell1', 'dia1', 'txt1', 'stk1', '36a799c3db668637'])
    expect(shape(c, 'sec1')).toMatchObject({ kind: 'section', text: 'Sprint board', color: 'smoke', w: 760, h: 380 })
    expect(shape(c, 'rect1')).toMatchObject({ kind: 'rect', color: 'blue', fill: 'transparent', weight: 'thick', border: 'dashed', fontSize: 20, locked: true })
    expect(shape(c, 'dia1')).toMatchObject({ kind: 'diamond', hex: '#FF7A00' })
    expect(shape(c, 'txt1')).toMatchObject({ kind: 'text', text: '# not a heading in Kiro\n* not a bullet in Kiro', ink: 'dark', fontSize: 16 })
  })

  it('takes a colour set in Obsidian over the named colour it left stale, presets as their nearest swatch', () => {
    const c = read(OBSIDIAN_SAVED)
    // Obsidian set preset "3" (yellow) and left `swatch: "lavender"` behind.
    expect(shape(c, 'stk1')).toMatchObject({ kind: 'sticky', color: 'yellow', text: 'Ship the canvas updated', x: -220, y: 355 })
    expect(shape(c, 'stk1').hex).toBeUndefined()
    // A card made in Obsidian has no `shape`: a rectangle, coloured preset "4".
    expect(shape(c, '36a799c3db668637')).toMatchObject({ kind: 'rect', color: 'green', text: 'heyhkejn' })
  })

  it('reads a custom colour set elsewhere as a custom fill, keeping the named one under it', () => {
    const c = read(JSON.stringify({ nodes: [{ id: 'a', type: 'text', x: 0, y: 0, width: 100, height: 100, text: '', shape: 'rect', swatch: 'blue', color: '#123456' }] }))
    expect(shape(c, 'a')).toMatchObject({ color: 'blue', hex: '#123456' })
  })

  it('reads a missing toEnd as an arrow, a start-only arrow as a line drawn the other way, and keeps routes', () => {
    const c = read(OBSIDIAN_SAVED)
    expect(conn(c, 'c1')).toMatchObject({ from: 'stk1', to: 'rect1', line: 'straight' })
    expect(conn(c, 'c1').arrows).toBeUndefined()
    expect(conn(c, 'c2')).toMatchObject({ from: 'rect1', to: 'ell1', arrows: 'both', dashed: true, color: 'red', line: 'curve' })
    expect(conn(c, 'c3').route).toEqual([{ x: 610, y: 270 }, { x: 605, y: 270 }])
    const back = read(JSON.stringify({
      nodes: [{ id: 'a', type: 'text', x: 0, y: 0, width: 10, height: 10, text: '' }, { id: 'b', type: 'text', x: 50, y: 0, width: 10, height: 10, text: '' }],
      edges: [{ id: 'e', fromNode: 'a', fromSide: 'left', toNode: 'b', toSide: 'right', fromEnd: 'arrow', toEnd: 'none' }],
    }))
    expect(conn(back, 'e')).toMatchObject({ from: 'b', fromSide: 'right', to: 'a', toSide: 'left' })
    expect(conn(back, 'e').arrows).toBeUndefined()
  })

  it('reverses stored corners with a start-only arrow and preserves its geometry', () => {
    const route = [{ x: 220, y: 50 }, { x: 300, y: 250 }]
    const c = read(JSON.stringify({
      nodes: [
        { id: 'a', type: 'text', x: 0, y: 0, width: 100, height: 100, text: '' },
        { id: 'b', type: 'text', x: 400, y: 200, width: 100, height: 100, text: '' },
      ],
      edges: [{ id: 'e', fromNode: 'a', toNode: 'b', fromSide: 'right', toSide: 'left', fromEnd: 'arrow', toEnd: 'none', line: 'elbow', route }],
    }))
    const reversed = conn(c, 'e')
    expect(reversed).toMatchObject({ from: 'b', to: 'a', fromSide: 'left', toSide: 'right', route: [route[1], route[0]] })
    expect(reversed.arrows).toBeUndefined()
    const original = connectorRoute(shape(c, 'a'), shape(c, 'b'), { fromSide: 'right', toSide: 'left', line: 'elbow', route })
    expect(connectorRoute(shape(c, reversed.from), shape(c, reversed.to), reversed).points).toEqual([...original.points].reverse())
    expect(conn(read(serializeCanvas(c)), 'e')).toEqual(reversed)
  })

  it('writes back every field it did not use, and nodes it cannot draw with their lines', () => {
    const doc = {
      kirocanvas: 1,
      plugin: { theme: 'dark' },
      nodes: [
        { id: 'f', type: 'file', file: 'notes/plan.md', x: 0, y: 0, width: 300, height: 200 },
        { id: 'a', type: 'text', x: 400, y: 0, width: 200, height: 100, text: 'hi', shape: 'sticky', swatch: 'yellow', color: '#FFC83D', borderSwatch: 'violet', styleAttributes: { shape: 'pill' } },
      ],
      edges: [{ id: 'fa', fromNode: 'f', toNode: 'a' }],
    }
    const c = read(JSON.stringify(doc))
    expect(c.shapes.map(s => s.id)).toEqual(['a'])
    const out = JSON.parse(serializeCanvas(c))
    expect(out.plugin).toEqual({ theme: 'dark' })
    expect(out.nodes[0]).toEqual(doc.nodes[0])
    expect(out.nodes[1]).toMatchObject({ borderSwatch: 'violet', styleAttributes: { shape: 'pill' }, text: 'hi', shape: 'sticky' })
    expect(out.edges).toEqual([{ id: 'fa', fromNode: 'f', toNode: 'a' }])
  })

  it('drops a kept line once one of its ends is gone', () => {
    const c = read(JSON.stringify({
      nodes: [{ id: 'f', type: 'link', url: 'https://example.com', x: 0, y: 0, width: 300, height: 200 }, { id: 'a', type: 'text', x: 400, y: 0, width: 200, height: 100, text: '' }],
      edges: [{ id: 'fa', fromNode: 'f', toNode: 'a' }],
    }))
    expect(JSON.parse(serializeCanvas({ ...c, shapes: [] })).edges).toEqual([])
  })

  it('writes what any JSON Canvas reader understands, and reads its own writing back unchanged', () => {
    const c = read(OBSIDIAN_SAVED)
    const out = JSON.parse(serializeCanvas(c))
    expect(out.kirocanvas).toBe(1)
    expect(out.nodes.find((n: { id: string }) => n.id === 'sec1')).toMatchObject({ type: 'group', label: 'Sprint board', x: -40, y: -80, width: 760, height: 380, color: '#D9D9D9' })
    expect(out.nodes.find((n: { id: string }) => n.id === 'stk1')).toMatchObject({ type: 'text', text: 'Ship the canvas updated', color: '3', shape: 'sticky', swatch: 'lavender' })
    expect(out.edges.find((e: { id: string }) => e.id === 'c2')).toEqual(JSON.parse(OBSIDIAN_SAVED).edges[1])
    expect(read(serializeCanvas(c))).toEqual(c)
  })

  it('rounds positions and sizes to whole numbers, as the spec requires', () => {
    const c = emptyCanvas()
    c.shapes.push({ id: 'a', kind: 'rect', x: 10.4, y: 10.6, w: 99.5, h: 50.2, text: '', color: 'sky' })
    expect(JSON.parse(serializeCanvas(c)).nodes[0]).toMatchObject({ x: 10, y: 11, width: 100, height: 50 })
  })

  it('refuses a document that is not a JSON Canvas', () => {
    expect(parseCanvas(JSON.stringify({ kind: 'kirocrew-canvas', schema: 1, shapes: [], connectors: [] })).ok).toBe(false)
    expect(parseCanvas(JSON.stringify({ nodes: 'x' })).ok).toBe(false)
    expect(parseCanvas(JSON.stringify({ edges: {} })).ok).toBe(false)
  })

  it.each([128, 12_000])('refuses %i-deep extension data without throwing', depth => {
    const nested = '['.repeat(depth) + '0' + ']'.repeat(depth)
    const documents = [
      `{"nodes":[],"edges":[],"plugin":${nested}}`,
      `{"nodes":[{"id":"a","type":"text","plugin":${nested}}],"edges":[]}`,
      `{"nodes":[],"edges":[{"id":"e","plugin":${nested}}]}`,
      `{"nodes":[],"edges":[],"plugin":${'{"child":'.repeat(depth)}0${'}'.repeat(depth)}}`,
    ]
    for (const raw of documents) {
      expect(() => JSON.parse(raw)).not.toThrow()
      expect(() => parseCanvas(raw)).not.toThrow()
      expect(parseCanvas(raw)).toEqual({ ok: false })
    }
  })

  it('round-trips shallow extension data and serializes its editor snapshot', () => {
    const doc = { nodes: [], edges: [], plugin: { nested: ['kept', { enabled: true }] } }
    const c = read(JSON.stringify(doc))
    expect(JSON.parse(serializeCanvas(c))).toEqual(doc)
    expect(() => JSON.stringify(c)).not.toThrow()
    expect(Object.isFrozen(c.source!.top.plugin)).toBe(true)
  })

  it('reads prototype member names as unknown colours, not as swatches', () => {
    const names = ['constructor', 'toString', '__proto__']
    const nodes = names.map((n, i) => ({ id: `n${i}`, type: 'text', text: n, shape: i ? 'rect' : 'text', x: i * 300, y: 0, width: 220, height: 130, color: n, swatch: n, borderColor: n, ink: n }))
    const edges = names.map((n, i) => ({ id: `e${i}`, fromNode: 'n0', toNode: `n${(i + 1) % 3}`, color: n, swatch: n }))
    const c = read(JSON.stringify({ kirocanvas: 1, nodes, edges }))
    for (const s of c.shapes) {
      expect(s.color).toBe('white')
      expect(s.borderColor).toBeUndefined()
      expect(s.ink).toBeUndefined()
    }
    for (const x of c.connectors) expect(x.color).toBeUndefined()
    expect(() => serializeCanvas(c)).not.toThrow()
  })
})

/** Key order is formatting; array order and every stored value are content. */
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b, 'en')).map(([k, v]) => [k, canonical(v)]))
  return value
}

const IMPORTED = {
  plugin: { nested: ['kept', { enabled: true }] },
  nodes: [
    { id: 'a', type: 'text', x: 0, y: 0, width: 100, height: 100, text: 'First', label: 'stray', color: '3', locked: true, custom: { rank: 1 } },
    { id: 'f', type: 'file', x: 200, y: 0, width: 100, height: 100, file: 'notes/plan.md' },
    { id: 'b', type: 'text', x: 400, y: 0, width: 100, height: 100, text: 'Second', custom: [1, 2] },
    { id: 'l', type: 'link', x: 600, y: 0, width: 100, height: 100, url: 'https://example.com' },
  ],
  edges: [
    { id: 'e', fromNode: 'a', toNode: 'b', color: '#123456', custom: { exact: true } },
    { id: 'foreign', fromNode: 'f', toNode: 'a', color: '#ABCDEF' },
    { id: 'back', fromNode: 'a', toNode: 'b', fromSide: 'right', toSide: 'left', fromEnd: 'arrow', toEnd: 'none', line: 'elbow', route: [{ x: 200, y: 50 }, { x: 300, y: 150 }], custom: 'kept' },
  ],
}
const output = (c: Canvas) => JSON.parse(serializeCanvas(c))

describe('source-preserving canvas writes', () => {
  it.each([
    ['Obsidian presets', JSON.parse(OBSIDIAN_SAVED)],
    ['hex colours, interleaved foreign nodes, reverse route and unknown fields', IMPORTED],
  ])('round-trips untouched %s exactly', (_name, fixture) => {
    expect(canonical(output(read(JSON.stringify(fixture))))).toEqual(canonical(fixture))
  })

  it.each(['recolour node', 'recolour edge', 'move', 'retext', 'toggle arrow', 'unlock'])(
    'writes only the changed keys for %s', edit => {
      const c = read(JSON.stringify(IMPORTED))
      const expected = structuredClone(IMPORTED)
      let edited: Canvas = c
      if (edit === 'recolour node') {
        edited = { ...c, shapes: c.shapes.map(s => s.id === 'a' ? { ...s, color: 'red' } : s) }
        Object.assign(expected.nodes[0], { color: '#F2573C', swatch: 'red' })
      } else if (edit === 'recolour edge') {
        edited = { ...c, connectors: c.connectors.map(e => e.id === 'e' ? { ...e, color: 'red' } : e) }
        Object.assign(expected.edges[0], { color: '#C13A22', swatch: 'red' })
      } else if (edit === 'move') {
        edited = { ...c, shapes: c.shapes.map(s => s.id === 'a' ? { ...s, x: 36 } : s) }
        expected.nodes[0].x = 36
      } else if (edit === 'retext') {
        edited = { ...c, shapes: c.shapes.map(s => s.id === 'a' ? { ...s, text: 'Edited' } : s) }
        expected.nodes[0].text = 'Edited'
      } else if (edit === 'toggle arrow') {
        edited = { ...c, connectors: c.connectors.map(e => e.id === 'e' ? { ...e, arrows: 'none' } : e) }
        Object.assign(expected.edges[0], { toEnd: 'none' })
      } else {
        edited = { ...c, shapes: c.shapes.map(s => s.id === 'a' ? { ...s, locked: undefined } : s) }
        delete expected.nodes[0].locked
      }
      expect(canonical(output(edited))).toEqual(canonical(expected))
    },
  )

  it('restacks only drawable slots and leaves foreign slots in place', () => {
    const c = read(JSON.stringify(IMPORTED))
    const expected = { ...IMPORTED, nodes: [IMPORTED.nodes[2], IMPORTED.nodes[1], IMPORTED.nodes[0], IMPORTED.nodes[3]], edges: [IMPORTED.edges[2], IMPORTED.edges[1], IMPORTED.edges[0]] }
    expect(canonical(output({ ...restack(c, new Set(['a']), 'front'), connectors: [...c.connectors].reverse() }))).toEqual(canonical(expected))
  })

  it('drops a deleted node and all its edges without changing survivors', () => {
    const c = read(JSON.stringify(IMPORTED))
    expect(canonical(output({ ...c, shapes: c.shapes.filter(s => s.id !== 'a') }))).toEqual(canonical({ ...IMPORTED, nodes: IMPORTED.nodes.slice(1), edges: [] }))
  })

  it('appends a new drawable after the last foreign node', () => {
    const c = read(JSON.stringify(IMPORTED))
    const added = { ...shape(c, 'b'), id: 'new', text: 'New' }
    const newNode = output({ ...emptyCanvas(), shapes: [added] }).nodes[0]
    expect(canonical(output({ ...c, shapes: [...c.shapes, added] }))).toEqual(canonical({ ...IMPORTED, nodes: [...IMPORTED.nodes, newNode] }))
  })

  it.each([false, true])('saves a new shape sent to back with foreign slots: %s', foreign => {
    const nodes = foreign
      ? [IMPORTED.nodes[0], IMPORTED.nodes[1], { ...IMPORTED.nodes[2], id: 'c' }]
      : [IMPORTED.nodes[0]]
    const c = read(JSON.stringify({ nodes, edges: [] }))
    const added = { ...shape(c, 'a'), id: 'b', text: 'New' }
    const edited = restack({ ...c, shapes: [...c.shapes, added] }, new Set(['b']), 'back')
    expect(edited.shapes.map(s => s.id)).toEqual(foreign ? ['b', 'a', 'c'] : ['b', 'a'])
    const newNode = output({ ...emptyCanvas(), shapes: [added] }).nodes[0]
    const actual = output(edited)
    expect(actual.nodes.map((n: { id: string }) => n.id)).toEqual(foreign ? ['b', 'f', 'a', 'c'] : ['b', 'a'])
    expect(actual.nodes).toEqual(foreign ? [newNode, nodes[1], nodes[0], nodes[2]] : [newNode, nodes[0]])
  })

  it.each([false, true])('saves a new connector sent to back with foreign slots: %s', foreign => {
    const edges = foreign ? IMPORTED.edges : [IMPORTED.edges[0]]
    const c = read(JSON.stringify({ nodes: IMPORTED.nodes, edges }))
    const added = { ...conn(c, 'e'), id: 'new' }
    const newEdge = output({ ...emptyCanvas(), shapes: c.shapes, connectors: [added] }).edges[0]
    const actual = output({ ...c, connectors: [added, ...c.connectors] })
    expect(actual.edges.map((e: { id: string }) => e.id)).toEqual(foreign ? ['new', 'foreign', 'e', 'back'] : ['new', 'e'])
    expect(actual.edges).toEqual(foreign ? [newEdge, edges[1], edges[0], edges[2]] : [newEdge, edges[0]])
    expect(actual.nodes).toEqual(IMPORTED.nodes)
  })

  it('writes an edited start-only edge in model orientation', () => {
    const c = read(JSON.stringify(IMPORTED))
    const back = conn(c, 'back')
    const expected = structuredClone(IMPORTED)
    Object.assign(expected.edges[2], { fromNode: 'b', toNode: 'a', fromSide: 'left', toSide: 'right', fromEnd: 'none', toEnd: 'arrow', route: [...IMPORTED.edges[2].route!].reverse(), dashed: true })
    expect(canonical(output({ ...c, connectors: c.connectors.map(e => e === back ? { ...e, dashed: true } : e) }))).toEqual(canonical(expected))
  })
})

describe('source identity across editing', () => {
  it('edit then undo restores the exact imported file and its source reference', () => {
    const initial = read(JSON.stringify(IMPORTED))
    let latest = initial
    function Harness() {
      const [canvas, setCanvas] = useState(initial)
      latest = canvas
      return <CanvasEditor canvas={canvas} onChange={setCanvas} />
    }
    render(<Harness />)
    const editor = screen.getByTestId('canvas-editor')
    // The unlocked second card is at (400, 0); the initial view adds (120, 100).
    const at = { clientX: 570, clientY: 150, button: 0, pointerId: 1 }
    fireEvent.pointerDown(editor, at)
    fireEvent.pointerMove(window, { ...at, clientX: 606 })
    fireEvent.pointerUp(window, { ...at, clientX: 606 })
    expect(shape(latest, 'b').x).not.toBe(shape(initial, 'b').x)
    expect(latest.source).toBe(initial.source)
    expect(JSON.stringify(latest)).not.toBe(JSON.stringify(initial))
    fireEvent.keyDown(editor, { key: 'Delete' })
    expect(latest.shapes.map(s => s.id)).toEqual(['a'])
    fireEvent.keyDown(editor, { key: 'z', metaKey: true })
    fireEvent.keyDown(editor, { key: 'z', metaKey: true })
    expect(latest.source).toBe(initial.source)
    expect(JSON.stringify(latest)).toBe(JSON.stringify(initial))
    expect(canonical(output(latest))).toEqual(canonical(IMPORTED))
  })

  it('pastes fresh ids without inheriting source values, keeping the shared source frozen', () => {
    const initial = read(JSON.stringify(IMPORTED))
    const source = initial.source!
    let counter = 0
    const random = vi.spyOn(crypto, 'getRandomValues').mockImplementation(<T extends ArrayBufferView | null>(array: T): T => {
      if (array) new Uint8Array(array.buffer, array.byteOffset, array.byteLength).fill(++counter)
      return array
    })
    try {
      const pasted = pasteClip(initial, copyShapes(initial, new Set(['a', 'b']))!).canvas
      expect(pasted.source).toBe(source)
      expect(pasted.shapes.slice(2).map(s => s.id)).toEqual(['s' + '01'.repeat(12), 's' + '02'.repeat(12)])
      const expected = output({ ...emptyCanvas(), shapes: pasted.shapes.slice(2), connectors: pasted.connectors.slice(2) })
      const actual = output(pasted)
      expect(actual.nodes.slice(0, 4)).toEqual(IMPORTED.nodes)
      expect(actual.nodes.slice(4)).toEqual(expected.nodes)
      expect(actual.edges.slice(0, 3)).toEqual(IMPORTED.edges)
      expect(actual.edges.slice(3)).toEqual(expected.edges)
      expect(Object.isFrozen(source)).toBe(true)
      expect(Object.isFrozen(source.nodes.a.custom)).toBe(true)
      expect(canonical(output(initial))).toEqual(canonical(IMPORTED))
    } finally { random.mockRestore() }
  })

  it('re-ids duplicate drawable and foreign entries without losing their raw values', () => {
    const fixture = { ...IMPORTED, nodes: [...IMPORTED.nodes, { ...IMPORTED.nodes[0], text: 'Duplicate' }, { ...IMPORTED.nodes[1], file: 'other.md' }], edges: [...IMPORTED.edges, { ...IMPORTED.edges[0], custom: { exact: false } }] }
    let counter = 0
    const random = vi.spyOn(crypto, 'getRandomValues').mockImplementation(<T extends ArrayBufferView | null>(array: T): T => {
      if (array) new Uint8Array(array.buffer, array.byteOffset, array.byteLength).fill(++counter)
      return array
    })
    try {
      const actual = output(read(JSON.stringify(fixture)))
      const expected = structuredClone(fixture)
      expected.nodes[4].id = 's' + '01'.repeat(12)
      expected.nodes[5].id = 's' + '02'.repeat(12)
      expected.edges[3].id = 'c' + '03'.repeat(12)
      expect(canonical(actual)).toEqual(canonical(expected))
    } finally { random.mockRestore() }
  })
})
