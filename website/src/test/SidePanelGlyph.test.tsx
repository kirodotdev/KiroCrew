import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { setSidePanelDock } from '../hooks/useSidePanelDock'

const mobile = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))
vi.mock('../components/icons/panels', () => {
  const stub = (name: string) => (props: { size?: number }) => <svg data-testid={name} data-size={props.size} />
  return {
    PanelRightSolid: stub('right-solid'),
    PanelRightLight: stub('right-light'),
    PanelBottomSolid: stub('bottom-solid'),
    PanelBottomLight: stub('bottom-light'),
  }
})

import { SidePanelDockHost, SidePanelGlyph } from '../components/SidePanelGlyph'

describe('SidePanelGlyph', () => {
  beforeEach(() => { localStorage.clear(); mobile.value = false; setSidePanelDock('right') })
  afterEach(() => { setSidePanelDock('right') })

  it('draws the right-dock glyph while the panel docks right', () => {
    render(<SidePanelGlyph size={15} />)
    expect(screen.getByTestId('right-solid').getAttribute('data-size')).toBe('15')
  })

  it('follows a flip to the bottom dock live', () => {
    render(<SidePanelGlyph />)
    act(() => setSidePanelDock('bottom'))
    expect(screen.getByTestId('bottom-solid')).toBeTruthy()
  })

  it('picks the open (light) variant for each dock', () => {
    const { unmount } = render(<SidePanelGlyph light />)
    expect(screen.getByTestId('right-light')).toBeTruthy()
    unmount()
    act(() => setSidePanelDock('bottom'))
    render(<SidePanelGlyph light />)
    expect(screen.getByTestId('bottom-light')).toBeTruthy()
  })

  it('keeps the right glyph on mobile, where the panel always opens right', () => {
    mobile.value = true
    act(() => setSidePanelDock('bottom'))
    render(<SidePanelGlyph />)
    expect(screen.getByTestId('right-solid')).toBeTruthy()
  })

  it('draws the right glyph under a host that cannot dock bottom', () => {
    act(() => setSidePanelDock('bottom'))
    render(
      <SidePanelDockHost value={false}>
        <SidePanelGlyph />
        <SidePanelGlyph light />
      </SidePanelDockHost>,
    )
    expect(screen.getByTestId('right-solid')).toBeTruthy()
    expect(screen.getByTestId('right-light')).toBeTruthy()
  })

  it('follows the dock under a host that can dock bottom', () => {
    act(() => setSidePanelDock('bottom'))
    render(
      <SidePanelDockHost value={true}>
        <SidePanelGlyph />
      </SidePanelDockHost>,
    )
    expect(screen.getByTestId('bottom-solid')).toBeTruthy()
  })
})
