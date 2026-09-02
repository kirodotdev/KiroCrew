/**
 * Call-site guard for the reorder-vs-nest band.
 *
 * ChatSidebar.folderNestBand.test.tsx locks the isFolderNestBand helper. This
 * file locks the OTHER half of the contract: that sidebarCollision passes the
 * MEASURED header height into the helper — not the shared FOLDER_HEADER_DROP_BAND
 * px constant. That is the exact regression codex flagged: a hardcoded 34px band
 * used on a shorter board header (~26px) made 5..29px of the row a nest zone,
 * so an intended reorder became a re-parent.
 *
 * If someone reverted the call site to `isFolderNestBand(offsetY, FOLDER_HEADER_DROP_BAND)`
 * the helper test would still pass and the DOM-marker tests would still pass, but
 * this test would fail: pointing at the LOWER edge of a 26px board header
 * (offsetY ≈ 22px) would resolve as nest under a 34px constant (22 ≤ 0.8*34=27.2)
 * but must resolve as REORDER under the measured 26px header (22 > 0.8*26=20.8).
 */
import { describe, it, expect } from 'vitest'
import type { DroppableContainer, ClientRect } from '@dnd-kit/core'
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

/** Build the minimum DroppableContainer shape sidebarCollision + pointerWithin
 *  need: an id, data.current with type + folderId, and rect/node ref-objects. */
function buildContainer(
  folderId: string,
  blockRect: ClientRect,
  headerHeightPx: number,
): DroppableContainer {
  // Node with a single child whose bounding rect returns the header height.
  // pointerWithin never touches node; sidebarCollision reads it for the header
  // measurement via node.current.firstElementChild.getBoundingClientRect().
  const header = document.createElement('div')
  Object.defineProperty(header, 'getBoundingClientRect', {
    value: () => rect(blockRect.top, blockRect.left, blockRect.width, headerHeightPx),
  })
  const node = document.createElement('div')
  node.appendChild(header)
  return {
    id: `col-A-folder-drop:${folderId}`,
    key: `col-A-folder-drop:${folderId}`,
    data: {
      current: { type: 'folder-drop', folderId },
    },
    rect: { current: blockRect },
    node: { current: node },
    disabled: false,
  } as unknown as DroppableContainer
}

const DRAGGED_FOLDER = 'folder-dragged'
const TARGET_FOLDER = 'folder-target'

// Every synthetic collision call needs pointerCoordinates + a droppableRects Map
// keyed by container id (pointerWithin's real hit-test), plus the containers
// themselves (an array — dnd-kit types call it DroppableContainer[]).
function runCollision(pointerY: number, headerHeightPx: number) {
  const blockRect = rect(100 /*top*/, 0, 200, 300 /*block much taller than header*/)
  const target = buildContainer(TARGET_FOLDER, blockRect, headerHeightPx)
  const droppableRects = new Map<string, ClientRect>([[target.id as string, blockRect]])
  return sidebarCollision({
    active: {
      id: DRAGGED_FOLDER,
      // Root folder drag (no `nested`), no subtree — so the "root folder" thirds
      // branch runs, which is the one exercising isFolderNestBand.
      data: { current: { type: 'folder', subtree: [] } },
      rect: { current: { initial: null, translated: null } },
    } as any,
    collisionRect: blockRect,
    droppableRects,
    droppableContainers: [target] as any,
    pointerCoordinates: { x: 100, y: pointerY },
  })
}

describe('sidebarCollision uses the MEASURED header (not FOLDER_HEADER_DROP_BAND)', () => {
  const BOARD_H = 26

  it('resolves middle of board header as nest (folder-drop)', () => {
    // Middle of a 26px header at absolute y = 100 + 13 = 113. With measured 26px
    // and 0.2..0.8 band, offsetY 13 sits in [5.2, 20.8] — nest.
    const collisions = runCollision(113, BOARD_H)
    expect(collisions[0]?.data?.droppableContainer?.data?.current?.type).toBe('folder-drop')
  })

  it('resolves LOWER-edge of board header as reorder (regression guard)', () => {
    // Absolute y = 100 + 22 = 122 → offsetY 22px into a 26px header.
    // Under the buggy 34px constant: 22 ≤ 0.8*34 = 27.2 → wrongly nest.
    // Under the correct measured 26px:  22 > 0.8*26 = 20.8 → fall through to
    // sortable reorder (sidebarCollision returns the closestCenter fallback over
    // 'folder' containers; here we only supply a 'folder-drop', so the fallback
    // finds nothing folder-typed and returns [], meaning definitely NOT a nest).
    const collisions = runCollision(122, BOARD_H)
    // The critical assertion is "not the nest droppable". Either an empty array
    // (fallback found no 'folder' targets in this synthetic setup) or a non
    // folder-drop container is acceptable — both prove reorder-vs-nest routing.
    const first = collisions[0]?.data?.droppableContainer?.data?.current?.type
    expect(first).not.toBe('folder-drop')
  })

  it('resolves UPPER-edge of board header as reorder (regression guard)', () => {
    // Absolute y = 100 + 4 = 104 → offsetY 4px. Under 34px constant: 4 ≥ 0.2*34
    // = 6.8 is FALSE (so also reorder), but the point of this case is completeness:
    // 4 < 0.2*26 = 5.2 → reorder under the measured header too.
    const collisions = runCollision(104, BOARD_H)
    const first = collisions[0]?.data?.droppableContainer?.data?.current?.type
    expect(first).not.toBe('folder-drop')
  })
})
