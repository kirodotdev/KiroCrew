/**
 * What the conductor is not allowed to change. A reply's `board-edit` and `board-add`
 * blocks are filtered here before they land: whatever touches a protected shape is
 * dropped, and what was dropped is reported so the person can be told.
 */
import {
  DEFAULT_SIZE, boardAccess, conductorAccessAt, conductorAccessOf,
  type Board, type BoardAdd, type BoardEdit, type ShapeKind,
} from './boardModel'

export interface Guarded<T> { value: T; refused: string[] }

/** The edit with every part that touches a protected shape taken out. */
export function guardEdit(b: Board, edit: BoardEdit): Guarded<BoardEdit> {
  const byId = new Map(b.shapes.map(s => [s.id, s]))
  const refused: string[] = []
  // An id the board does not know passes: applying it skips it anyway.
  const may = (id: string) => { const s = byId.get(id); return !s || conductorAccessOf(b, s) === 'allow' }
  const shapes = (edit.shapes ?? []).filter(e => {
    if (!e || typeof e.id !== 'string') return true
    const target = typeof e.in === 'string' ? byId.get(e.in) : undefined
    // A move by coordinates must not land inside a protected section either.
    const own = byId.get(e.id)
    const lands = own && (typeof e.x === 'number' || typeof e.y === 'number')
      ? conductorAccessAt(b, { x: (typeof e.x === 'number' ? e.x : own.x) + (e.w ?? own.w) / 2, y: (typeof e.y === 'number' ? e.y : own.y) + (e.h ?? own.h) / 2 })
      : 'allow'
    if (may(e.id) && lands === 'allow' && (!target || conductorAccessOf(b, target) === 'allow')) return true
    refused.push(e.id)
    return false
  })
  const del = (edit.delete ?? []).filter(id => {
    if (typeof id !== 'string' || may(id)) return true
    refused.push(id)
    return false
  })
  const arrange = (edit.arrange ?? []).filter(id => {
    if (typeof id !== 'string' || may(id)) return true
    refused.push(id)
    return false
  })
  const deleteConnectors = (edit.deleteConnectors ?? []).filter(d => {
    const conns = b.connectors.filter(c => (typeof d === 'string' ? c.id === d : !!d && c.from === d.from && c.to === d.to))
    const bad = conns.find(c => !may(c.from) || !may(c.to))
    if (!bad) return true
    refused.push(`${bad.from} -> ${bad.to}`)
    return false
  })
  return { value: { ...edit, shapes, delete: del, arrange, deleteConnectors }, refused: [...new Set(refused)] }
}

/** The additions with every shape that would land in a protected place, and every connector to a protected shape, taken out. */
export function guardAdd(b: Board, add: BoardAdd): Guarded<BoardAdd> {
  const byId = new Map(b.shapes.map(s => [s.id, s]))
  const refused: string[] = []
  const declared = new Set((add.shapes ?? []).map(s => s?.ref).filter((r): r is string => typeof r === 'string'))
  const dropped = new Set<string>()
  // A name in this block is a new shape; any other is a shape already on the board.
  const existingDenied = (id: string) => { const s = declared.has(id) ? undefined : byId.get(id); return !!s && conductorAccessOf(b, s) === 'deny' }
  const shapes = (add.shapes ?? []).filter(s => {
    if (!s) return false
    const kind = s.kind as ShapeKind | undefined
    let ok: boolean
    if (typeof s.in === 'string') ok = !dropped.has(s.in) && !existingDenied(s.in)
    else if (typeof s.x === 'number' && typeof s.y === 'number') {
      const size = DEFAULT_SIZE[kind ?? 'sticky'] ?? DEFAULT_SIZE.sticky
      ok = conductorAccessAt(b, { x: s.x + (s.w ?? size.w) / 2, y: s.y + (s.h ?? size.h) / 2 }) === 'allow'
    } else ok = boardAccess(b) === 'allow'
    if (ok) return true
    if (s.ref) dropped.add(s.ref)
    refused.push((s.text ?? '').trim() || s.ref || 'a new shape')
    return false
  })
  const connectors = (add.connectors ?? []).filter(c => {
    if (!c) return false
    const bad = [c.from, c.to].some(id => dropped.has(id) || existingDenied(id))
    if (bad) refused.push(`${c.from} -> ${c.to}`)
    return !bad
  })
  return { value: { ...add, shapes, connectors }, refused: [...new Set(refused)] }
}
