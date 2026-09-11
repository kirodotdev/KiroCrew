/**
 * Parity pin: the Sessions sidebar's column grip and the Crew Members roster's
 * column grip are the SAME component, rendering the same bar.
 *
 * The two drifted once — the sidebar carried an inlined 2px accent pill while
 * the roster went through a `ResizeHandle` that painted a flat translucent
 * strip — and a user spotted the difference between pages. This test keeps
 * both surfaces on the shared component and pins the visual recipe that
 * component renders, so a future "just tweak the sidebar's bar" lands in the
 * shared file or fails here.
 *
 * Mutation-verified: inlining a `role="separator" aria-orientation="vertical"`
 * back into either page fails the source pin; dropping `rounded-full` or the
 * hover/active accent classes from the bar fails the recipe pin.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import ResizeHandle from '../components/ResizeHandle'
import type { usePointerDrag } from '../hooks/usePointerDrag'

type HandleProps = ReturnType<typeof usePointerDrag>

const NOOP_HANDLE: HandleProps = {
  onPointerDown: () => {},
  onPointerMove: () => {},
  onPointerUp: () => {},
  onPointerCancel: () => {},
  onLostPointerCapture: () => {},
}

const SURFACES: ReadonlyArray<readonly [name: string, file: string]> = [
  ['ChatSidebar', join(process.cwd(), 'src', 'pages', 'ChatSidebar.tsx')],
  ['MembersPage', join(process.cwd(), 'src', 'pages', 'members', 'MembersPage.tsx')],
]

describe('column resize grip — one component on both pages', () => {
  it.each(SURFACES)('%s mounts the shared ResizeHandle and hand-rolls no vertical separator', (_name, file) => {
    const src = readFileSync(file, 'utf8')
    expect(src).toMatch(/import ResizeHandle from '(\.\.\/)+components\/ResizeHandle'/)
    expect(src).toContain('<ResizeHandle')
    // A raw vertical separator in the page is the drift this pin forbids. The
    // sidebar's horizontal history-pane splitter is a different affordance and
    // stays out of scope, hence the orientation in the pattern.
    expect(src).not.toMatch(/aria-orientation="vertical"/)
  })

  it('renders the sidebar bar recipe: 6px hit strip carrying a 2px rounded accent-on-hover bar', () => {
    const { getByRole, getByTestId } = render(
      <ResizeHandle handleProps={NOOP_HANDLE} label="Resize member list" inset={12} />,
    )
    const strip = getByRole('separator', { name: 'Resize member list' })
    expect(strip.getAttribute('aria-orientation')).toBe('vertical')
    expect(strip.style.touchAction).toBe('none')
    expect(strip.className).toMatch(/\bw-1\.5\b/)
    expect(strip.className).toMatch(/\bgroup\/drag\b/)
    expect(strip.className).toMatch(/\bcursor-col-resize\b/)

    const bar = getByTestId('resize-handle-bar')
    expect(bar.getAttribute('aria-hidden')).toBe('true')
    for (const cls of ['w-[2px]', 'rounded-full', 'bg-transparent', 'group-hover/drag:bg-accent', 'group-active/drag:bg-accent-hover', 'group-focus-visible/drag:bg-accent', 'resize-accent']) {
      expect(bar.className.split(/\s+/)).toContain(cls)
    }
    // The inset is the host card's corner radius, trimmed from BOTH ends.
    expect(bar.style.height).toBe('calc(100% - 24px)')
  })

  it('no inset → the bar spans the full strip', () => {
    const { getByTestId } = render(<ResizeHandle handleProps={NOOP_HANDLE} label="x" />)
    expect(getByTestId('resize-handle-bar').style.height).toBe('100%')
  })

  it('className overrides the hit strip placement without touching the bar', () => {
    const { getByRole, getByTestId } = render(
      <ResizeHandle handleProps={NOOP_HANDLE} label="x" className="sidebar-resize-handle absolute top-0 -right-[3px] h-full z-10" />,
    )
    const strip = getByRole('separator')
    expect(strip.className.split(/\s+/)).toEqual(expect.arrayContaining(['sidebar-resize-handle', 'absolute', 'h-full']))
    // tailwind-merge keeps the default width when the caller sets none.
    expect(strip.className).toMatch(/\bw-1\.5\b/)
    expect(getByTestId('resize-handle-bar').className).toContain('w-[2px]')
  })

  it('is a window splitter only when it can be operated: tab stop + arrow keys follow onNudge', () => {
    const onNudge = vi.fn()
    const { getByRole, rerender } = render(<ResizeHandle handleProps={NOOP_HANDLE} label="x" onNudge={onNudge} />)
    const strip = getByRole('separator')
    expect(strip.tabIndex).toBe(0)
    fireEvent.keyDown(strip, { key: 'ArrowRight' })
    fireEvent.keyDown(strip, { key: 'ArrowLeft', shiftKey: true })
    fireEvent.keyDown(strip, { key: 'ArrowUp' })
    expect(onNudge.mock.calls).toEqual([[16], [-64]])

    rerender(<ResizeHandle handleProps={NOOP_HANDLE} label="x" />)
    expect(getByRole('separator').hasAttribute('tabindex')).toBe(false)
  })
})
