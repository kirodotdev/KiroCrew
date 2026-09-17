/**
 * `OfflineMenuReason` renders nothing while connected, so a test that drives only
 * the offline case passes without ever exercising the guard.
 */
import { describe, it, expect } from 'vitest'
import { screen } from '@testing-library/react'
import { createTestStore, renderWithProviders } from './helpers'
import type { RootState } from '../store'
import OfflineMenuReason from '../components/OfflineMenuReason'

// renderWithProviders takes a store; a preloadedState key is silently ignored.
const storeWith = (connected: boolean) => createTestStore({
  dashboard: {
    connected,
    status: { platform: 'darwin' },
    slots: [],
    approvalMode: 'normal',
  } as unknown as RootState['dashboard'],
})

describe('OfflineMenuReason', () => {
  it('renders nothing while the gateway is connected', () => {
    renderWithProviders(<OfflineMenuReason />, { store: storeWith(true) })
    expect(screen.queryByTestId('menu-offline-reason')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('states the reason as a status row while the gateway is offline', () => {
    renderWithProviders(<OfflineMenuReason />, { store: storeWith(false) })
    const row = screen.getByTestId('menu-offline-reason')
    expect(row).toHaveAttribute('role', 'status')
    expect(row.textContent?.trim()).not.toBe('')
  })

  it('honours a caller-supplied testId so a host menu can target its own row', () => {
    renderWithProviders(<OfflineMenuReason testId="submenu-offline-reason" />, {
      store: storeWith(false),
    })
    expect(screen.getByTestId('submenu-offline-reason')).toBeInTheDocument()
    expect(screen.queryByTestId('menu-offline-reason')).toBeNull()
  })
})
