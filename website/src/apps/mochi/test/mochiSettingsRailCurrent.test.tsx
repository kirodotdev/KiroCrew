/**
 * Mochi's settings rail picks one section at a time and paints the open one with an inline
 * style. A style is invisible to assistive technology, so a screen reader heard twelve
 * identical buttons. The open section's row carries `aria-current="page"`; no other row does.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

vi.mock('../src/mochiApi', () => ({
  api: {
    hasShell: true,
    getConfig: async () => ({
      mochi: { petName: 'Mochi', petInstance: 'self' },
      shortcuts: {
        toggleWindow: 'CommandOrControl+Shift+M',
        screenCapture: 'CommandOrControl+Shift+S',
        hideAll: 'CommandOrControl+Shift+H',
      },
      window: { chatAlwaysOnTop: true },
    }),
    getMochiTrustLevel: async () => 'default',
    getStats: async () => ({}),
    instancesList: async () => ({
      state: 'ready',
      instances: [{ id: 'self', name: 'This instance', local_port: 5476, status: { state: 'connected' } }],
    }),
    updateConfig: async () => undefined,
    setMochiTrustLevel: async () => undefined,
    applyShortcuts: async () => ({}),
    setPetInstance: async () => true,
    onSettingsCloseRequested: () => () => {},
  },
}))

const { SettingsPanel } = await import('../src/renderer/SettingsPanel')

const railRows = () => within(screen.getByRole('navigation')).getAllByRole('button')

describe('Mochi settings rail: the open section is the current page', () => {
  it('marks the open section with aria-current="page"', async () => {
    render(<SettingsPanel onClose={vi.fn()} />)
    expect(await screen.findByRole('button', { name: 'General' })).toHaveAttribute('aria-current', 'page')
  })

  it('marks no other section', async () => {
    render(<SettingsPanel onClose={vi.fn()} />)
    await screen.findByRole('button', { name: 'General' })
    const others = railRows().filter(row => row.textContent !== 'General')
    expect(others.length).toBeGreaterThan(1)
    expect(others.filter(row => row.hasAttribute('aria-current'))).toEqual([])
  })

  it('moves the mark to the section that was opened', async () => {
    const user = userEvent.setup()
    render(<SettingsPanel onClose={vi.fn()} />)
    await user.click(await screen.findByRole('button', { name: 'Instances' }))
    // The Instances pane loads its list; wait for it so no update lands after the test.
    await screen.findByText('This instance')
    expect(screen.getByRole('button', { name: 'Instances' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: 'General' })).not.toHaveAttribute('aria-current')
  })
})
