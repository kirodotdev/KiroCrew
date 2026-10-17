import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { APPS_FLOOR_PX, AdaptiveMobileRail, shouldFoldSecondary, stableRailHeight } from '../shell/nav/adaptiveMobileRail'

describe('shouldFoldSecondary', () => {
  it('keeps the pins while the Apps list still gets its floor', () => {
    expect(shouldFoldSecondary(1000, 300, 240, 120, )).toBe(false)
    expect(shouldFoldSecondary(300 + 240 + 120 + APPS_FLOOR_PX, 300, 240, 120)).toBe(false)
  })
  it('folds once pinning would leave the Apps list under the floor', () => {
    expect(shouldFoldSecondary(300 + 240 + 120 + APPS_FLOOR_PX - 1, 300, 240, 120)).toBe(true)
    expect(shouldFoldSecondary(740, 300, 240, 120)).toBe(true)
  })
  it('keeps the shipped pinned layout before the rail is measured', () => {
    expect(shouldFoldSecondary(0, 300, 240, 120)).toBe(false)
  })
})

describe('stableRailHeight', () => {
  it('ignores a shrink at the same width (keyboard, browser toolbar)', () => {
    const tall = stableRailHeight(null, 72, 1000)
    expect(stableRailHeight(tall, 72, 640)).toEqual({ width: 72, height: 1000 })
  })
  it('adopts growth at the same width', () => {
    expect(stableRailHeight({ width: 72, height: 700 }, 72, 900)).toEqual({ width: 72, height: 900 })
  })
  it('re-decides from scratch when the width changes (rotation)', () => {
    expect(stableRailHeight({ width: 72, height: 1000 }, 80, 500)).toEqual({ width: 80, height: 500 })
  })
  it('keeps pinned tiles pinned when the keyboard shrinks a tall rail below the floor', () => {
    const sizes = [300, 240, 120]
    const open = stableRailHeight(stableRailHeight(null, 72, 1000), 72, 640)
    expect(shouldFoldSecondary(1000, ...sizes as [number, number, number])).toBe(false)
    expect(shouldFoldSecondary(640, ...sizes as [number, number, number])).toBe(true)
    expect(shouldFoldSecondary(open.height, ...sizes as [number, number, number])).toBe(false)
  })
})

function renderRail() {
  return render(
    <AdaptiveMobileRail
      data-testid="rail"
      className="h-full"
      top={<button>Sessions</button>}
      apps={<button>App one</button>}
      secondary={<><button>Terminal</button><button>Customize</button></>}
      bottom={<button>Settings</button>}
    />,
  )
}

describe('AdaptiveMobileRail', () => {
  it('pins the secondary tiles outside the Apps scroller when there is room', () => {
    renderRail()
    const rail = screen.getByTestId('rail')
    const apps = within(rail).getByTestId('mobile-nav-rail-apps')
    expect(rail).toHaveAttribute('data-secondary-folded', 'false')
    expect(apps).not.toContainElement(screen.getByRole('button', { name: 'Terminal' }))
    expect(within(rail).queryByRole('separator', { hidden: true })).toBeNull()
  })

  it('folds them into the Apps scroller, after the apps and behind a divider, on a short rail', () => {
    const height = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(700)
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const h = this.getAttribute('data-testid') === 'mobile-nav-rail-secondary' ? 240 : 200
      return { height: h, width: 72, top: 0, left: 0, bottom: h, right: 72, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
    })
    try {
      renderRail()
      const rail = screen.getByTestId('rail')
      const apps = within(rail).getByTestId('mobile-nav-rail-apps')
      expect(rail).toHaveAttribute('data-secondary-folded', 'true')
      const terminal = screen.getByRole('button', { name: 'Terminal' })
      expect(apps).toContainElement(terminal)
      // After the apps, never before them: the apps are the reason to fold.
      const appOne = screen.getByRole('button', { name: 'App one' })
      expect(appOne.compareDocumentPosition(terminal) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(within(apps).getByRole('separator', { hidden: true })).toBeInTheDocument()
      expect(apps).not.toContainElement(screen.getByRole('button', { name: 'Settings' }))
    } finally {
      height.mockRestore()
      rect.mockRestore()
    }
  })
})
