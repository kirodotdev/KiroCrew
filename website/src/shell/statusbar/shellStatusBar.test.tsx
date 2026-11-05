import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { ShellStatusBar } from './shellStatusBar'
import type { AppShell } from '../appShell'

const toggle = vi.hoisted(() => vi.fn())
vi.mock('../../hooks/useBottomTerminal', () => ({ toggleBottomTerminal: toggle }))

function shellWith(over: Partial<AppShell>): AppShell {
  return {
    terminalEnabled: true, terminalPoppedOut: false, bottomTerminalOpen: false, activeSlotProject: '/proj',
    hasRenderableMobileConnect: true, mobileConnectOpen: false, setMobileConnectOpen: vi.fn(),
    ...over,
  } as unknown as AppShell
}

describe('ShellStatusBar', () => {
  it('toggles the docked terminal and opens phone connect', () => {
    const setMobileConnectOpen = vi.fn()
    render(<ShellStatusBar shell={shellWith({ setMobileConnectOpen })} />)
    expect(screen.getByRole('toolbar', { name: 'Status bar' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Terminal' }))
    expect(toggle).toHaveBeenCalledWith('/proj')

    fireEvent.click(screen.getByRole('button', { name: 'Connect your phone' }))
    expect(setMobileConnectOpen).toHaveBeenCalledWith(true)
  })

  it('marks the terminal button pressed while the panel is open', () => {
    render(<ShellStatusBar shell={shellWith({ bottomTerminalOpen: true })} />)
    expect(screen.getByRole('button', { name: 'Terminal' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('renders nothing when neither control is available', () => {
    const { container } = render(<ShellStatusBar shell={shellWith({ terminalEnabled: false, hasRenderableMobileConnect: false })} />)
    expect(container).toBeEmptyDOMElement()
  })
})
