/**
 * KiroAccountStatusLine's "Go to Kiro account" link sits on the Agent Harness
 * tab next to the subagent number fields, whose draft registers a leave guard.
 * A plain click must ask that guard before the tab unmounts the draft.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import {
  NavigationLeaveGuardProvider,
  useRegisterNavigationLeaveGuard,
} from '../../components/NavigationLeaveGuard'
import { KiroAccountStatusLine } from './KiroAccountStatusLine'
import { KIRO_ACCOUNT_SETTINGS_TAB } from './kiroSignInLink'

vi.mock('../../api/client', () => ({
  api: { kasLoginStatus: vi.fn(async () => ({ authenticated: false })), kasLoginSources: vi.fn(async () => ({ kiro_crew: { stored: false }, kiro_cli: { stored: false } })) },
}))
vi.mock('./kiroSignedInAccount', () => ({
  useSignedInAccount: () => ({ name: null, hidden: false }),
}))

function Guarded({ guard, children }: { guard: () => boolean; children: React.ReactNode }) {
  useRegisterNavigationLeaveGuard(guard)
  return <>{children}</>
}
function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}
function renderGuarded(guard: () => boolean) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={['/settings/agent']}>
        <NavigationLeaveGuardProvider>
          <Guarded guard={guard}>
            <KiroAccountStatusLine />
            <Where />
          </Guarded>
        </NavigationLeaveGuardProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('KiroAccountStatusLine link asks the leave guard', () => {
  it('stays on the tab when the guard keeps the draft', () => {
    const guard = vi.fn(() => false)
    renderGuarded(guard)
    fireEvent.click(screen.getByTestId('kiro-account-status-link'))
    expect(guard).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('where').textContent).toBe('/settings/agent')
  })

  it('goes to the Kiro account tab when the guard allows it', () => {
    const guard = vi.fn(() => true)
    renderGuarded(guard)
    fireEvent.click(screen.getByTestId('kiro-account-status-link'))
    expect(guard).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('where').textContent).toBe(`/settings/${KIRO_ACCOUNT_SETTINGS_TAB}`)
  })
})
