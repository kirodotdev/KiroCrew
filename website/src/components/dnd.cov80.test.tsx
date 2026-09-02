import { render, screen } from '@testing-library/react'
import { DndContext } from '@dnd-kit/core'
import { folderAwareCollision, DndDraggable, DndDroppable } from './dnd'

const closestCenter = vi.hoisted(() => vi.fn(() => [{ id: 'closest' }]))
const pointerWithin = vi.hoisted(() => vi.fn(() => [{ id: 'within' }]))

vi.mock('@dnd-kit/core', async importOriginal => {
  const mod = await importOriginal<typeof import('@dnd-kit/core')>()
  return { ...mod, closestCenter, pointerWithin }
})

type Args = Parameters<typeof folderAwareCollision>[0]

function container(type: string, id: string) {
  return { id, data: { current: { type } } }
}

function args(activeType: string | undefined, containers: unknown[]): Args {
  return {
    active: { id: 'a', data: { current: activeType ? { type: activeType } : {} } },
    droppableContainers: containers,
  } as unknown as Args
}

describe('folderAwareCollision', () => {
  beforeEach(() => {
    closestCenter.mockClear().mockReturnValue([{ id: 'closest' }] as never)
    pointerWithin.mockClear().mockReturnValue([{ id: 'within' }] as never)
  })

  it('dragging a folder restricts collisions to folder containers', () => {
    const folder = container('folder', 'f1')
    const item = container('item', 'i1')
    const out = folderAwareCollision(args('folder', [folder, item]))

    expect(out).toEqual([{ id: 'closest' }])
    expect(pointerWithin).not.toHaveBeenCalled()
    expect(closestCenter.mock.calls[0][0].droppableContainers).toEqual([folder])
  })

  it('dragging an item prefers the innermost droppable under the pointer', () => {
    const out = folderAwareCollision(args('item', [container('folder', 'f1')]))
    expect(out).toEqual([{ id: 'within' }])
    expect(closestCenter).not.toHaveBeenCalled()
  })

  it('falls back to closestCenter when nothing is under the pointer', () => {
    pointerWithin.mockReturnValue([] as never)
    const out = folderAwareCollision(args('item', [container('folder', 'f1')]))
    expect(out).toEqual([{ id: 'closest' }])
  })

  it('an active node with no type is treated as an item, not a folder', () => {
    const out = folderAwareCollision(args(undefined, [container('folder', 'f1')]))
    expect(out).toEqual([{ id: 'within' }])
  })
})

describe('DndDraggable / DndDroppable', () => {
  it('DndDraggable hands the render prop a ref, listeners and a drag flag', () => {
    render(
      <DndContext>
        <DndDraggable id="zzq-d" data={{ type: 'item' }}>
          {({ setNodeRef, listeners, attributes, isDragging }) => (
            <button ref={setNodeRef} {...attributes} {...listeners} data-dragging={isDragging}>
              zzq-drag
            </button>
          )}
        </DndDraggable>
      </DndContext>,
    )
    const el = screen.getByRole('button', { name: 'zzq-drag' })
    expect(el.getAttribute('data-dragging')).toBe('false')
    // dnd-kit attaches its own a11y wiring through `attributes`.
    expect(el.getAttribute('aria-roledescription')).toBeTruthy()
  })

  it('DndDroppable hands the render prop a ref and an isOver flag', () => {
    render(
      <DndContext>
        <DndDroppable id="zzq-t" data={{ type: 'folder' }}>
          {({ setNodeRef, isOver }) => (
            <div ref={setNodeRef} data-over={isOver}>zzq-drop</div>
          )}
        </DndDroppable>
      </DndContext>,
    )
    expect(screen.getByText('zzq-drop').getAttribute('data-over')).toBe('false')
  })
})
