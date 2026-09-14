/**
 * On a touch screen the Move to folder picker renders inline beneath its
 * trigger, inside a menu that is already capped to the viewport and scrolls.
 * The picker's desktop flyout cap (a fixed max height with its own scroll) must
 * not survive into that inline body: there it made a second scroll box that
 * ended at a row boundary, so the list looked complete while later folders sat
 * behind an inner scroll that scrolling the menu never revealed.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, cleanup } from '@testing-library/react'
import { renderWithProviders, createConnectedTestStore } from './helpers'
import FolderMoveSubmenu from '../components/FolderMoveSubmenu'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from '../components/ui/context-menu'
import type { ChatFolder } from '../types'

const { device } = vi.hoisted(() => ({ device: { touch: true } }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => device.touch }))

afterEach(() => {
  cleanup()
  device.touch = true
})

function folders(): ChatFolder[] {
  const top = Array.from({ length: 14 }, (_, i) => ({ id: `f${i}`, name: `Folder ${i}`, order: i, parent_id: '' }))
  const nested = Array.from({ length: 5 }, (_, i) => ({ id: `n${i}`, name: `Child ${i}`, order: i, parent_id: 'f2' }))
  return [...top, ...nested] as unknown as ChatFolder[]
}

/** Every ancestor of `el` up to (not including) the menu content. */
function ancestorsBelowMenu(el: HTMLElement): HTMLElement[] {
  const out: HTMLElement[] = []
  let cur = el.parentElement
  while (cur && cur.getAttribute('role') !== 'menu') {
    out.push(cur)
    cur = cur.parentElement
  }
  return out
}

const SCROLL_OR_CAP = /\bmax-h-|\boverflow-(y-)?(auto|scroll|hidden)\b/

/**
 * No box between the row and the menu may clip or scroll. jsdom applies no
 * stylesheet, so a utility class counts as in effect unless an inline style
 * overrides it, which is how the inline body neutralises a caller's cap.
 */
function expectNoInnerScrollBox(row: HTMLElement) {
  for (const a of ancestorsBelowMenu(row)) {
    if (!SCROLL_OR_CAP.test(a.className)) continue
    expect(a.style.maxHeight).toBe('none')
    expect(a.style.overflow).toBe('visible')
  }
}

describe('FolderMoveSubmenu inline on touch', () => {
  it('chat-title dropdown: every folder sits in the menu scroll, not a capped inner box', () => {
    const onPick = vi.fn()
    renderWithProviders(
      <DropdownMenu open>
        <DropdownMenuTrigger>Title</DropdownMenuTrigger>
        <DropdownMenuContent>
          <FolderMoveSubmenu variant="dropdown" folders={folders()} onPick={onPick} />
        </DropdownMenuContent>
      </DropdownMenu>,
      { store: createConnectedTestStore() },
    )
    fireEvent.click(screen.getByRole('button', { name: /Move to folder/ }))
    const items = screen.getAllByRole('menuitem')
    expect(items).toHaveLength(20)
    const last = items[items.length - 1]
    expectNoInnerScrollBox(last)
    fireEvent.click(last)
    expect(onPick).toHaveBeenCalledWith('f13')
  })

  it('nested folders are listed and pickable from the inline body', () => {
    const onPick = vi.fn()
    renderWithProviders(
      <DropdownMenu open>
        <DropdownMenuTrigger>Title</DropdownMenuTrigger>
        <DropdownMenuContent>
          <FolderMoveSubmenu variant="dropdown" folders={folders()} onPick={onPick} />
        </DropdownMenuContent>
      </DropdownMenu>,
      { store: createConnectedTestStore() },
    )
    fireEvent.click(screen.getByRole('button', { name: /Move to folder/ }))
    const child = screen.getByRole('menuitem', { name: /Folder 2 \/ Child 4|Child 4/ })
    expectNoInnerScrollBox(child)
    fireEvent.click(child)
    expect(onPick).toHaveBeenCalledWith('n4')
  })

  it('sessions-list context menu: the same inline body has no capped inner box', () => {
    const onPick = vi.fn()
    renderWithProviders(
      <ContextMenu>
        <ContextMenuTrigger>Row</ContextMenuTrigger>
        <ContextMenuContent>
          <FolderMoveSubmenu variant="context" folders={folders()} onPick={onPick} />
        </ContextMenuContent>
      </ContextMenu>,
      { store: createConnectedTestStore() },
    )
    fireEvent.contextMenu(screen.getByText('Row'))
    fireEvent.click(screen.getByRole('button', { name: /Move to folder/ }))
    const items = screen.getAllByRole('menuitem')
    const last = items[items.length - 1]
    expectNoInnerScrollBox(last)
    fireEvent.click(last)
    expect(onPick).toHaveBeenCalledWith('f13')
  })
})
