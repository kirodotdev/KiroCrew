/**
 * A failed chunk load of the organization sign-in form stays inside the chooser.
 *
 * The form loads in its own chunk after the Work account choice is clicked.
 * When that chunk fails to load, the failure must show in the form's place,
 * with Try Again, and the other sign-in choices must stay on screen: the throw
 * never reaches the host's boundary (the Settings card, the sign-in notice, the
 * chat page). Try Again must run the import again, because React.lazy caches a
 * rejected import. Focus stays on the slot that holds the notice, never on the
 * page.
 */
import '../i18n/all'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'

import KasLoginGate from '../components/KasLoginGate'
import { i18nT } from '../i18n/t'
import { renderWithProviders } from './helpers'

const chunk = vi.hoisted(() => ({ fail: true, imports: 0 }))

// The form's module as its chunk: an import that rejects the way a browser
// rejects a module script it could not fetch, until the test restores it.
vi.mock('../components/KasOrganizationForm', () => {
  chunk.imports += 1
  if (chunk.fail) throw new TypeError('Importing a module script failed.')
  return { default: () => <div data-testid="kas-login-org-form"><input aria-label="org lookup" autoFocus /></div> }
})

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn().mockResolvedValue({
        authenticated: false,
        provider: null,
        identity: null,
        transport: 'loopback',
      }),
      sessionsUsageRefresh: vi.fn().mockRejectedValue(new Error('no refresh in this test')),
    },
  }
})

/** A named ceiling for the lazy import, its rejection and the boundary's commit on a loaded worker. */
const CHUNK_SETTLE_TIMEOUT_MS = 5000

afterEach(() => { vi.restoreAllMocks() })

describe('KasLoginGate when the organization form chunk fails to load', () => {
  it('shows the failure inline, keeps the other choices, and Try Again loads the form', async () => {
    // React logs the caught render error; the boundary handling it is the point.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    renderWithProviders(<KasLoginGate />)
    fireEvent.click(
      await screen.findByRole('button', { name: 'Work account — sign in with my work email' }),
    )

    const tryAgain = await screen.findByRole(
      'button', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    expect(screen.getByText(i18nT('components.kasLogin.could_not_start_sign_in'))).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue with Google' }), 'the chooser stays mounted').toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: i18nT('components.askAgent.ask_the_agent') }),
      'no hand-off beside the unsaved lookup and region fields',
    ).toBeNull()
    expect(chunk.imports).toBe(1)
    const focused = document.activeElement
    expect(focused, 'focus is not left on the page').not.toBe(document.body)
    expect(focused?.contains(tryAgain), 'focus holds the failure notice and its Try Again').toBe(true)

    chunk.fail = false
    fireEvent.click(tryAgain)
    expect(
      await screen.findByTestId('kas-login-org-form', {}, { timeout: CHUNK_SETTLE_TIMEOUT_MS }),
      'Try Again brings the form once its chunk loads',
    ).toBeInTheDocument()
    expect(chunk.imports, 'Try Again runs the import again').toBe(2)
    await waitFor(
      () => expect(
        screen.getByTestId('kas-login-org-form').parentElement,
        'the loaded form\'s slot is not focusable',
      ).not.toHaveAttribute('tabindex'),
      { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    expect(document.activeElement, 'the form\'s own autoFocus places focus').toBe(
      screen.getByRole('textbox', { name: 'org lookup' }),
    )
  })
})
