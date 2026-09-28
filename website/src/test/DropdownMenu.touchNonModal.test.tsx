// Regression: on a phone, dashboard controls needed two taps whenever a
// dropdown menu was open. A modal Radix menu sets `pointer-events: none` on
// `document.body`, so the first tap outside only closed the menu and the
// control under the finger never saw it. The shared `DropdownMenu` wrapper now
// defaults to non-modal on touch devices: the outside tap reaches its target
// and closes the menu in the same tap. Mouse devices keep the modal default,
// and an explicit `modal` prop wins everywhere.
//
// happy-dom does not enforce `pointer-events`, so "the tap reaches the target"
// is asserted two ways: the body is not blocked, and the target's click
// handler runs while the menu closes.
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'

function stubPointer(kind: 'touch' | 'mouse'): void {
  vi.spyOn(window, 'matchMedia').mockImplementation((query: string) => ({
    matches: kind === 'touch' && (query === '(pointer: coarse)' || query === '(hover: none)'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList)
}

function Harness({ modal, onOutside, onOpenChange }: {
  modal?: boolean
  onOutside?: () => void
  onOpenChange?: (open: boolean) => void
}) {
  return (
    <>
      <button type="button" data-testid="new-session" onClick={onOutside}>New</button>
      <DropdownMenu defaultOpen modal={modal} onOpenChange={onOpenChange}>
        <DropdownMenuTrigger data-testid="trigger">more</DropdownMenuTrigger>
        <DropdownMenuContent data-testid="content">
          <DropdownMenuItem>Rename</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  )
}

// A finger tap outside the menu: Radix waits for the tap's `click` before it
// dismisses a touch pointer-down, so both events are needed.
function tapOutside(target: HTMLElement): void {
  fireEvent.pointerDown(target, { pointerType: 'touch', button: 0 })
  fireEvent.pointerUp(target, { pointerType: 'touch', button: 0 })
  fireEvent.click(target)
}

// Radix arms its outside-pointer listener on a timeout after the menu opens.
async function letMenuSettle(): Promise<void> {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('DropdownMenu modal default follows the pointer type', () => {
  it('on a touch device, leaves the page tappable and one outside tap both activates the target and closes the menu', async () => {
    stubPointer('touch')
    const onOutside = vi.fn()
    const onOpenChange = vi.fn()
    render(<Harness onOutside={onOutside} onOpenChange={onOpenChange} />)
    expect(screen.getByTestId('content')).toBeInTheDocument()
    await letMenuSettle()

    expect(document.body.style.pointerEvents).not.toBe('none')

    tapOutside(screen.getByTestId('new-session'))

    expect(onOutside).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByTestId('content')).not.toBeInTheDocument())
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('on a mouse device, stays modal: the page behind the open menu is blocked', async () => {
    stubPointer('mouse')
    render(<Harness />)
    expect(screen.getByTestId('content')).toBeInTheDocument()
    await letMenuSettle()

    expect(document.body.style.pointerEvents).toBe('none')
  })

  it('keeps an explicit modal={true} on a touch device', async () => {
    stubPointer('touch')
    render(<Harness modal />)
    await letMenuSettle()

    expect(document.body.style.pointerEvents).toBe('none')
  })

  it('keeps an explicit modal={false} on a mouse device', async () => {
    stubPointer('mouse')
    render(<Harness modal={false} />)
    await letMenuSettle()

    expect(document.body.style.pointerEvents).not.toBe('none')
  })

  it('on a touch device, choosing an item still closes the menu', async () => {
    stubPointer('touch')
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)
    await letMenuSettle()

    fireEvent.click(screen.getByText('Rename'))

    await waitFor(() => expect(screen.queryByTestId('content')).not.toBeInTheDocument())
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
