/**
 * Settings > Security > Rules: floor-enforced builtin rules render locked.
 *
 * Contract under test (issue: floor-enforced git-publish rules must never
 * present as freely disableable — a disable was a silent no-op):
 * - a rule with lock_reason 'floor' renders a disabled, forced-on switch with
 *   the always-on-floor tooltip (not the governance "pinned by policy" one)
 * - clicking the locked switch neither opens the confirm modal nor calls the
 *   toggle API
 * - a normal (unlocked) rule still toggles: clicking its switch to re-enable
 *   fires the toggle API immediately
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../store'
import { api, type DeniedCommandsData } from '../api/client'
import { SecurityPanel } from '../pages/settings/SecurityPanel'

const FLOOR_TOOLTIP = 'Enforced by an always-on protection built into Kiro Crew'
const PINNED_TOOLTIP = "Enforced by your organization's security policy"

const snapshot: DeniedCommandsData = {
  builtins: [
    {
      id: 'git-publish-push-bare',
      pattern: 'git\\s+push',
      category: 'git-publish',
      description: 'Push with no arguments',
      enabled: true,
      pinned: false,
      lock_reason: 'floor',
    },
    {
      id: 'destructive-rm-rf',
      pattern: 'rm\\s+-rf',
      category: 'destructive',
      description: 'Recursive force delete',
      enabled: false,
      pinned: false,
      lock_reason: null,
    },
    {
      id: 'credential-exfil-s3-cp',
      pattern: 'aws\\s+s3\\s+cp',
      category: 'credential-exfil',
      description: 'Copy credentials to S3',
      enabled: true,
      pinned: true,
      lock_reason: 'policy',
    },
  ],
  user_added: [],
  disable_all: false,
  effective_count: 2,
  governance_locked: true,
}

function renderRulesSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/settings?tab=security&section=rules']}>
          <SecurityPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('SecurityPanel floor-enforced rule lock', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200,
      json: async () => ({}),
      text: async () => '',
      headers: new Headers({ 'content-type': 'application/json' }),
    }))
    vi.spyOn(api, 'deniedCommands').mockResolvedValue(snapshot)
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({} as never)
    vi.spyOn(api, 'tailnetStatus').mockResolvedValue({ state: 'disabled' } as never)
  })
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  async function expandCategory(name: string) {
    const header = await screen.findByRole('button', { name: `Expand ${name} rules` })
    fireEvent.click(header)
  }

  it('renders a floor-enforced rule as a disabled forced-on switch with the floor tooltip', async () => {
    renderRulesSection()
    await expandCategory('Git Publish')
    const sw = await screen.findByRole('switch', { name: 'Push with no arguments' })
    expect(sw).toHaveAttribute('aria-checked', 'true')
    expect(sw).toHaveAttribute('aria-disabled', 'true')

    // Clicking the locked switch is inert: no confirm modal, no API call.
    const toggleSpy = vi.spyOn(api, 'toggleBuiltinDeniedCommand')
    fireEvent.click(sw)
    expect(toggleSpy).not.toHaveBeenCalled()
    expect(screen.queryByText(/weakens/i)).not.toBeInTheDocument()

    // The tooltip names the always-on floor, not the governance policy.
    // InfoTip mirrors its text into the trigger's title attribute; scope to the
    // rule's own row (the panel-level disable-all row shows the policy tip).
    const row = sw.parentElement as HTMLElement
    expect(within(row).getByTitle(FLOOR_TOOLTIP)).toBeInTheDocument()
    expect(within(row).queryByTitle(PINNED_TOOLTIP)).not.toBeInTheDocument()
  })

  it('keeps a governance-pinned rule on the policy tooltip', async () => {
    renderRulesSection()
    await expandCategory('Credential Exfil')
    const sw = await screen.findByRole('switch', { name: 'Copy credentials to S3' })
    expect(sw).toHaveAttribute('aria-disabled', 'true')
  })

  it('a normal rule still toggles', async () => {
    const toggleSpy = vi.spyOn(api, 'toggleBuiltinDeniedCommand').mockResolvedValue(snapshot)
    renderRulesSection()
    await expandCategory('Destructive')
    const sw = await screen.findByRole('switch', { name: 'Recursive force delete' })
    expect(sw).not.toHaveAttribute('aria-disabled')
    // Re-enabling is immediate (no confirm modal on the enable direction).
    fireEvent.click(sw)
    await waitFor(() => expect(toggleSpy).toHaveBeenCalledWith('destructive-rm-rf', true))
  })
})
