// Regression: a session/context menu taller than the space below its trigger
// must stay fully reachable on a short (mobile) viewport. Before this fix the
// shared Radix Content wrappers used `overflow-hidden` with no height cap, so
// the bottom items (e.g. "Close session") were clipped off-screen with no way
// to scroll or reposition. The fix caps the Content to the height Radix
// measured as available between the trigger and the viewport edge (its own
// collision CSS var) and scrolls the overflow.
//
// jsdom/happy-dom compute no layout and do not evaluate Radix's collision
// vars, so we assert the durable structural contract — the classes that make
// the menu fit and scroll — rather than a pixel-measured scroll, which would
// be flaky here.
import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent } from '../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from '../components/ui/context-menu'

function classesOf(el: Element | null): string {
  return el?.getAttribute('class') ?? ''
}

describe('menu Content overflow on a short viewport', () => {
  it('DropdownMenuContent caps to the available height and scrolls, never clips', () => {
    const { getByTestId } = render(
      <DropdownMenu open>
        <DropdownMenuContent forceMount data-testid="ddc">
          <DropdownMenuItem>Close session</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    )
    const cls = classesOf(getByTestId('ddc'))
    expect(cls).toContain('max-h-[var(--radix-dropdown-menu-content-available-height)]')
    expect(cls).toContain('overflow-y-auto')
    expect(cls).not.toContain('overflow-hidden')
  })

  it('DropdownMenuSubContent is height-capped and scrollable too', () => {
    const { getByTestId } = render(
      <DropdownMenu open>
        <DropdownMenuContent forceMount>
          <DropdownMenuSub open>
            <DropdownMenuSubTrigger>More</DropdownMenuSubTrigger>
            <DropdownMenuSubContent forceMount data-testid="dds">
              <DropdownMenuItem>Deep item</DropdownMenuItem>
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        </DropdownMenuContent>
      </DropdownMenu>,
    )
    const cls = classesOf(getByTestId('dds'))
    expect(cls).toContain('max-h-[var(--radix-dropdown-menu-content-available-height)]')
    expect(cls).toContain('overflow-y-auto')
    expect(cls).not.toContain('overflow-hidden')
  })

  it('ContextMenuContent caps to the available height and scrolls, never clips', () => {
    // ContextMenu Root has no `open` prop — it opens on a contextmenu event
    // against its trigger, so fire that to mount the portal content.
    const { getByTestId } = render(
      <ContextMenu>
        <ContextMenuTrigger data-testid="cmt">row</ContextMenuTrigger>
        <ContextMenuContent forceMount data-testid="cmc">
          <ContextMenuItem>Close session</ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>,
    )
    fireEvent.contextMenu(getByTestId('cmt'))
    const cls = classesOf(getByTestId('cmc'))
    expect(cls).toContain('max-h-[var(--radix-context-menu-content-available-height)]')
    expect(cls).toContain('overflow-y-auto')
    expect(cls).not.toContain('overflow-hidden')
  })
})
