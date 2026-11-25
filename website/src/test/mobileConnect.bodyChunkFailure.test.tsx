/**
 * A failed chunk load of the "Connect your phone" dialog's body stays inside the dialog.
 *
 * The dialog opens over the whole dashboard, and only its body is a lazy chunk.
 * When that chunk fails to load (gateway unreachable, a stale chunk after a
 * rebuild, a browser that keeps the failed fetch), the failure must show inside
 * the dialog's own frame, which still closes, and never reach the app shell's
 * root boundary, which would replace the whole dashboard. Try Again must run the
 * import again: React.lazy caches a rejected import, so clearing a boundary's
 * error re-renders the same rejection without fetching anything.
 */
import '../i18n/all'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'

import ErrorBoundary from '../components/ErrorBoundary'
import { MobileConnectDialog, type useMobileConnect } from '../components/mobileConnect'
import { i18nT } from '../i18n/t'

const chunk = vi.hoisted(() => ({ fail: true, imports: 0 }))

// The body's module as its chunk: an import that rejects the way a browser
// rejects a module script it could not fetch, until the test restores it.
vi.mock('../components/MobileConnectModal', () => {
  chunk.imports += 1
  if (chunk.fail) throw new TypeError('Failed to fetch dynamically imported module: /assets/MobileConnectModal.js')
  return { MobileConnectBody: ({ kinds }: { kinds: string[] }) => <div data-testid="mobile-connect-body">{kinds.join(',')}</div> }
})

/** A named ceiling for the lazy import, its rejection and the boundary's commit on a loaded worker. */
const CHUNK_SETTLE_TIMEOUT_MS = 5000

afterEach(() => { vi.restoreAllMocks() })

describe('the Connect your phone dialog when its body chunk fails to load', () => {
  it('shows the failure inside the dialog, which still closes, and Try Again imports the chunk again', async () => {
    // React logs the caught render error; the boundary handling it is the point.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const setMobileConnectOpen = vi.fn()
    const mobileConnect = {
      mobileConnectOpen: true,
      setMobileConnectOpen,
      mobileConnectKinds: ['login_link'],
      hasRenderableMobileConnect: true,
    } as unknown as ReturnType<typeof useMobileConnect>
    render(
      <ErrorBoundary scope="test-app-shell" fallback={<div data-testid="app-shell-boundary-fallback" />}>
        <MobileConnectDialog mobileConnect={mobileConnect} />
      </ErrorBoundary>,
    )
    const dialog = screen.getByRole('dialog')
    const tryAgain = await within(dialog).findByRole(
      'button', { name: i18nT('components.errorBoundary.try_again') }, { timeout: CHUNK_SETTLE_TIMEOUT_MS },
    )
    expect(chunk.imports).toBe(1)
    expect(within(dialog).getByText(i18nT('components.mobileConnect.dialog_load_failed'))).toBeInTheDocument()
    expect(screen.queryByTestId('app-shell-boundary-fallback'), 'the failure stays inside the dialog').toBeNull()

    fireEvent.click(within(dialog).getByRole('button', { name: i18nT('components.mobileConnect.close') }))
    expect(setMobileConnectOpen).toHaveBeenCalledWith(false)

    chunk.fail = false
    fireEvent.click(tryAgain)
    expect(
      await within(dialog).findByTestId('mobile-connect-body', {}, { timeout: CHUNK_SETTLE_TIMEOUT_MS }),
    ).toHaveTextContent('login_link')
    expect(chunk.imports, 'Try Again runs the import again').toBe(2)
  })
})
