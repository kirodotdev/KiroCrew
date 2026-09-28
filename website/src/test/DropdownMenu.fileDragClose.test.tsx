// Regression: dropping a file into the chat while a sidebar "..." menu was open
// did nothing. A modal Radix DropdownMenu puts `pointer-events: none` on
// `document.body` while open, drag events honour it, and a native file drag
// never produces the outside `pointerdown` that would dismiss the menu. The
// shared `DropdownMenu` wrapper now closes an open modal menu when a file drag
// enters the window, so the composer's drop zone receives the following
// `dragover`/`drop`.
//
// happy-dom has no `DragEvent`, so the drag is a plain `Event` with a
// `dataTransfer` attached the same way the browser exposes it.
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuTrigger } from '../components/ui/context-menu'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

function windowDragEnter(types: string[]): void {
  const event = new Event('dragenter', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'dataTransfer', {
    value: { types, items: [], files: [], dropEffect: 'none' },
  })
  window.dispatchEvent(event)
}

function Uncontrolled({ onOpenChange, modal }: { onOpenChange?: (open: boolean) => void; modal?: boolean }) {
  return (
    <DropdownMenu defaultOpen onOpenChange={onOpenChange} modal={modal}>
      <DropdownMenuTrigger data-testid="trigger">more</DropdownMenuTrigger>
      <DropdownMenuContent data-testid="content">
        <DropdownMenuItem>Rename</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function Controlled({ onOpenChange }: { onOpenChange: (open: boolean) => void }) {
  const [open, setOpen] = useState(true)
  return (
    <DropdownMenu open={open} onOpenChange={(next) => { setOpen(next); onOpenChange(next) }}>
      <DropdownMenuTrigger data-testid="trigger">more</DropdownMenuTrigger>
      <DropdownMenuContent data-testid="content">
        <DropdownMenuItem>Rename</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

describe('DropdownMenu closes when a file drag enters the window', () => {
  it('dismisses an open uncontrolled menu and reports it through onOpenChange', async () => {
    const onOpenChange = vi.fn()
    render(<Uncontrolled onOpenChange={onOpenChange} />)
    expect(screen.getByTestId('content')).toBeInTheDocument()

    windowDragEnter(['Files'])

    await waitFor(() => expect(screen.queryByTestId('content')).not.toBeInTheDocument())
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('dismisses an open controlled menu through onOpenChange', async () => {
    const onOpenChange = vi.fn()
    render(<Controlled onOpenChange={onOpenChange} />)
    expect(screen.getByTestId('content')).toBeInTheDocument()

    windowDragEnter(['Files'])

    await waitFor(() => expect(screen.queryByTestId('content')).not.toBeInTheDocument())
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("ignores the app's own in-page drags (session rows, folder moves)", () => {
    const onOpenChange = vi.fn()
    render(<Uncontrolled onOpenChange={onOpenChange} />)

    windowDragEnter(['text/plain'])
    windowDragEnter(['application/x-kiro-session'])

    expect(screen.getByTestId('content')).toBeInTheDocument()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('leaves a non-modal menu alone: it never blocks the drop zone', () => {
    const onOpenChange = vi.fn()
    render(<Uncontrolled onOpenChange={onOpenChange} modal={false} />)

    windowDragEnter(['Files'])

    expect(screen.getByTestId('content')).toBeInTheDocument()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('stops listening once the menu is closed by other means', async () => {
    const onOpenChange = vi.fn()
    render(<Uncontrolled onOpenChange={onOpenChange} />)

    fireEvent.keyDown(screen.getByTestId('content'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('content')).not.toBeInTheDocument())
    onOpenChange.mockClear()

    windowDragEnter(['Files'])
    expect(onOpenChange).not.toHaveBeenCalled()
  })
})

describe('ContextMenu closes when a file drag enters the window', () => {
  function Harness({ onOpenChange }: { onOpenChange: (open: boolean) => void }) {
    return (
      <ContextMenu onOpenChange={onOpenChange}>
        <ContextMenuTrigger data-testid="row">row</ContextMenuTrigger>
        <ContextMenuContent data-testid="content">
          <ContextMenuItem>Close session</ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>
    )
  }

  it('dismisses a right-click menu and reports it through onOpenChange', async () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)
    fireEvent.contextMenu(screen.getByTestId('row'))
    expect(screen.getByTestId('content')).toBeInTheDocument()
    expect(onOpenChange).toHaveBeenCalledWith(true)

    windowDragEnter(['Files'])

    await waitFor(() => expect(screen.queryByTestId('content')).not.toBeInTheDocument())
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
  })

  it("ignores the app's own in-page drags", () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)
    fireEvent.contextMenu(screen.getByTestId('row'))
    onOpenChange.mockClear()

    windowDragEnter(['text/plain'])

    expect(screen.getByTestId('content')).toBeInTheDocument()
    expect(onOpenChange).not.toHaveBeenCalled()
  })
})
