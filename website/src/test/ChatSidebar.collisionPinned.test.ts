/**
 * `sidebarCollision`, session branch: which droppables a SESSION drag may resolve
 * to, pinned against hand-built dnd-kit geometry (no render).
 *
 * Contract:
 *  (1) A `pinned-session` droppable (the insertion target between pinned rows) is
 *      eligible only when the dragged session is itself pinned AND was picked up
 *      in the same container. Otherwise it is invisible to every sidebar step.
 *  (2) The portaled `chat-pane-ref` zone wins only when no sidebar droppable
 *      contains the pointer.
 *  (3) A pointer-less (keyboard / synthetic) session drag with no sidebar
 *      droppable at all degrades to closestCenter over EVERY droppable; with a
 *      sidebar droppable present it never resolves to the pane.
 *  (4) A pointer drag inside no droppable falls back to the nearest SIDEBAR
 *      droppable by edge distance, never to the pane.
 *
 * Neighbouring cases already pinned elsewhere and not repeated here:
 *  - pane overlapping a containing sidebar folder, pane-only keyboard drag,
 *    edge-vs-centre near miss: dndCollisionDepth.test.tsx
 *  - sticky folder headers: ChatSidebar.stickyHeaderCollision.test.tsx
 *  - folder-drag branches: ChatSidebarMoreCoverage.test.tsx
 */
import { afterEach, describe, expect, it } from 'vitest'
import type { ClientRect, CollisionDetection, DroppableContainer } from '@dnd-kit/core'
import { sidebarCollision } from '../pages/ChatSidebar'

type Args = Parameters<CollisionDetection>[0]

function rect(top: number, bottom: number, left = 0, right = 240): ClientRect {
  return { top, bottom, left, right, width: right - left, height: bottom - top } as ClientRect
}

/** A droppable whose node is appended under `host`, so DOM containment between
 *  candidates is real (the leaf-first re-ranking reads `node.contains`). */
function container(id: string, r: ClientRect, host: HTMLElement, data: Record<string, unknown>): DroppableContainer {
  const node = document.createElement('div')
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

const nodeOf = (c: DroppableContainer) => c.node.current as HTMLElement

function args(opts: {
  active: Record<string, unknown>
  containers: DroppableContainer[]
  pointer: { x: number; y: number } | null
  collisionRect?: ClientRect
}): Args {
  return {
    active: { id: 'dragged', data: { current: opts.active }, rect: { current: { initial: null, translated: null } } },
    collisionRect: opts.collisionRect ?? rect(0, 20, 0, 40),
    droppableRects: new Map(opts.containers.map(c => [c.id, c.rect.current as ClientRect])),
    droppableContainers: opts.containers,
    pointerCoordinates: opts.pointer,
  } as unknown as Args
}

const ids = (hits: ReturnType<CollisionDetection>) => hits.map(h => String(h.id))

let roots: HTMLElement[] = []
function newRoot(): HTMLElement {
  const el = document.createElement('div')
  document.body.appendChild(el)
  roots.push(el)
  return el
}
afterEach(() => {
  for (const r of roots) r.remove()
  roots = []
})

/** Root lane 0..800 holding one pinned insertion target at 100..140 (container
 *  `root`). The pane, when present, lives in an unrelated DOM branch, as the
 *  portal puts it. */
function pinnedWorld() {
  const laneHost = newRoot()
  const lane = container('root-lane', rect(0, 800), laneHost, { type: 'folder-drop', folderId: null })
  const pinned = container('pinned-session:list:k2', rect(100, 140), nodeOf(lane),
    { type: 'pinned-session', key: 'k2', container: 'root' })
  return { lane, pinned }
}

const ON_PINNED_ROW = { x: 50, y: 120 }

describe('sidebarCollision — pinned-session eligibility', () => {
  it('resolves to the pinned target when the dragged session is pinned in the same container', () => {
    const { lane, pinned } = pinnedWorld()
    const hits = sidebarCollision(args({
      active: { type: 'session', key: 'k1', pinned: true, container: 'root' },
      containers: [lane, pinned],
      pointer: ON_PINNED_ROW,
    }))
    expect(ids(hits)[0]).toBe('pinned-session:list:k2')
  })

  it('ignores the pinned target when the dragged session was picked up in another container', () => {
    const { lane, pinned } = pinnedWorld()
    const hits = sidebarCollision(args({
      active: { type: 'session', key: 'k1', pinned: true, container: 'f1' },
      containers: [lane, pinned],
      pointer: ON_PINNED_ROW,
    }))
    expect(ids(hits)).toEqual(['root-lane'])
  })

  it('ignores the pinned target when the dragged session is not pinned', () => {
    const { lane, pinned } = pinnedWorld()
    for (const active of [
      { type: 'session', key: 'k1', pinned: false, container: 'root' },
      { type: 'session', key: 'k1', container: 'root' },
    ]) {
      const hits = sidebarCollision(args({ active, containers: [lane, pinned], pointer: ON_PINNED_ROW }))
      expect(ids(hits)).toEqual(['root-lane'])
    }
  })

  it('keeps an ineligible pinned target out of the near-miss fallback too', () => {
    // Pointer 5px above both the lane's and the pinned row's top edge: an edge tie
    // that DOM containment would break in the pinned row's favour if it were a
    // candidate at all.
    const laneHost = newRoot()
    const lane = container('root-lane', rect(0, 800), laneHost, { type: 'folder-drop', folderId: null })
    const pinned = container('pinned-session:list:k2', rect(0, 40), nodeOf(lane),
      { type: 'pinned-session', key: 'k2', container: 'root' })
    const above = { x: 50, y: -5 }
    const eligible = sidebarCollision(args({
      active: { type: 'session', key: 'k1', pinned: true, container: 'root' },
      containers: [lane, pinned],
      pointer: above,
    }))
    expect(ids(eligible)[0]).toBe('pinned-session:list:k2')
    const ineligible = sidebarCollision(args({
      active: { type: 'session', key: 'k1', pinned: true, container: 'f1' },
      containers: [lane, pinned],
      pointer: above,
    }))
    expect(ids(ineligible)).toEqual(['root-lane'])
  })
})

describe('sidebarCollision — the chat-pane zone', () => {
  it('wins when the pointer is inside the pane and inside no sidebar droppable', () => {
    const laneHost = newRoot()
    const lane = container('root-lane', rect(0, 800, 0, 240), laneHost, { type: 'folder-drop', folderId: null })
    const pane = container('chat-pane-ref', rect(0, 900, 300, 1200), newRoot(), { type: 'chat-pane-ref' })
    const hits = sidebarCollision(args({
      active: { type: 'session', key: 'k1' },
      containers: [lane, pane],
      pointer: { x: 700, y: 400 },
    }))
    expect(ids(hits)).toEqual(['chat-pane-ref'])
  })

  it('loses to an eligible pinned target that contains the pointer, even when the pane overlaps it', () => {
    const { lane, pinned } = pinnedWorld()
    // The pane is listed FIRST and spans the whole sidebar, so neither incoming
    // order nor geometry can be what hands the drop to the sidebar.
    const pane = container('chat-pane-ref', rect(0, 3000, 0, 2000), newRoot(), { type: 'chat-pane-ref' })
    const hits = sidebarCollision(args({
      active: { type: 'session', key: 'k1', pinned: true, container: 'root' },
      containers: [pane, lane, pinned],
      pointer: ON_PINNED_ROW,
    }))
    expect(ids(hits)).not.toContain('chat-pane-ref')
    expect(ids(hits)[0]).toBe('pinned-session:list:k2')
  })
})

describe('sidebarCollision — pointer-less (keyboard) session drags', () => {
  it('with no sidebar droppable, degrades to closestCenter over every droppable', () => {
    // The only sidebar droppable is an INELIGIBLE pinned target, so the sidebar set
    // is empty; the fallback ranks everything, pane included, by centre distance.
    const pinned = container('pinned-session:list:k2', rect(0, 40, 0, 240), newRoot(),
      { type: 'pinned-session', key: 'k2', container: 'root' })
    const pane = container('chat-pane-ref', rect(0, 900, 300, 1200), newRoot(), { type: 'chat-pane-ref' })
    const nearPane = sidebarCollision(args({
      active: { type: 'session', key: 'k1' },
      containers: [pinned, pane],
      pointer: null,
      collisionRect: rect(440, 460, 730, 770),
    }))
    expect(ids(nearPane)).toEqual(['chat-pane-ref', 'pinned-session:list:k2'])
  })

  it('with a sidebar droppable present, never resolves to the pane, however close its centre is', () => {
    const folder = container('folder-drop:f1', rect(0, 40, 0, 240), newRoot(), { type: 'folder-drop', folderId: 'f1' })
    const pane = container('chat-pane-ref', rect(0, 900, 300, 1200), newRoot(), { type: 'chat-pane-ref' })
    const hits = sidebarCollision(args({
      active: { type: 'session', key: 'k1' },
      containers: [pane, folder],
      pointer: null,
      // Dead centre on the pane.
      collisionRect: rect(440, 460, 730, 770),
    }))
    expect(ids(hits)).toEqual(['folder-drop:f1'])
  })
})

describe('sidebarCollision — a pointer inside no droppable', () => {
  it('falls back to the nearest sidebar droppable by edge, never the pane even when its edge is nearer', () => {
    const near = container('folder-drop:near', rect(0, 40, 0, 240), newRoot(), { type: 'folder-drop', folderId: 'near' })
    const far = container('folder-drop:far', rect(200, 240, 0, 240), newRoot(), { type: 'folder-drop', folderId: 'far' })
    const pane = container('chat-pane-ref', rect(0, 900, 300, 1200), newRoot(), { type: 'chat-pane-ref' })
    // x=299 sits 1px left of the pane and 59px right of both folders.
    const hits = sidebarCollision(args({
      active: { type: 'session', key: 'k1' },
      containers: [pane, far, near],
      pointer: { x: 299, y: 20 },
    }))
    expect(ids(hits)).not.toContain('chat-pane-ref')
    expect(ids(hits)).toEqual(['folder-drop:near', 'folder-drop:far'])
  })
})
