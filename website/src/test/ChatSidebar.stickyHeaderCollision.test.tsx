/**
 * Sticky folder headers vs dnd-kit hit-testing.
 *
 * A folder header is `position: sticky`, so while its block is scrolled up the
 * header is PAINTED at the top of the lane, far below its block's unclipped
 * rect top. A descendant folder block scrolled under that pinned header still
 * has its own (unclipped) rect covering the header's painted position, so a
 * hit-test that ranks block rects leaf-first (pointerWithinDeepest) or by box
 * size (pointerWithin) resolves a pointer on the VISIBLE parent header to the
 * hidden child: the session lands in the child, the ring lights the child.
 *
 * sidebarCollision must resolve a pointer on a painted header to THAT header's
 * folder-drop, for session drags and for both folder-drag branches, and the
 * nest band must be measured from the header's live top, not the block's.
 * A pointer on rows BELOW the pinned header is in no header rect and keeps the
 * containment resolution (the child), which the last case pins.
 *
 * Geometry: lane 139..838. Parent block scrolled to -500..1800 with its 32px
 * header pinned at 139..171 (depth 0). Child block 0..400, its header pinned
 * one row lower at 171..203 (depth 1).
 */
import { describe, it, expect } from 'vitest'
import type { ClientRect, Collision, CollisionDetection, DroppableContainer } from '@dnd-kit/core'
import { sidebarCollision } from '../pages/ChatSidebar'

function rect(top: number, bottom: number, left = 245, right = 487): ClientRect {
  return { top, bottom, left, right, width: right - left, height: bottom - top } as ClientRect
}

/** A droppable whose node is appended to `host`. When `headerRect` is given the
 *  node's FIRST child is a header row reporting that live rect — the element
 *  the collision reads through node.firstElementChild. */
function container(id: string, r: ClientRect, host: HTMLElement, data: Record<string, unknown>, headerRect?: ClientRect): DroppableContainer {
  const node = document.createElement('div')
  if (headerRect) {
    const header = document.createElement('div')
    Object.defineProperty(header, 'getBoundingClientRect', { value: () => headerRect })
    node.appendChild(header)
  }
  host.appendChild(node)
  return {
    id,
    key: id,
    data: { current: data },
    rect: { current: r },
    node: { current: node },
    disabled: false,
  } as unknown as DroppableContainer
}

const HEADER_H = 32
const LANE_TOP = 139

function buildWorld() {
  const laneEl = document.createElement('div')
  document.body.appendChild(laneEl)
  const lane = container('root-lane', rect(LANE_TOP, 838), laneEl, { type: 'folder-drop', folderId: null })
  const parentHost = document.createElement('div')
  ;(lane.node.current as HTMLElement).appendChild(parentHost)
  const parent = container('folder-drop:parent', rect(-500, 1800), parentHost,
    { type: 'folder-drop', folderId: 'parent' }, rect(LANE_TOP, LANE_TOP + HEADER_H))
  const childHost = document.createElement('div')
  ;(parent.node.current as HTMLElement).appendChild(childHost)
  const child = container('folder-drop:child', rect(0, 400), childHost,
    { type: 'folder-drop', folderId: 'child' }, rect(LANE_TOP + HEADER_H, LANE_TOP + 2 * HEADER_H))
  const containers = [lane, parent, child]
  const droppableRects = new Map<string, ClientRect>(containers.map(c => [c.id as string, c.rect.current as ClientRect]))
  return { lane, parent, child, containers, droppableRects, cleanup: () => laneEl.remove() }
}

function args(pointer: { x: number; y: number }, world: ReturnType<typeof buildWorld>, activeData: Record<string, unknown>): Parameters<CollisionDetection>[0] {
  return {
    active: {
      id: 'dragged',
      data: { current: activeData },
      rect: { current: { initial: null, translated: null } },
    },
    collisionRect: rect(pointer.y, pointer.y + 20, pointer.x, pointer.x + 40),
    droppableRects: world.droppableRects,
    droppableContainers: world.containers,
    pointerCoordinates: pointer,
  } as unknown as Parameters<CollisionDetection>[0]
}

const winner = (collisions: Collision[]) => (collisions[0] ? String(collisions[0].id) : null)

// Middle of the pinned parent header: 16px into 32px, inside the 0.2..0.8 band.
const ON_PARENT_HEADER = { x: 366, y: LANE_TOP + 16 }

describe('a pointer on a PINNED folder header resolves to that folder, not the descendant scrolled under it', () => {
  it('session drag: files into the parent whose header is painted under the pointer', () => {
    const world = buildWorld()
    expect(winner(sidebarCollision(args(ON_PARENT_HEADER, world, { type: 'session', key: 's1' })))).toBe('folder-drop:parent')
    world.cleanup()
  })

  it('nested-folder drag (non-sibling target): re-parents into the parent, not the hidden child', () => {
    const world = buildWorld()
    const a = args(ON_PARENT_HEADER, world, { type: 'folder', nested: true, subtree: ['sub-x'], siblings: ['sub-x'] })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:parent')
    world.cleanup()
  })

  it('root-folder drag: the nest band is anchored on the pinned header, so its middle nests into the parent', () => {
    const world = buildWorld()
    const a = args(ON_PARENT_HEADER, world, { type: 'folder', subtree: ['dragged'] })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:parent')
    world.cleanup()
  })

  it('root-folder drag: the pinned header\'s bottom edge is still a reorder edge (band from the header\'s live top)', () => {
    const world = buildWorld()
    // 29px into the 32px header > 0.8 * 32 = 25.6: falls through to the reorder
    // fallback, which finds no `folder` sortable here and yields nothing.
    const a = args({ x: 366, y: LANE_TOP + 29 }, world, { type: 'folder', subtree: ['dragged'] })
    expect(sidebarCollision(a).map(c => String(c.id))).not.toContain('folder-drop:parent')
    world.cleanup()
  })

  it('a pointer on a row BELOW the pinned headers keeps the containment resolution (the child)', () => {
    const world = buildWorld()
    // y=300 is inside the child block and inside neither header's live rect.
    expect(winner(sidebarCollision(args({ x: 366, y: 300 }, world, { type: 'session', key: 's1' })))).toBe('folder-drop:child')
    expect(winner(sidebarCollision(args({ x: 366, y: 300 }, world, { type: 'folder', nested: true, subtree: ['sub-x'] })))).toBe('folder-drop:child')
    world.cleanup()
  })

  it('a pointer on the CHILD\'s own pinned header (one row lower) resolves to the child', () => {
    const world = buildWorld()
    const a = args({ x: 366, y: LANE_TOP + HEADER_H + 16 }, world, { type: 'session', key: 's1' })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:child')
    world.cleanup()
  })
})
