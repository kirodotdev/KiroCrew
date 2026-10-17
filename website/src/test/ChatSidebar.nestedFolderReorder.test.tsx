/**
 * Nested subfolder drag: reorder-vs-nest routing in sidebarCollision.
 *
 * #10428 reported a control asymmetry. `chat_folder_move`'s `before` / `after`
 * (#10396) let an agent position a nested subfolder among its siblings, and the
 * sidebar draws nested rows in that stored `order` — but a person had no way to
 * change it, because a nested drag resolved to ONE outcome. The collision branch
 * for a nested row filtered the droppables down to `folder-drop` zones only, so
 * every offset on every target read as a re-parent and no reorder target existed.
 *
 * This locks the routing that closes it, which is the same "thirds" rule a root
 * row already used: pointing at the MIDDLE band of a sibling's header re-parents
 * INTO that sibling, and pointing at its header EDGE or its body falls through to
 * the sibling reorder. The two directions are asserted separately because only
 * the second one changed — a nested drag could always nest, and it is the edge
 * offset that used to nest and must now reorder.
 *
 * The band arithmetic itself is `ChatSidebar.folderNestBand.test.tsx`, and that
 * the MEASURED header feeds it is `ChatSidebar.folderNestBandCallSite.test.tsx`.
 * This file is about which droppable the nested branch hands back.
 */
import { describe, it, expect } from 'vitest'
import type { Active, DroppableContainer, ClientRect } from '@dnd-kit/core'
import { sidebarCollision } from '../pages/ChatSidebar'

// jsdom builds a ClientRect-shaped literal, not a DOMRect instance. dnd-kit's
// algorithms only read the numeric fields, so a plain object is enough.
function rect(top: number, left: number, width: number, height: number): ClientRect {
  return {
    top, left, width, height,
    right: left + width,
    bottom: top + height,
  } as ClientRect
}

/** The `folder-drop` zone a nested drag can re-parent into. Its node carries one
 *  child whose rect reports the header height, which is what the collision reads
 *  to place the nest band. */
function dropContainer(folderId: string, blockRect: ClientRect, headerHeightPx: number): DroppableContainer {
  const header = document.createElement('div')
  Object.defineProperty(header, 'getBoundingClientRect', {
    value: () => rect(blockRect.top, blockRect.left, blockRect.width, headerHeightPx),
  })
  const node = document.createElement('div')
  node.appendChild(header)
  return {
    id: `folder-drop:${folderId}`,
    key: `folder-drop:${folderId}`,
    data: { current: { type: 'folder-drop', folderId } },
    rect: { current: blockRect },
    node: { current: node },
    disabled: false,
  } as unknown as DroppableContainer
}

/** The sortable container a reorder resolves to. Its dnd-kit id IS the folder id,
 *  which is what `handleSidebarDragEnd` passes to the renumber as `over.id`. */
function sortableContainer(folderId: string, blockRect: ClientRect): DroppableContainer {
  return {
    id: folderId,
    key: folderId,
    data: { current: { type: 'folder' } },
    rect: { current: blockRect },
    node: { current: document.createElement('div') },
    disabled: false,
  } as unknown as DroppableContainer
}

const DRAGGED = 'kid-dragged'
const SIBLING = 'kid-sibling'
const HEADER_H = 26

// One expanded sibling block, 300px tall with a 26px header at y=100. The nest
// band is the middle 60% of the HEADER: absolute y in [105.2, 120.8].
const SIBLING_BLOCK = rect(100, 0, 200, 300)

function runNestedCollision(pointerY: number, siblings: readonly string[] = [DRAGGED, SIBLING]) {
  const drop = dropContainer(SIBLING, SIBLING_BLOCK, HEADER_H)
  const sortable = sortableContainer(SIBLING, SIBLING_BLOCK)
  return sidebarCollision({
    active: {
      id: DRAGGED,
      data: { current: { type: 'folder', nested: true, subtree: [DRAGGED], siblings } },
      rect: { current: { initial: null, translated: null } },
    } as Active,
    collisionRect: SIBLING_BLOCK,
    // pointerWithin's real hit-test reads this map, keyed by container id.
    droppableRects: new Map<string, ClientRect>([
      [drop.id as string, SIBLING_BLOCK],
      [sortable.id as string, SIBLING_BLOCK],
    ]),
    droppableContainers: [drop, sortable],
    pointerCoordinates: { x: 100, y: pointerY },
  })
}

const resolvedType = (collisions: ReturnType<typeof runNestedCollision>) =>
  collisions[0]?.data?.droppableContainer?.data?.current?.type

describe('nested subfolder drag: sidebarCollision routing', () => {
  it('resolves the MIDDLE of a sibling header as a re-parent', () => {
    // y = 100 + 13 → offsetY 13 into a 26px header, inside [5.2, 20.8].
    expect(resolvedType(runNestedCollision(113))).toBe('folder-drop')
  })

  it('resolves the LOWER edge of a sibling header as a reorder', () => {
    // y = 100 + 23 → offsetY 23 > 0.8*26 = 20.8. Before #10428 the nested branch
    // returned the folder-drop here whatever the offset, so this is the case that
    // made a nested row un-reorderable.
    const collisions = runNestedCollision(123)
    expect(resolvedType(collisions)).toBe('folder')
    expect(collisions[0]?.id).toBe(SIBLING)
  })

  it('resolves the UPPER edge of a sibling header as a reorder', () => {
    // y = 100 + 3 → offsetY 3 < 0.2*26 = 5.2.
    expect(resolvedType(runNestedCollision(103))).toBe('folder')
  })

  it('resolves the sibling BODY, far below its header, as a reorder', () => {
    // y = 250 is 150px into the block — nowhere near the header, so there is no
    // nest gesture to claim it and it belongs to the sibling reorder.
    expect(resolvedType(runNestedCollision(250))).toBe('folder')
  })

  it('keeps a NON-sibling target a re-parent at every offset', () => {
    // The same lower-edge pointer, with the target outside the dragged row's
    // container. There is no reorder to fall through to across containers — the
    // renumber refuses it — so the gesture stays what it has always been.
    expect(resolvedType(runNestedCollision(123, [DRAGGED]))).toBe('folder-drop')
    expect(resolvedType(runNestedCollision(113, [DRAGGED]))).toBe('folder-drop')
  })

  it('resolves a reorder inside the sibling ring even when a nearer row is outside it', () => {
    // The reorder fallback ranks by distance to a container's CENTRE, and every
    // folder row — root and nested — is now a `folder` container. An outsider can
    // therefore sit nearer the pointer than the sibling being aimed at: here a
    // short root block centred 3px away against the tall sibling block centred
    // 127px away. Unranked, the drag would resolve to the outsider, and the
    // renumber refuses a target in another container — so the drag would land on
    // nothing and read as having been ignored.
    const drop = dropContainer(SIBLING, SIBLING_BLOCK, HEADER_H)
    const sibling = sortableContainer(SIBLING, SIBLING_BLOCK)
    const OUTSIDER = 'root-elsewhere'
    const outsiderBlock = rect(113, 0, 200, 20)
    const outsider = sortableContainer(OUTSIDER, outsiderBlock)
    // The dragged row as the pointer currently holds it: a single 26px row at the
    // sibling header's lower edge. closestCenter ranks by distance from THIS rect's
    // centre (123) — the outsider's centre is the same 123, the tall sibling block's
    // is 127px away, so the outsider wins on distance and only the ring filter
    // keeps it out.
    const draggedRect = rect(110, 0, 200, 26)
    const collisions = sidebarCollision({
      active: {
        id: DRAGGED,
        data: { current: { type: 'folder', nested: true, subtree: [DRAGGED], siblings: [DRAGGED, SIBLING] } },
        rect: { current: { initial: null, translated: null } },
      } as Active,
      collisionRect: draggedRect,
      droppableRects: new Map<string, ClientRect>([
        [drop.id as string, SIBLING_BLOCK],
        [sibling.id as string, SIBLING_BLOCK],
        [outsider.id as string, outsiderBlock],
      ]),
      droppableContainers: [drop, sibling, outsider],
      // Lower edge of the sibling header: the reorder fallback, not the nest band.
      pointerCoordinates: { x: 100, y: 123 },
    })
    expect(collisions[0]?.id).toBe(SIBLING)
  })

  it('never offers the dragged row own subtree as a target', () => {
    // Its own `folder-drop` is filtered out before any ranking, so a drag that
    // resolves to nothing is a release that keeps the current parent rather than
    // a folder dropped into itself.
    const own = dropContainer(DRAGGED, SIBLING_BLOCK, HEADER_H)
    const collisions = sidebarCollision({
      active: {
        id: DRAGGED,
        data: { current: { type: 'folder', nested: true, subtree: [DRAGGED], siblings: [DRAGGED] } },
        rect: { current: { initial: null, translated: null } },
      } as Active,
      collisionRect: SIBLING_BLOCK,
      droppableRects: new Map<string, ClientRect>([[own.id as string, SIBLING_BLOCK]]),
      droppableContainers: [own],
      pointerCoordinates: { x: 100, y: 113 },
    })
    expect(collisions).toEqual([])
  })

  it('resolves a KEYBOARD nested drag as a reorder, not a re-parent', () => {
    // A keyboard drag carries no pointer coordinates, so it has no "where on the
    // row" and can never land in a nest band. Resolving it against the
    // `folder-drop` zones would make every keyboard drop a re-parent, leaving a
    // keyboard user with exactly the harm #10428 reports -- an order only an agent
    // can set -- while the row walks its sibling ring on screen. The root branch
    // already lands on the ring for the same reason: its whole thirds block is
    // gated on `pointerCoordinates`.
    const drop = dropContainer(SIBLING, SIBLING_BLOCK, HEADER_H)
    const sortable = sortableContainer(SIBLING, SIBLING_BLOCK)
    const collisions = sidebarCollision({
      active: {
        id: DRAGGED,
        data: { current: { type: 'folder', nested: true, subtree: [DRAGGED], siblings: [DRAGGED, SIBLING] } },
        rect: { current: { initial: null, translated: null } },
      } as Active,
      collisionRect: SIBLING_BLOCK,
      droppableRects: new Map<string, ClientRect>([
        [drop.id as string, SIBLING_BLOCK],
        [sortable.id as string, SIBLING_BLOCK],
      ]),
      droppableContainers: [drop, sortable],
      // The keyboard sensor's signature: activated, moving, no pointer.
      pointerCoordinates: null,
    })
    expect(resolvedType(collisions)).toBe('folder')
    expect(collisions[0]?.id).toBe(SIBLING)
  })

  it('keeps a KEYBOARD nested drag a re-parent when there is no sibling ring', () => {
    // Drag data predating the `siblings` field, where a nested row's only gesture
    // was a re-parent. With no ring there is no reorder to offer, so the
    // folder-drop resolution it already had is the honest answer -- the keyboard
    // fix must not silently turn that case into a reorder against nothing.
    const drop = dropContainer(SIBLING, SIBLING_BLOCK, HEADER_H)
    const sortable = sortableContainer(SIBLING, SIBLING_BLOCK)
    const collisions = sidebarCollision({
      active: {
        id: DRAGGED,
        data: { current: { type: 'folder', nested: true, subtree: [DRAGGED] } },
        rect: { current: { initial: null, translated: null } },
      } as Active,
      collisionRect: SIBLING_BLOCK,
      droppableRects: new Map<string, ClientRect>([
        [drop.id as string, SIBLING_BLOCK],
        [sortable.id as string, SIBLING_BLOCK],
      ]),
      droppableContainers: [drop, sortable],
      pointerCoordinates: null,
    })
    expect(resolvedType(collisions)).toBe('folder-drop')
  })
})
