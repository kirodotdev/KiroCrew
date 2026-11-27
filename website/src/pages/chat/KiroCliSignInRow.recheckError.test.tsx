import { screen } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import KiroCliSignInRow from './KiroCliSignInRow'

const SIGNED_OUT = {
  platform: 'Linux',
  installed: true,
  authenticated: false,
  login_command: 'kiro-cli login',
  sso_login_command: 'kiro-cli login --use-device-flow --license pro',
  bundled_cli: false,
}

// The shared entry after a Check again that failed: the last answer is kept and
// the query is in error. Mocked at the hook so the row's own rendering is pinned.
vi.mock('../../hooks/useKiroPrerequisite', () => ({
  useKiroPrerequisite: () => ({
    statusQuery: { data: SIGNED_OUT, isError: true, error: new Error('gateway unavailable') },
    checkAgain: vi.fn(),
    checking: false,
  }),
}))

describe('KiroCliSignInRow after a failed Check again', () => {
  it('says the check did not run and keeps the last answer', () => {
    renderWithProviders(<KiroCliSignInRow fallback="fallback text" />)
    expect(screen.getByTestId('kiro-cli-sign-in-recheck-error')).toHaveTextContent('gateway unavailable')
    expect(screen.getByText(SIGNED_OUT.login_command)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Check sign-in again/ })).toBeInTheDocument()
  })
})
