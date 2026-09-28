/**
 * Regression pins for the two collision inversions that made "drop a session
 * into a tall parent folder" impossible in a sidebar whose folder tree is
 * taller than the scroll viewport (a sidebar with ~170 open sessions):
 *
 *  M1 — pointerWithin ranks containing droppables by average pointer→corner
 *  distance, a box-size proxy for "innermost". Droppable rects are UNCLIPPED
 *  border boxes, so an expanded folder block (2300px tall) overflows its
 *  ancestor root lane (viewport-sized, ~700px). The ancestor's SMALLER box
 *  out-ranked the folder the pointer was visually on: no highlight on the
 *  folder, and the drop resolved to the lane — which UNFILES the session.
 *  pointerWithinDeepest re-ranks containment leaf-first (a contained node's
 *  candidate beats its container's).
 *
 *  M2 — the near-miss fallback used closestCenter: a pointer a fraction of a
 *  px outside the tall folder's top edge is half the folder's height from its
 *  center, so a small subfolder ~100px away won and the drop landed there.
 *  closestEdge measures to the nearest rect EDGE instead, with near-ties
 *  (sub-px ancestor/child edge overlap) broken by DOM depth.
 *
 * Geometry below is lifted from the live reproduction (repro harness against
 * the real fixture shape), not invented: lane 139..838, folder block
 * 139..2443, subfolder 173..309.
 */
import { describe, it, expect } from 'vitest'
import type { ClientRect, Collision, CollisionDetection, DroppableContainer } from '@dnd-kit/core'
import { pointerWithinDeepest, closestEdge } from '../components/dnd'
import { sidebarCollision } from '../pages/ChatSidebar'

function rect(top: number, bottom: number, left = 245, right = 487): ClientRect {
  return { top, bottom, left, right, width: right - left, height: bottom - top } as ClientRect
}

/** A droppable whose node is nested `depth` levels under document.body, so
 *  DOM-depth ranking sees the real ancestor/descendant relationship. */
function container(id: string, r: ClientRect, host: HTMLElement, data: Record<string, unknown> = {}): DroppableContainer {
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

/** Build the proven-broken world: root lane (viewport-sized box) is the DOM
 *  ANCESTOR of a folder block that overflows it, plus a small subfolder. */
function buildWorld() {
  const laneEl = document.createElement('div')
  document.body.appendChild(laneEl)
  const lane = container('root-lane', rect(139, 838), laneEl, { type: 'folder-drop', folderId: null })
  // The folder-drop node lives INSIDE the lane's node.
  const kiroHost = document.createElement('div')
  ;(lane.node.current as HTMLElement).appendChild(kiroHost)
  const kiro = container('folder-drop:kiro', rect(139, 2443), kiroHost, { type: 'folder-drop', folderId: 'kiro' })
  const docwHost = document.createElement('div')
  ;(kiro.node.current as HTMLElement).appendChild(docwHost)
  const docw = container('folder-drop:docw', rect(173, 309), docwHost, { type: 'folder-drop', folderId: 'docw' })
  const containers = [lane, kiro, docw]
  const droppableRects = new Map<string, ClientRect>(containers.map(c => [c.id as string, c.rect.current as ClientRect]))
  const cleanup = () => { laneEl.remove() }
  return { lane, kiro, docw, containers, droppableRects, cleanup }
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

describe('pointerWithinDeepest (M1: containment ranked leaf-first)', () => {
  it('resolves a pointer on a tall folder to the folder, not its viewport-sized ancestor lane', () => {
    const world = buildWorld()
    // Pointer on one of the folder's own session rows, below the lane's
    // clipped bottom edge is irrelevant — 685 is inside BOTH boxes.
    const a = args({ x: 366, y: 685 }, world, { type: 'session', key: 's1' })
    expect(winner(pointerWithinDeepest(a))).toBe('folder-drop:kiro')
    world.cleanup()
  })

  it('still prefers the subfolder when the pointer is inside it (innermost containment)', () => {
    const world = buildWorld()
    const a = args({ x: 366, y: 240 }, world, { type: 'session', key: 's1' })
    expect(winner(pointerWithinDeepest(a))).toBe('folder-drop:docw')
    world.cleanup()
  })

  it('an UNRELATED droppable (portaled, deeply nested elsewhere) cannot steal a win by raw depth', () => {
    const world = buildWorld()
    // A portaled zone whose node lives in a separate, DEEPLY nested DOM branch
    // (like the chat-pane reference target). Its rect happens to contain the
    // pointer too. With no ancestor/descendant relation to the folder nodes it
    // must keep pointerWithin's incoming order — never win just for being
    // nested deeper in an unrelated subtree.
    let host: HTMLElement = document.body
    for (let i = 0; i < 20; i++) { const d = document.createElement('div'); host.appendChild(d); host = d }
    const portal = container('portal-zone', rect(139, 2443, 245, 487), host, { type: 'chat-pane-ref' })
    world.containers.push(portal)
    world.droppableRects.set(portal.id as string, portal.rect.current as ClientRect)
    const a = args({ x: 366, y: 685 }, world, { type: 'session', key: 's1' })
    const ranked = pointerWithinDeepest(a).map(c => String(c.id))
    // The folder still wins (it is contained by the lane, so it is a leaf of
    // that relation); the portal ranks by pointerWithin's own metric, behind
    // the leaf group's winner despite its 20-deep node.
    expect(ranked[0]).toBe('folder-drop:kiro')
    world.cleanup()
    document.body.replaceChildren()
  })
})

describe('closestEdge (M2: near-miss ranked by edge distance, ties by containment)', () => {
  it('a pointer a fraction of a px above the tall folder resolves to it, not the small subfolder closestCenter preferred', () => {
    const world = buildWorld()
    // 0.14px above every top edge — the exact autoscroll-hold position from
    // the reproduction (list padding row): a TRUE near miss, outside all boxes.
    const a = args({ x: 366, y: 138.86 }, world, { type: 'session', key: 's1' })
    const ranked = closestEdge(a)
    // Lane and folder edges tie within the epsilon; the folder's node is
    // contained by the lane's, so leaf-first puts the folder ahead.
    expect(winner(ranked)).toBe('folder-drop:kiro')
    // The subfolder (34px away) must rank behind both same-edge candidates.
    expect(ranked.map(c => String(c.id)).indexOf('folder-drop:docw')).toBe(2)
    world.cleanup()
  })

  it('ranks by true edge distance when edges are NOT tied', () => {
    const world = buildWorld()
    // Shift the subfolder's box so the pointer sits strictly outside every
    // box with UNEQUAL gaps: 2px above the lane/folder tops, 12px above the
    // subfolder's. Distance ordering alone must pick the nearer edge group.
    world.droppableRects.set('folder-drop:docw', rect(151, 309))
    const a = args({ x: 366, y: 137 }, world, { type: 'session', key: 's1' })
    const ranked = closestEdge(a).map(c => String(c.id))
    expect(ranked[0]).toBe('folder-drop:kiro')
    expect(ranked[2]).toBe('folder-drop:docw')
    world.cleanup()
  })

  it('edges exactly EDGE_TIE_EPSILON_PX apart still count as the same edge (containment decides)', () => {
    const world = buildWorld()
    // The folder's top sits exactly 1px below the lane's — a 1px border or
    // offset between the lane node and a folder-drop node is a real layout.
    // Inclusive grouping keeps the folder ahead; an exclusive cut would hand
    // the win back to the ancestor lane.
    world.droppableRects.set('folder-drop:kiro', rect(140, 2443))
    const a = args({ x: 366, y: 138 }, world, { type: 'session', key: 's1' })
    expect(winner(closestEdge(a))).toBe('folder-drop:kiro')
    world.cleanup()
  })

  it('degrades to closestCenter — not to no target — when there are no pointer coordinates (keyboard drags)', () => {
    const world = buildWorld()
    const a = args({ x: 0, y: 0 }, world, { type: 'session', key: 's1' })
    ;(a as { pointerCoordinates: unknown }).pointerCoordinates = null
    expect(closestEdge(a).length).toBeGreaterThan(0)
    world.cleanup()
  })
})

describe('leafFirst ordering through pointerWithinDeepest', () => {
  it('emits a three-level containment chain deepest-first all the way down', () => {
    const world = buildWorld()
    // Pointer inside lane ⊃ kiro ⊃ docw. The FULL ranking must be the chain
    // reversed, not just the right head: consumers may walk past rank 0.
    const a = args({ x: 366, y: 240 }, world, { type: 'session', key: 's1' })
    expect(pointerWithinDeepest(a).map(c => String(c.id))).toEqual([
      'folder-drop:docw', 'folder-drop:kiro', 'root-lane',
    ])
    world.cleanup()
  })
})

describe('leafFirst semantics with an unrelated candidate interleaved', () => {
  it('every non-container lands in the first peel, in incoming order; containers follow leaf-first', () => {
    const world = buildWorld()
    // An unrelated candidate (separate DOM branch) whose rect also contains
    // the pointer. It contains no other candidate, so it belongs to the first
    // peel alongside docw — ahead of both containers — with the incoming
    // metric ordering the peel internally. Callers that must keep a specific
    // unrelated zone behind containers pre-filter it (the chat-pane step).
    const otherHost = document.createElement('div')
    document.body.appendChild(otherHost)
    const other = container('other-zone', rect(139, 2443), otherHost, { type: 'folder-drop', folderId: 'other' })
    world.containers.push(other)
    world.droppableRects.set(other.id as string, other.rect.current as ClientRect)
    const a = args({ x: 366, y: 240 }, world, { type: 'session', key: 's1' })
    expect(pointerWithinDeepest(a).map(c => String(c.id))).toEqual([
      'folder-drop:docw', 'other-zone', 'folder-drop:kiro', 'root-lane',
    ])
    otherHost.remove()
    world.cleanup()
  })
})

describe('keyboard drags (no pointer coordinates) never resolve to no target', () => {
  it('a nested-folder re-parent drag without pointer coordinates still yields a winner', () => {
    const world = buildWorld()
    const a = args({ x: 0, y: 0 }, world, { type: 'folder', nested: true, subtree: ['sub-x'] })
    ;(a as { pointerCoordinates: unknown }).pointerCoordinates = null
    expect(sidebarCollision(a).length).toBeGreaterThan(0)
    world.cleanup()
  })

  it('a session drag without pointer coordinates still yields a winner', () => {
    const world = buildWorld()
    const a = args({ x: 0, y: 0 }, world, { type: 'session', key: 's1' })
    ;(a as { pointerCoordinates: unknown }).pointerCoordinates = null
    expect(sidebarCollision(a).length).toBeGreaterThan(0)
    world.cleanup()
  })

  it('a session drag without pointer coordinates resolves even when the pane is the ONLY droppable', () => {
    // Flat layouts can mount the chat-pane zone with no sidebar droppable at
    // all; a keyboard drag must still land somewhere.
    const host = document.createElement('div')
    document.body.appendChild(host)
    const pane = container('chat-pane-zone', rect(0, 900, 500, 1400), host, { type: 'chat-pane-ref' })
    const droppableRects = new Map<string, ClientRect>([[pane.id as string, pane.rect.current as ClientRect]])
    const a = {
      active: { id: 'dragged', data: { current: { type: 'session', key: 's1' } }, rect: { current: { initial: null, translated: null } } },
      collisionRect: rect(100, 120, 100, 140),
      droppableRects,
      droppableContainers: [pane],
      pointerCoordinates: null,
    } as unknown as Parameters<CollisionDetection>[0]
    expect(sidebarCollision(a).map(c => String(c.id))).toEqual(['chat-pane-zone'])
    host.remove()
  })
})

describe('deliberate un-filing stays reachable (the lane must still win where it should)', () => {
  it('a pointer on empty lane space below the folders resolves to the lane/ungroup bucket', () => {
    const world = buildWorld()
    // root-group: the ungrouped-sessions bucket, a DOM CHILD of the lane,
    // spanning the area below the folder blocks.
    const groupHost = document.createElement('div')
    ;(world.lane.node.current as HTMLElement).appendChild(groupHost)
    const group = container('root-group', rect(700, 838), groupHost, { type: 'folder-drop', folderId: null })
    world.containers.push(group)
    world.droppableRects.set(group.id as string, group.rect.current as ClientRect)
    // Shrink the folder so the pointer at y=780 is genuinely outside it.
    world.droppableRects.set('folder-drop:kiro', rect(139, 690))
    const a = args({ x: 366, y: 780 }, world, { type: 'session', key: 's1' })
    const ranked = sidebarCollision(a).map(c => String(c.id))
    expect(ranked[0]).toBe('root-group')
    world.cleanup()
  })

  it('the explicit un-nest hint beats its containing group when hovered', () => {
    const world = buildWorld()
    const groupHost = document.createElement('div')
    ;(world.lane.node.current as HTMLElement).appendChild(groupHost)
    const group = container('root-group', rect(700, 838), groupHost, { type: 'folder-drop', folderId: null })
    const hintHost = document.createElement('div')
    ;(group.node.current as HTMLElement).appendChild(hintHost)
    const hint = container('root-unnest-hint', rect(710, 782), hintHost, { type: 'folder-drop', folderId: null })
    world.containers.push(group, hint)
    world.droppableRects.set(group.id as string, group.rect.current as ClientRect)
    world.droppableRects.set(hint.id as string, hint.rect.current as ClientRect)
    world.droppableRects.set('folder-drop:kiro', rect(139, 690))
    const a = args({ x: 366, y: 740 }, world, { type: 'folder', nested: true, subtree: ['sub-x'] })
    expect(winner(sidebarCollision(a))).toBe('root-unnest-hint')
    world.cleanup()
  })
})

describe('the portaled chat-pane zone cannot shadow sidebar targets', () => {
  it('a pane rect overlapping the sidebar loses to any containing sidebar target, regardless of incoming order', () => {
    const world = buildWorld()
    // Pane rect covers the whole sidebar area; its node lives in an unrelated
    // DOM branch (portal), so containment cannot arbitrate — the session
    // branch must consult sidebar containers first.
    let host: HTMLElement = document.body
    for (let i = 0; i < 3; i++) { const d = document.createElement('div'); host.appendChild(d); host = d }
    const pane = container('chat-pane-zone', rect(0, 3000, 0, 2000), host, { type: 'chat-pane-ref' })
    world.containers.unshift(pane) // incoming order puts the pane FIRST
    world.droppableRects.set(pane.id as string, pane.rect.current as ClientRect)
    const a = args({ x: 366, y: 685 }, world, { type: 'session', key: 's1' })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:kiro')
    world.cleanup()
    document.body.replaceChildren()
  })
})

describe('sidebarCollision session branch wires the fixed strategies', () => {
  it('containment: session drag over the tall folder yields the folder (drop files, ring shows)', () => {
    const world = buildWorld()
    const a = args({ x: 366, y: 685 }, world, { type: 'session', key: 's1' })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:kiro')
    world.cleanup()
  })

  it('near miss: pointer just outside every box still resolves to the folder whose edge it touches', () => {
    const world = buildWorld()
    const a = args({ x: 366, y: 138.5 }, world, { type: 'session', key: 's1' })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:kiro')
    world.cleanup()
  })

  it('nested-folder re-parent drag over the tall folder targets the folder, not the lane (no silent un-nest)', () => {
    const world = buildWorld()
    const a = args({ x: 366, y: 685 }, world, { type: 'folder', nested: true, subtree: ['sub-x'] })
    expect(winner(sidebarCollision(a))).toBe('folder-drop:kiro')
    world.cleanup()
  })
})
