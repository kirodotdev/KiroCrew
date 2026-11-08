/**
 * Nothing inside a closed folder body is a drop target (#28543).
 *
 * A collapsed folder keeps its body mounted and hides it. dnd-kit picks targets
 * from measured boxes and ignores visibility, so the hidden subfolder's boxes
 * (its `folder-drop` block, its nested sortable row, its parent cue) lie over
 * the rows painted below the collapsed header. The issue's layout: P is
 * collapsed and holds S, and N sits below P. A drop aimed at the gap below P,
 * over S's hidden box, must never resolve to S.
 *
 * Each case puts the pointer inside both S's hidden box and a visible box, and
 * checks the collision resolves to the visible one (or to nothing), never S.
 * The nodes are mounted in a real FolderBody, so the cases follow the marker it
 * sets when closed (`inert`) rather than a copy of it.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import type { Active, ClientRect, DroppableContainer } from '@dnd-kit/core'
import { sidebarCollision } from '../pages/ChatSidebar'
import { FolderBody } from '../components/FolderBody'

function rect(top: number, left: number, width: number, height: number): ClientRect {
  return { top, left, width, height, right: left + width, bottom: top + height } as ClientRect
}

const P = 'folder-p'
const S = 'folder-s'
const N = 'folder-n'
const M = 'folder-m'
const HEADER_H = 34
// N's block, painted right below P's header. S's hidden box covers the same span.
const N_BLOCK = rect(134, 0, 200, 100)
const S_BLOCK = rect(134, 0, 200, 100)

let nextNode = 0
/** An element mounted inside a real FolderBody, closed or open, the way a
 *  collapsed folder holds its subfolders. */
function inFolderBody(closed: boolean): HTMLElement {
  const testId = `node-${nextNode++}`
  const { getByTestId } = render(<FolderBody open={!closed}><div data-testid={testId} /></FolderBody>)
  return getByTestId(testId)
}

/** A block element with a header row painted at `box`, inside a FolderBody. */
function blockNode(box: ClientRect, closed: boolean): HTMLElement {
  const block = inFolderBody(closed)
  const header = document.createElement('div')
  Object.defineProperty(header, 'getBoundingClientRect', { value: () => rect(box.top, box.left, box.width, HEADER_H) })
  block.appendChild(header)
  return block
}

function container(id: string, data: Record<string, unknown>, box: ClientRect, node: HTMLElement): DroppableContainer {
  return { id, key: id, data: { current: data }, rect: { current: box }, node: { current: node }, disabled: false } as unknown as DroppableContainer
}

function run(active: Record<string, unknown>, containers: DroppableContainer[], y: number) {
  return sidebarCollision({
    active: { id: 'dragged', data: { current: active }, rect: { current: { initial: null, translated: null } } } as Active,
    collisionRect: rect(y - 17, 0, 200, 34),
    droppableRects: new Map(containers.map(c => [c.id as string, c.rect.current as ClientRect])),
    droppableContainers: containers,
    pointerCoordinates: { x: 100, y },
  }).map(c => c.id)
}

afterEach(() => cleanup())

// The middle of the shared span: S's hidden header and N's visible header.
const Y = N_BLOCK.top + HEADER_H / 2

describe('a closed folder body holds no drop target', () => {
  it('a root folder drag does not nest into the hidden subfolder', () => {
    const hiddenDrop = container(`folder-drop:${S}`, { type: 'folder-drop', folderId: S }, S_BLOCK, blockNode(S_BLOCK, true))
    const nNode = blockNode(N_BLOCK, false)
    const visibleDrop = container(`folder-drop:${N}`, { type: 'folder-drop', folderId: N }, N_BLOCK, nNode)
    const nRow = container(N, { type: 'folder' }, N_BLOCK, nNode)
    const out = run({ type: 'folder', subtree: [M], siblings: [P, N, M] }, [hiddenDrop, visibleDrop, nRow], Y)
    expect(out).not.toContain(`folder-drop:${S}`)
    expect(out[0]).toBe(`folder-drop:${N}`)
  })

  it('a session drag does not resolve to the hidden subfolder block or its sortable row', () => {
    const sNode = blockNode(S_BLOCK, true)
    const hiddenDrop = container(`folder-drop:${S}`, { type: 'folder-drop', folderId: S }, S_BLOCK, sNode)
    const hiddenRow = container(S, { type: 'folder', nested: true }, S_BLOCK, sNode)
    const visibleDrop = container(`folder-drop:${N}`, { type: 'folder-drop', folderId: N }, N_BLOCK, blockNode(N_BLOCK, false))
    const out = run({ type: 'session' }, [hiddenDrop, hiddenRow, visibleDrop], Y + HEADER_H)
    expect(out).not.toContain(`folder-drop:${S}`)
    expect(out).not.toContain(S)
    expect(out[0]).toBe(`folder-drop:${N}`)
  })

  it('a session drag skips the hidden parent cue, which claims its own header rect', () => {
    const cue = inFolderBody(true)
    Object.defineProperty(cue, 'getBoundingClientRect', { value: () => rect(S_BLOCK.top, 0, 200, HEADER_H) })
    const hiddenCue = container(`parent-drop:${S}`, { type: 'folder-drop', folderId: S, headerHitRect: 'self', headerHitDepth: 1 }, rect(S_BLOCK.top, 0, 200, HEADER_H), cue)
    const visibleDrop = container(`folder-drop:${N}`, { type: 'folder-drop', folderId: N }, N_BLOCK, blockNode(N_BLOCK, false))
    const out = run({ type: 'session' }, [hiddenCue, visibleDrop], Y)
    expect(out[0]).toBe(`folder-drop:${N}`)
  })

  it('a nested folder drag does not re-parent into the hidden subfolder', () => {
    const hiddenDrop = container(`folder-drop:${S}`, { type: 'folder-drop', folderId: S }, S_BLOCK, blockNode(S_BLOCK, true))
    const out = run({ type: 'folder', nested: true, subtree: ['folder-x'], siblings: ['folder-x'] }, [hiddenDrop], Y + HEADER_H)
    expect(out).not.toContain(`folder-drop:${S}`)
  })

  it('the same target inside an OPEN body still takes the drop', () => {
    const openDrop = container(`folder-drop:${S}`, { type: 'folder-drop', folderId: S }, S_BLOCK, blockNode(S_BLOCK, false))
    expect(run({ type: 'session' }, [openDrop], Y + HEADER_H)[0]).toBe(`folder-drop:${S}`)
  })
})
