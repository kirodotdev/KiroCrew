// ContextMenuStableTrigger hands a rebuilt trigger element to Radix through a
// fixed outlet, so the menu tree skips the owner's re-renders. These tests pin
// that the element still behaves as a plain `ContextMenuTrigger asChild` child:
// right-click opens the menu, the element's own handlers and ref still run, a
// re-render updates the same DOM node without remounting it, and focus returns
// to it when the menu closes.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ContextMenuContent, ContextMenuItem, ContextMenuStableTrigger } from '../components/ui/context-menu'

const CONTENT = (
  <ContextMenuContent data-testid="menu">
    <ContextMenuItem>Rename</ContextMenuItem>
  </ContextMenuContent>
)

let setLabel: (label: string) => void = () => {}

function Row({ onContextMenu, onPointerDown, rowRef }: {
  onContextMenu?: (e: React.MouseEvent) => void
  onPointerDown?: (e: React.PointerEvent) => void
  rowRef?: (node: HTMLDivElement | null) => void
}) {
  const [label, set] = useState('first')
  setLabel = set
  return (
    <ContextMenuStableTrigger content={CONTENT}>
      <div data-testid="row" role="button" tabIndex={0} className={`row-${label}`} ref={rowRef}
        onContextMenu={onContextMenu} onPointerDown={onPointerDown}>
        {label}
      </div>
    </ContextMenuStableTrigger>
  )
}

afterEach(cleanup)

describe('ContextMenuStableTrigger', () => {
  it('opens on right-click and runs the element\'s own handlers too', () => {
    const onContextMenu = vi.fn()
    const onPointerDown = vi.fn()
    render(<Row onContextMenu={onContextMenu} onPointerDown={onPointerDown} />)
    const row = screen.getByTestId('row')
    expect(row.getAttribute('data-state')).toBe('closed')

    fireEvent.pointerDown(row, { pointerType: 'mouse', button: 2 })
    fireEvent.contextMenu(row, { clientX: 40, clientY: 20 })

    expect(onPointerDown).toHaveBeenCalledTimes(1)
    expect(onContextMenu).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toBeTruthy()
    expect(row.getAttribute('data-state')).toBe('open')
  })

  it('keeps the same DOM node and its ref across a re-render, and still opens after it', () => {
    const rowRef = vi.fn()
    render(<Row rowRef={rowRef} />)
    const row = screen.getByTestId('row')

    act(() => { setLabel('second') })

    expect(screen.getByTestId('row')).toBe(row)
    expect(row.className).toBe('row-second')
    expect(row.textContent).toBe('second')
    expect(rowRef).toHaveBeenCalledWith(row)
    expect(rowRef).not.toHaveBeenCalledWith(null)
    fireEvent.contextMenu(row, { clientX: 40, clientY: 20 })
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toBeTruthy()
  })

  it('returns focus to the element when Escape closes a keyboard-opened menu', async () => {
    render(<Row />)
    const row = screen.getByTestId('row')
    row.focus()
    // The keyboard's menu key and Shift+F10 reach the page as a contextmenu
    // event on the focused element.
    fireEvent.contextMenu(row)
    const menu = screen.getByTestId('menu')
    expect(menu.contains(document.activeElement)).toBe(true)

    fireEvent.keyDown(menu, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('menu')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(row))
  })

})
