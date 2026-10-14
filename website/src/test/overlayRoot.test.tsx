import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { useState, type ReactNode } from 'react'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { LocalOverlayRoot, OverlayPortal, useLocalCrewHidden } from '../components/overlayRoot'
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/popover'
import { Dialog, DialogContent, DialogTitle } from '../components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../components/ui/select'
import Modal from '../components/Modal'

function Counter() {
  const [n, setN] = useState(0)
  return <button type="button" onClick={() => setN(n + 1)}>count {n}</button>
}

function HiddenProbe() {
  return <span data-testid="hidden-probe">{String(useLocalCrewHidden())}</span>
}

const localRoot = () => document.querySelector<HTMLElement>('[data-local-overlay-root]')

describe('overlay root', () => {
  it('portals to document.body outside the local pane', () => {
    render(<OverlayPortal><div data-testid="layer" /></OverlayPortal>)
    expect(screen.getByTestId('layer').parentElement).toBe(document.body)
  })

  it('reports the Crew as showing outside the local pane', () => {
    render(<HiddenProbe />)
    expect(screen.getByTestId('hidden-probe')).toHaveTextContent('false')
  })

  it('portals local layers into one body-level container', () => {
    render(
      <LocalOverlayRoot hidden={false}>
        <OverlayPortal><div data-testid="a" /></OverlayPortal>
        <OverlayPortal><div data-testid="b" /></OverlayPortal>
      </LocalOverlayRoot>,
    )
    const root = localRoot()
    expect(root?.parentElement).toBe(document.body)
    expect(screen.getByTestId('a').parentElement).toBe(root)
    expect(screen.getByTestId('b').parentElement).toBe(root)
  })

  it('hides and returns its layers with their state across a Crew switch', () => {
    const view = render(
      <LocalOverlayRoot hidden={false}>
        <HiddenProbe />
        <OverlayPortal><Counter /></OverlayPortal>
      </LocalOverlayRoot>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'count 0' }))
    const button = screen.getByRole('button', { name: 'count 1' })
    expect(button).toBeVisible()

    view.rerender(
      <LocalOverlayRoot hidden>
        <HiddenProbe />
        <OverlayPortal><Counter /></OverlayPortal>
      </LocalOverlayRoot>,
    )
    expect(button).not.toBeVisible()
    expect(screen.getByTestId('hidden-probe')).toHaveTextContent('true')

    view.rerender(
      <LocalOverlayRoot hidden={false}>
        <HiddenProbe />
        <OverlayPortal><Counter /></OverlayPortal>
      </LocalOverlayRoot>,
    )
    expect(screen.getByRole('button', { name: 'count 1' })).toBe(button)
    expect(button).toBeVisible()
  })

  it('removes its container on unmount', () => {
    const view = render(<LocalOverlayRoot hidden={false}><span /></LocalOverlayRoot>)
    expect(localRoot()).not.toBeNull()
    view.unmount()
    expect(localRoot()).toBeNull()
  })

  it('routes the shared Radix wrappers into the owning pane', () => {
    render(
      <LocalOverlayRoot hidden={false}>
        <Popover open>
          <PopoverTrigger>open</PopoverTrigger>
          <PopoverContent>popover body</PopoverContent>
        </Popover>
      </LocalOverlayRoot>,
    )
    expect(localRoot()).toContainElement(screen.getByText('popover body'))
  })

  it('leaves a Radix dialog on document.body, so it never holds the page from out of sight', async () => {
    render(
      <LocalOverlayRoot hidden>
        <Dialog open>
          <DialogContent aria-label="Draft"><DialogTitle>Draft</DialogTitle></DialogContent>
        </Dialog>
      </LocalOverlayRoot>,
    )
    const dialog = await screen.findByRole('dialog', { name: 'Draft' })
    expect(localRoot()).not.toContainElement(dialog)
  })

  it('keeps what a Radix dialog opens on document.body too, stacked above it', async () => {
    const ui = (confirmOpen: boolean) => (
      <LocalOverlayRoot hidden>
        <Dialog open>
          <DialogContent aria-label="Editor">
            <DialogTitle>Editor</DialogTitle>
            <Modal open={confirmOpen} onClose={() => {}} title="Confirm">sure?</Modal>
          </DialogContent>
        </Dialog>
      </LocalOverlayRoot>
    )
    const view = render(ui(false))
    const editor = await screen.findByRole('dialog', { name: 'Editor' })
    view.rerender(ui(true))
    const confirm = await screen.findByRole('dialog', { name: 'Confirm' })
    expect(localRoot()).not.toContainElement(confirm)
    // Later in document order paints above at equal z-index.
    expect(editor.compareDocumentPosition(confirm) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('keeps a hidden popover mounted with its input, refusing Escape and outside clicks', async () => {
    const onOpenChange = vi.fn()
    const ui = (hidden: boolean) => (
      <LocalOverlayRoot hidden={hidden}>
        <Popover open onOpenChange={onOpenChange}>
          <PopoverTrigger>open</PopoverTrigger>
          <PopoverContent><input aria-label="Goal" defaultValue="" /></PopoverContent>
        </Popover>
      </LocalOverlayRoot>
    )
    const view = render(ui(false))
    const input = await screen.findByLabelText('Goal')
    fireEvent.change(input, { target: { value: 'unsaved goal' } })
    view.rerender(ui(true))
    fireEvent.keyDown(document.body, { key: 'Escape' })
    fireEvent.pointerDown(document.body)
    expect(onOpenChange).not.toHaveBeenCalled()

    view.rerender(ui(false))
    expect(screen.getByLabelText('Goal')).toBe(input)
    expect(input).toHaveValue('unsaved goal')
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('keeps a modal popover outside a dialog from locking the page', async () => {
    render(
      <LocalOverlayRoot hidden={false}>
        <Popover open modal>
          <PopoverTrigger>open</PopoverTrigger>
          <PopoverContent>page popover</PopoverContent>
        </Popover>
      </LocalOverlayRoot>,
    )
    await screen.findByText('page popover')
    expect(document.body.style.pointerEvents).not.toBe('none')
  })

  it('honours a modal popover inside a dialog', async () => {
    render(
      <Dialog open>
        <DialogContent aria-label="Editor">
          <DialogTitle>Editor</DialogTitle>
          <Popover open modal>
            <PopoverTrigger>open</PopoverTrigger>
            <PopoverContent>dialog popover</PopoverContent>
          </Popover>
        </DialogContent>
      </Dialog>,
    )
    await waitFor(() => expect(document.querySelector('[data-radix-popper-content-wrapper]')).not.toBeNull())
    // A modal popover hides the rest of the page, the dialog included.
    await waitFor(() => expect(document.querySelector('[role="dialog"][aria-label="Editor"]')?.closest('[aria-hidden="true"]')).not.toBeNull())
  })

  it('closes a hidden menu the way a click outside would', async () => {
    const onOpenChange = vi.fn()
    const ui = (hidden: boolean) => (
      <LocalOverlayRoot hidden={hidden}>
        <DropdownMenu open onOpenChange={onOpenChange}>
          <DropdownMenuTrigger>menu</DropdownMenuTrigger>
          <DropdownMenuContent><DropdownMenuItem>Item</DropdownMenuItem></DropdownMenuContent>
        </DropdownMenu>
      </LocalOverlayRoot>
    )
    const view = render(ui(false))
    await screen.findByRole('menu')
    view.rerender(ui(true))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('leaves a menu outside the local pane open across a switch', async () => {
    const onOpenChange = vi.fn()
    render(
      <>
        <LocalOverlayRoot hidden><span /></LocalOverlayRoot>
        <DropdownMenu open onOpenChange={onOpenChange}>
          <DropdownMenuTrigger>menu</DropdownMenuTrigger>
          <DropdownMenuContent><DropdownMenuItem>Item</DropdownMenuItem></DropdownMenuContent>
        </DropdownMenu>
      </>,
    )
    await screen.findByRole('menu')
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('stops a hidden picker list from claiming keys meant for the Crew in front', () => {
    const onChoose = vi.fn()
    const wrapper = (hidden: boolean) => ({ children }: { children: ReactNode }) => (
      <LocalOverlayRoot hidden={hidden}>{children}</LocalOverlayRoot>
    )
    const hiddenView = renderHook(() => useListKeyboardNav({ open: true, count: 2, onChoose, onClose: () => {} }), { wrapper: wrapper(true) })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onChoose).not.toHaveBeenCalled()
    hiddenView.unmount()

    renderHook(() => useListKeyboardNav({ open: true, count: 2, onChoose, onClose: () => {} }), { wrapper: wrapper(false) })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onChoose).toHaveBeenCalledTimes(1)
  })

  it('closes a hidden select list and keeps its value', async () => {
    const onOpenChange = vi.fn()
    const ui = (hidden: boolean) => (
      <LocalOverlayRoot hidden={hidden}>
        <Select open value="a" onOpenChange={onOpenChange}>
          <SelectTrigger aria-label="Pick"><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="a">Alpha</SelectItem></SelectContent>
        </Select>
      </LocalOverlayRoot>
    )
    const view = render(ui(false))
    expect(onOpenChange).not.toHaveBeenCalled()
    view.rerender(ui(true))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('keeps a hidden Modal open through Escape aimed at the Crew in front', () => {
    const onClose = vi.fn()
    const ui = (hidden: boolean) => (
      <LocalOverlayRoot hidden={hidden}>
        <Modal open onClose={onClose} title="Draft">body</Modal>
      </LocalOverlayRoot>
    )
    const view = render(ui(true))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()

    view.rerender(ui(false))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  // The tie overlayRoot.tsx documents: Modal and a Radix dialog share z-[100]
  // (backdrop) / z-[101] (panel), and the local root is attached at boot, so a
  // local Modal paints under a dialog opened later. Changing either z-index or
  // the attach order changes which one is on top; this makes that visible.
  it('pins the Modal / dialog z-index tie and the order that breaks it', () => {
    render(
      <>
        <LocalOverlayRoot hidden={false}>
          <Modal open onClose={() => {}} title="Local">body</Modal>
        </LocalOverlayRoot>
        <Dialog open>
          <DialogContent><DialogTitle>Global</DialogTitle></DialogContent>
        </Dialog>
      </>,
    )
    const root = localRoot()!
    for (const z of ['z-[100]', 'z-[101]']) {
      const layers = [...document.querySelectorAll<HTMLElement>(`[class~="${z}"]`)]
      const local = layers.filter(el => root.contains(el))
      const global = layers.filter(el => !root.contains(el))
      expect(local.length, `Modal ${z}`).toBeGreaterThan(0)
      expect(global.length, `dialog ${z}`).toBeGreaterThan(0)
      // At equal z-index, later in document order paints on top.
      expect(local[0].compareDocumentPosition(global[0]) & Node.DOCUMENT_POSITION_FOLLOWING, z).toBeTruthy()
    }
  })
})
