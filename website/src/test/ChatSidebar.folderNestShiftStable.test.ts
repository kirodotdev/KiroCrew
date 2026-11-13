/**
 * A root folder drag over an expanded folder's header gives one result per
 * pointer position, whether or not the drag has slid the target (#28542).
 *
 * A reorder result slides the target block by the dragged row's height (a
 * transform on its sortable wrapper), and a nest result takes that slide away.
 * When the nest band was judged against the slid header, each result moved the
 * header across the pointer and flipped the next one: nest and reorder
 * alternated with no pointer movement. The band is judged where the header sits
 * with no slide, which is where a nest leaves it.
 *
 * Each case runs the collision twice at the SAME pointer: once with the target
 * slid (the state a reorder result paints) and once unslid (the state a nest
 * result paints). Both must agree.
 */
import { describe, it, expect, afterEach } from 'vitest'
import type { Active, DroppableContainer, ClientRect } from '@dnd-kit/core'
import { sidebarCollision } from '../pages/ChatSidebar'

function rect(top: number, left: number, width: number, height: number): ClientRect {
  return { top, left, width, height, right: left + width, bottom: top + height } as ClientRect
}

const DRAGGED = 'folder-b'
const TARGET = 'folder-a'
const HEADER_H = 34
// The dragged row's height: how far a reorder slides the target.
const SLIDE = 34
// Where the target block was laid out, before any slide.
const BLOCK = rect(100, 0, 200, 300)

/** The target's sortable wrapper, its folder-drop block and header row, and the
 *  sortable `folder` container the reorder fallback resolves to. */
function buildTarget(transform: string, slid: boolean) {
  const wrapper = document.createElement('div')
  wrapper.setAttribute('data-folder-sortable', TARGET)
  wrapper.style.transform = transform
  const block = document.createElement('div')
  const header = document.createElement('div')
  // The header is painted where the slide puts it.
  Object.defineProperty(header, 'getBoundingClientRect', {
    value: () => rect(BLOCK.top + (slid ? SLIDE : 0), 0, 200, HEADER_H),
  })
  block.appendChild(header)
  wrapper.appendChild(block)
  document.body.appendChild(wrapper)
  const drop = {
    id: `folder-drop:${TARGET}`, key: `folder-drop:${TARGET}`,
    data: { current: { type: 'folder-drop', folderId: TARGET } },
    rect: { current: BLOCK }, node: { current: block }, disabled: false,
  } as unknown as DroppableContainer
  const sortable = {
    id: TARGET, key: TARGET,
    data: { current: { type: 'folder' } },
    rect: { current: BLOCK }, node: { current: wrapper }, disabled: false,
  } as unknown as DroppableContainer
  return { wrapper, drop, sortable }
}

afterEach(() => { document.body.replaceChildren() })

/** The droppable type the collision resolves to: `folder-drop` is a nest,
 *  `folder` is a reorder. */
function resolve(pointerY: number, transform: string, slid: boolean): string | undefined {
  const { drop, sortable } = buildTarget(transform, slid)
  const collisions = sidebarCollision({
    active: {
      id: DRAGGED,
      data: { current: { type: 'folder', subtree: [DRAGGED], siblings: [TARGET, DRAGGED] } },
      rect: { current: { initial: null, translated: null } },
    } as Active,
    collisionRect: rect(pointerY - 17, 0, 200, 34),
    droppableRects: new Map<string, ClientRect>([[drop.id as string, BLOCK], [sortable.id as string, BLOCK]]),
    droppableContainers: [drop, sortable],
    pointerCoordinates: { x: 100, y: pointerY },
  })
  return collisions[0]?.data?.droppableContainer?.data?.current?.type
}

const SLID = `translate3d(0px, ${SLIDE}px, 0) scaleX(1) scaleY(1)`

describe('root folder nest band holds still under the reorder slide', () => {
  it('a pointer on the slid header (the first session row of the unslid block) reorders in both states', () => {
    const y = BLOCK.top + SLIDE + HEADER_H / 2
    expect(resolve(y, SLID, true)).toBe('folder')
    expect(resolve(y, 'none', false)).toBe('folder')
  })

  it('a pointer on the middle of the unslid header nests in both states', () => {
    const y = BLOCK.top + HEADER_H / 2
    expect(resolve(y, SLID, true)).toBe('folder-drop')
    expect(resolve(y, 'none', false)).toBe('folder-drop')
  })

  it('reads the slide from a matrix() transform as well', () => {
    const y = BLOCK.top + HEADER_H / 2
    expect(resolve(y, `matrix(1, 0, 0, 1, 0, ${SLIDE})`, true)).toBe('folder-drop')
    expect(resolve(BLOCK.top + SLIDE + HEADER_H / 2, `matrix(1, 0, 0, 1, 0, ${SLIDE})`, true)).toBe('folder')
  })

  it('the header edges stay reorder targets in the unslid frame', () => {
    expect(resolve(BLOCK.top + 3, SLID, true)).toBe('folder')
    expect(resolve(BLOCK.top + HEADER_H - 3, SLID, true)).toBe('folder')
  })
})
