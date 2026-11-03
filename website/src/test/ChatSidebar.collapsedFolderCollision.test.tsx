/**
 * Subfolders inside a COLLAPSED folder vs dnd-kit hit-testing.
 *
 * FolderBody keeps a closed body mounted (`inert`, hidden), and the subfolder
 * blocks inside it measure at the collapsed body's position: directly on top of
 * the next folder down. A pointer on that next folder's header is then also
 * inside the hidden subfolder's header rect, and the two ordinary headers tie;
 * the hidden one, registered first, used to win, so the drop filed into a
 * folder the person cannot see.
 *
 * Geometry: lane 100..900. `oncall` is collapsed: its block is its 32px header
 * at 100..132, and its inert body holds the subfolder `oncall-dev`, measured at
 * 132..500 with its header at 132..164. The visible folder `study` follows at
 * 132..260, header 132..164.
 */
import { describe, it, expect } from 'vitest'
import type { ClientRect, Collision, CollisionDetection, DroppableContainer } from '@dnd-kit/core'
import { sidebarCollision } from '../pages/ChatSidebar'

function rect(top: number, bottom: number, left = 245, right = 487): ClientRect {
  return { top, bottom, left, right, width: right - left, height: bottom - top } as ClientRect
}

/** A droppable appended to `host`, whose first child is a header row reporting
 *  `headerRect` (the element the collision reads through firstElementChild). */
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

function buildWorld() {
  const laneEl = document.createElement('div')
  document.body.appendChild(laneEl)
  const lane = container('root-lane', rect(100, 900), laneEl, { type: 'folder-drop', folderId: null })
  const laneNode = lane.node.current as HTMLElement
  const oncall = container('folder-drop:oncall', rect(100, 132), laneNode,
    { type: 'folder-drop', folderId: 'oncall' }, rect(100, 132))
  // The collapsed body, as FolderBody renders it while closed.
  const body = document.createElement('div')
  body.setAttribute('inert', '')
  ;(oncall.node.current as HTMLElement).appendChild(body)
  const hiddenDev = container('folder-drop:oncall-dev', rect(132, 500), body,
    { type: 'folder-drop', folderId: 'oncall-dev' }, rect(132, 164))
  const study = container('folder-drop:study', rect(132, 260), laneNode,
    { type: 'folder-drop', folderId: 'study' }, rect(132, 164))
  const containers = [lane, oncall, hiddenDev, study]
  const droppableRects = new Map<string, ClientRect>(containers.map(c => [c.id as string, c.rect.current as ClientRect]))
  return { containers, droppableRects, cleanup: () => laneEl.remove() }
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

// Middle of study's header: 16px into 32px, inside the 0.2..0.8 nest band.
const ON_STUDY_HEADER = { x: 366, y: 148 }

describe('a subfolder hidden in a collapsed folder never takes a drop aimed at the folder below it', () => {
  it('session drag on the next folder\'s header files into that folder', () => {
    const world = buildWorld()
    expect(winner(sidebarCollision(args(ON_STUDY_HEADER, world, { type: 'session', key: 's1' })))).toBe('folder-drop:study')
    world.cleanup()
  })

  it('session drag on the next folder\'s rows files into that folder', () => {
    const world = buildWorld()
    // y=220 is a study row, and inside the hidden subfolder's measured block.
    expect(winner(sidebarCollision(args({ x: 366, y: 220 }, world, { type: 'session', key: 's1' })))).toBe('folder-drop:study')
    world.cleanup()
  })

  it('root-folder drag nests into the visible folder under the pointer', () => {
    const world = buildWorld()
    const a = args(ON_STUDY_HEADER, world, { type: 'folder', subtree: ['dragged'] })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:study')
    world.cleanup()
  })

  it('nested-folder drag re-parents into the visible folder under the pointer', () => {
    const world = buildWorld()
    const a = args(ON_STUDY_HEADER, world, { type: 'folder', nested: true, subtree: ['sub-x'], siblings: ['sub-x'] })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:study')
    world.cleanup()
  })

  it('the collapsed folder\'s own header still takes the drop', () => {
    const world = buildWorld()
    expect(winner(sidebarCollision(args({ x: 366, y: 116 }, world, { type: 'session', key: 's1' })))).toBe('folder-drop:oncall')
    world.cleanup()
  })
})
