import { describe, expect, it } from 'vitest'
import { act, render, screen, within } from '@testing-library/react'
import { AdaptiveMobileRail } from '../shell/nav/adaptiveMobileRail'
import { MORE_BELOW_MASK } from '../components/ModelEffortDropdown'

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
  it('scrolls top, apps and secondary tiles as one region above a pinned Settings', () => {
    renderRail()
    const rail = screen.getByTestId('rail')
    const scroller = within(rail).getByTestId('mobile-nav-rail-scroller')
    const sessions = screen.getByRole('button', { name: 'Sessions' })
    const appOne = screen.getByRole('button', { name: 'App one' })
    const terminal = screen.getByRole('button', { name: 'Terminal' })
    for (const el of [sessions, appOne, terminal]) expect(scroller).toContainElement(el)
    expect(scroller).not.toContainElement(screen.getByRole('button', { name: 'Settings' }))
    // Nothing inside the region scrolls on its own.
    expect(scroller.querySelectorAll('.overflow-y-auto')).toHaveLength(0)
    // The secondary tiles follow the apps, behind a divider.
    expect(appOne.compareDocumentPosition(terminal) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(scroller).getByRole('separator', { hidden: true })).toBeInTheDocument()
  })

  it('fades the bottom edge only while tiles sit below the fold', () => {
    renderRail()
    const scroller = screen.getByTestId('mobile-nav-rail-scroller')
    const mask = MORE_BELOW_MASK.split(' ')[0]
    const setOverflow = (hidden: number) => {
      Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => 100 })
      Object.defineProperty(scroller, 'scrollHeight', { configurable: true, get: () => 100 + hidden })
      Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => 0 })
      act(() => { scroller.dispatchEvent(new Event('scroll')) })
    }
    setOverflow(0)
    expect(scroller.className).not.toContain(mask)
    setOverflow(60)
    expect(scroller.className).toContain(mask)
    setOverflow(0)
    expect(scroller.className).not.toContain(mask)
  })
})
